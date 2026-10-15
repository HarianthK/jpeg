# Writes a real baseline JPEG: YCbCr, 8x8 DCT, quantization, zigzag, then Huffman coding.
# Run: python jpeg.py IN.png [-o OUT.jpg] [-q 75] [--444]
import math
import struct
import sys

# Annex K.1, in natural (row by row) order. Quality scales them; see quant_tables.
LUMA_Q = [16, 11, 10, 16, 24, 40, 51, 61,
          12, 12, 14, 19, 26, 58, 60, 55,
          14, 13, 16, 24, 40, 57, 69, 56,
          14, 17, 22, 29, 51, 87, 80, 62,
          18, 22, 37, 56, 68, 109, 103, 77,
          24, 35, 55, 64, 81, 104, 113, 92,
          49, 64, 78, 87, 103, 121, 120, 101,
          72, 92, 95, 98, 112, 100, 103, 99]
CHROMA_Q = [17, 18, 24, 47, 99, 99, 99, 99,
            18, 21, 26, 66, 99, 99, 99, 99,
            24, 26, 56, 99, 99, 99, 99, 99,
            47, 66, 99, 99, 99, 99, 99, 99] + [99] * 32

# ZIGZAG[k] is the natural-order index of the k-th coefficient sent.
ZIGZAG = [0, 1, 8, 16, 9, 2, 3, 10, 17, 24, 32, 25, 18, 11, 4, 5,
          12, 19, 26, 33, 40, 48, 41, 34, 27, 20, 13, 6, 7, 14, 21, 28,
          35, 42, 49, 56, 57, 50, 43, 36, 29, 22, 15, 23, 30, 37, 44, 51,
          58, 59, 52, 45, 38, 31, 39, 46, 53, 60, 61, 54, 47, 55, 62, 63]

# Annex K.3 to K.6: how many codes of each length 1..16, then the symbols in code order.
DC_LUMA = ([0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0], bytes(range(12)))
DC_CHROMA = ([0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0], bytes(range(12)))
AC_LUMA = ([0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D], bytes.fromhex(
    "01020300041105122131410613516107227114328191a1082342b1c11552d1f0"
    "2433627282090a161718191a25262728292a3435363738393a434445464748494a"
    "535455565758595a636465666768696a737475767778797a838485868788898a"
    "92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9ca"
    "d2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9fa"))
AC_CHROMA = ([0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77], bytes.fromhex(
    "000102031104052131061241510761711322328108144291a1b1c109233352f0"
    "156272d10a162434e125f11718191a262728292a35363738393a434445464748494a"
    "535455565758595a636465666768696a737475767778797a82838485868788898a"
    "92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9ca"
    "d2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9fa"))

# The DCT as a matrix: row u holds C(u)/2 * cos((2x+1)u pi/16), so a block's DCT is M f M^T.
DCT = [[(math.sqrt(0.125) if u == 0 else 0.5) * math.cos((2 * x + 1) * u * math.pi / 16) for x in range(8)]
       for u in range(8)]


def quant_tables(quality):
    # The IJG formula: 50 is the table as printed, lower scales it up, higher down to all ones at 100.
    if not 1 <= quality <= 100:
        raise ValueError("quality must be 1 to 100")
    scale = 5000 // quality if quality < 50 else 200 - 2 * quality
    return [[min(max((q * scale + 50) // 100, 1), 255) for q in table] for table in (LUMA_Q, CHROMA_Q)]


def huffman_codes(table):
    # Canonical codes (Annex C): count up within a length, then shift left for the next length.
    bits, values = table
    codes, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[values[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return codes


def ycbcr(r, g, b):
    # JFIF's conversion, full range 0..255 for all three.
    return (0.299 * r + 0.587 * g + 0.114 * b,
            -0.168736 * r - 0.331264 * g + 0.5 * b + 128,
            0.5 * r - 0.418688 * g - 0.081312 * b + 128)


def planes(rgb, width, height, mcu):
    # Three level-shifted planes, padded to whole MCUs by repeating the last row and column.
    pw, ph = -(-width // mcu) * mcu, -(-height // mcu) * mcu
    out = ([], [], [])
    for y in range(ph):
        row = min(y, height - 1) * width
        rows = ([], [], [])
        for x in range(pw):
            i = 3 * (row + min(x, width - 1))
            for plane, v in zip(rows, ycbcr(rgb[i], rgb[i + 1], rgb[i + 2])):
                plane.append(v - 128)
        for plane, r in zip(out, rows):
            plane.append(r)
    return out


def halve(plane):
    # 4:2:0: each chroma sample is the mean of a 2x2 square, centred between the four pixels.
    return [[(a[2 * x] + a[2 * x + 1] + b[2 * x] + b[2 * x + 1]) / 4 for x in range(len(a) // 2)]
            for a, b in zip(plane[0::2], plane[1::2])]


def fdct(block):
    # Separable: rows first, then columns. block and result are 64 values in natural order.
    rows = [[sum(block[8 * y + x] * DCT[u][x] for x in range(8)) for u in range(8)] for y in range(8)]
    return [sum(DCT[v][y] * rows[y][u] for y in range(8)) for v in range(8) for u in range(8)]


def coefficients(block, q):
    # DCT, divide by the table, round, then read out in zigzag order. Float noise is snapped off
    # first so an exact half (1020 / 136) rounds away from zero, as libjpeg does; see DOCS.md.
    f = fdct(block)
    out = []
    for z in ZIGZAG:
        v = round(f[z], 6) / q[z]
        out.append(int(v + 0.5) if v >= 0 else -int(0.5 - v))
    return out


def category(v):
    # The size class of v, and its bits: v itself if positive, else v - 1 in that many bits.
    s = abs(v).bit_length()
    return s, (v if v >= 0 else v + (1 << s) - 1)


class BitWriter:
    # JPEG packs bits high end first, and a 0x00 follows every 0xFF so it cannot read as a marker.
    def __init__(self):
        self.out = bytearray()
        self.acc = 0
        self.count = 0

    def write(self, value, n):
        self.acc = (self.acc << n) | (value & ((1 << n) - 1))
        self.count += n
        while self.count >= 8:
            self.count -= 8
            byte = (self.acc >> self.count) & 0xFF
            self.out.append(byte)
            if byte == 0xFF:
                self.out.append(0)
        self.acc &= (1 << self.count) - 1

    def flush(self):
        # The last byte is filled with ones.
        if self.count:
            self.write((1 << (8 - self.count)) - 1, 8 - self.count)


def encode_block(w, zz, prev_dc, dc, ac):
    # DC is sent as the change from the last block of this component; AC as (zeros before, size).
    s, bits = category(zz[0] - prev_dc)
    w.write(*dc[s])
    w.write(bits, s)
    run = 0
    for v in zz[1:]:
        if v == 0:
            run += 1
            continue
        while run > 15:
            w.write(*ac[0xF0])
            run -= 16
        s, bits = category(v)
        w.write(*ac[(run << 4) | s])
        w.write(bits, s)
        run = 0
    if run:
        w.write(*ac[0x00])
    return zz[0]


def segment(marker, body):
    return struct.pack(">HH", marker, len(body) + 2) + body


def encode(rgb, width, height, quality=75, subsample=True):
    # rgb is width * height * 3 bytes, row by row. Returns the bytes of a JFIF file.
    if not (0 < width < 65536 and 0 < height < 65536) or len(rgb) != width * height * 3:
        raise ValueError("rgb must be width * height * 3 bytes, each side 1 to 65535")
    luma_q, chroma_q = quant_tables(quality)
    mcu = 16 if subsample else 8
    y_plane, cb, cr = planes(rgb, width, height, mcu)
    if subsample:
        cb, cr = halve(cb), halve(cr)
    dc_l, ac_l, dc_c, ac_c = (huffman_codes(t) for t in (DC_LUMA, AC_LUMA, DC_CHROMA, AC_CHROMA))
    w = BitWriter()
    prev = [0, 0, 0]

    def block(plane, x0, y0):
        return [plane[y0 + j][x0 + i] for j in range(8) for i in range(8)]

    # Interleaved: each MCU holds its luma blocks left to right, top to bottom, then one Cb and one Cr.
    for my in range(0, len(y_plane), mcu):
        for mx in range(0, len(y_plane[0]), mcu):
            for dy in range(0, mcu, 8):
                for dx in range(0, mcu, 8):
                    prev[0] = encode_block(w, coefficients(block(y_plane, mx + dx, my + dy), luma_q), prev[0], dc_l, ac_l)
            cx, cy = (mx // 2, my // 2) if subsample else (mx, my)
            prev[1] = encode_block(w, coefficients(block(cb, cx, cy), chroma_q), prev[1], dc_c, ac_c)
            prev[2] = encode_block(w, coefficients(block(cr, cx, cy), chroma_q), prev[2], dc_c, ac_c)
    w.flush()

    luma_sampling = 0x22 if subsample else 0x11
    return b"".join([
        b"\xff\xd8",
        segment(0xFFE0, b"JFIF\0\x01\x01\x00" + struct.pack(">HHBB", 1, 1, 0, 0)),
        segment(0xFFDB, b"\x00" + bytes(luma_q[z] for z in ZIGZAG) + b"\x01" + bytes(chroma_q[z] for z in ZIGZAG)),
        segment(0xFFC0, struct.pack(">BHHB", 8, height, width, 3) + bytes([1, luma_sampling, 0, 2, 0x11, 1, 3, 0x11, 1])),
        segment(0xFFC4, b"".join(bytes([tc_th]) + bytes(t[0]) + t[1] for tc_th, t in
                                 ((0x00, DC_LUMA), (0x10, AC_LUMA), (0x01, DC_CHROMA), (0x11, AC_CHROMA)))),
        segment(0xFFDA, bytes([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0])),
        bytes(w.out),
        b"\xff\xd9",
    ])


if __name__ == "__main__":
    argv = sys.argv[1:]
    args = [a for i, a in enumerate(argv) if not a.startswith("-") and (i == 0 or argv[i - 1] not in ("-o", "-q"))]
    if not args:
        sys.exit("usage: python jpeg.py IN.png [-o OUT.jpg] [-q 75] [--444]")
    from PIL import Image  # only to read the input picture; the JPEG is all this file
    source = args[0]
    out = argv[argv.index("-o") + 1] if "-o" in argv else source.rsplit(".", 1)[0] + ".jpg"
    quality = int(argv[argv.index("-q") + 1]) if "-q" in argv else 75
    image = Image.open(source).convert("RGB")
    packed = encode(image.tobytes(), image.width, image.height, quality, subsample="--444" not in argv)
    open(out, "wb").write(packed)
    raw = image.width * image.height * 3
    print(f"{source}: {image.width}x{image.height}, {raw} bytes of pixels -> {len(packed)} bytes "
          f"({raw / len(packed):.1f} to 1), quality {quality}, {'4:4:4' if '--444' in argv else '4:2:0'}")
