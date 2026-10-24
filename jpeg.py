# Writes and reads baseline JPEGs: YCbCr, 8x8 DCT, quantization, zigzag, then Huffman coding.
# Run: python jpeg.py IN.png [-o OUT.jpg] [-q 75] [--444] [--grey] [--optimize], or -d IN.jpg [-o OUT.png]
import math
import struct
import sys
import types

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


def build_table(counts):
    # Annex K.2 (figures K.1 to K.4), as libjpeg does it. counts holds 256 symbol counts.
    # A dummy symbol 256 with count 1 takes the all-ones code, then is dropped; see DOCS.md.
    freq = list(counts) + [1]
    size, others = [0] * 257, [-1] * 257
    while True:
        # The two least frequent; ties go to the higher symbol, so the dummy ends up deepest.
        c1 = max((i for i in range(257) if freq[i]), key=lambda i: (-freq[i], i))
        rest = [i for i in range(257) if freq[i] and i != c1]
        if not rest:
            break
        c2 = max(rest, key=lambda i: (-freq[i], i))
        freq[c1] += freq[c2]
        freq[c2] = 0
        for c in (c1, c2):
            size[c] += 1
            while others[c] >= 0:
                c = others[c]
                size[c] += 1
        c = c1
        while others[c] >= 0:
            c = others[c]
        others[c] = c2
    bits = [0] * (max(size) + 1)
    for i in range(257):
        if size[i]:
            bits[size[i]] += 1
    # K.3: while a code is longer than 16, move two leaves up and split a shorter leaf to make room.
    for i in range(len(bits) - 1, 16, -1):
        while bits[i]:
            j = i - 2
            while not bits[j]:
                j -= 1
            bits[i] -= 2
            bits[i - 1] += 1
            bits[j + 1] += 2
            bits[j] -= 1
    bits = (bits + [0] * 17)[:17]
    # Drop the dummy: it is last in code order, so it takes one code from the longest length.
    longest = max(i for i in range(17) if bits[i])
    bits[longest] -= 1
    values = bytes(i for length in range(1, max(size) + 1) for i in range(256) if size[i] == length)
    return bits[1:], values


class Tally:
    # Stands in for a code table on the first pass: counts each symbol asked for, codes nothing.
    def __init__(self):
        self.counts = [0] * 256

    def __getitem__(self, symbol):
        self.counts[symbol] += 1
        return 0, 0


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


def encode(rgb, width, height, quality=75, subsample=True, optimize=False, grey=False, restart=0):
    # rgb is width * height * 3 bytes, row by row. Returns the bytes of a JFIF file.
    # optimize counts this picture's symbols first and builds its own Huffman tables from them.
    if not 0 <= restart < 65536:
        raise ValueError("restart is 0 to 65535 MCUs")
    if not (0 < width < 65536 and 0 < height < 65536) or len(rgb) != width * height * 3:
        raise ValueError("rgb must be width * height * 3 bytes, each side 1 to 65535")
    luma_q, chroma_q = quant_tables(quality)
    # grey keeps only the brightness channel: one block per MCU, one component in the file.
    mcu = 16 if subsample and not grey else 8
    y_plane, cb, cr = planes(rgb, width, height, mcu)
    if subsample:
        cb, cr = halve(cb), halve(cr)

    def block(plane, x0, y0):
        return [plane[y0 + j][x0 + i] for j in range(8) for i in range(8)]

    # Interleaved: each MCU holds its luma blocks left to right, top to bottom, then one Cb and one Cr.
    blocks = []
    for my in range(0, len(y_plane), mcu):
        for mx in range(0, len(y_plane[0]), mcu):
            for dy in range(0, mcu, 8):
                for dx in range(0, mcu, 8):
                    blocks.append((0, coefficients(block(y_plane, mx + dx, my + dy), luma_q)))
            if not grey:
                cx, cy = (mx // 2, my // 2) if subsample else (mx, my)
                blocks.append((1, coefficients(block(cb, cx, cy), chroma_q)))
                blocks.append((2, coefficients(block(cr, cx, cy), chroma_q)))

    # restart: every that many MCUs, end the byte, write RST0 to RST7 in turn and start DC from zero,
    # so a damaged byte spoils only up to the next marker.
    per_mcu = (mcu // 8) ** 2 + (0 if grey else 2)

    def code(w, dc_l, ac_l, dc_c=None, ac_c=None):
        prev = [0, 0, 0]
        for i, (c, zz) in enumerate(blocks):
            if restart and i and i % (per_mcu * restart) == 0:
                w.flush()
                w.out += bytes([0xFF, 0xD0 + (i // (per_mcu * restart) - 1) % 8])
                prev = [0, 0, 0]
            prev[c] = encode_block(w, zz, prev[c], *((dc_l, ac_l) if c == 0 else (dc_c, ac_c)))

    tables = (DC_LUMA, AC_LUMA) if grey else (DC_LUMA, AC_LUMA, DC_CHROMA, AC_CHROMA)
    if optimize:
        tallies = [Tally() for _ in tables]
        code(types.SimpleNamespace(write=lambda value, n: None, flush=lambda: None, out=bytearray()), *tallies)
        tables = [build_table(t.counts) for t in tallies]
    w = BitWriter()
    code(w, *(huffman_codes(t) for t in tables))
    w.flush()

    luma_sampling = 0x22 if subsample else 0x11
    dqt = b"\x00" + bytes(luma_q[z] for z in ZIGZAG)
    if grey:
        sof = struct.pack(">BHHB", 8, height, width, 1) + bytes([1, 0x11, 0])
        sos = bytes([1, 1, 0x00, 0, 63, 0])
    else:
        dqt += b"\x01" + bytes(chroma_q[z] for z in ZIGZAG)
        sof = struct.pack(">BHHB", 8, height, width, 3) + bytes([1, luma_sampling, 0, 2, 0x11, 1, 3, 0x11, 1])
        sos = bytes([3, 1, 0x00, 2, 0x11, 3, 0x11, 0, 63, 0])
    return b"".join([
        b"\xff\xd8",
        segment(0xFFE0, b"JFIF\0\x01\x01\x00" + struct.pack(">HHBB", 1, 1, 0, 0)),
        segment(0xFFDB, dqt),
        segment(0xFFC0, sof),
        segment(0xFFC4, b"".join(bytes([tc_th]) + bytes(t[0]) + t[1] for tc_th, t in
                                 zip((0x00, 0x10, 0x01, 0x11), tables))),
        segment(0xFFDD, struct.pack(">H", restart)) if restart else b"",
        segment(0xFFDA, sos),
        bytes(w.out),
        b"\xff\xd9",
    ])


# Reading. Everything from here on trusts nothing in the file; see DOCS.md.
REFUSED = {0xC1: "extended sequential (SOF1)", 0xC3: "lossless (SOF3)",
           0xC9: "arithmetic coded (SOF9)", 0xCA: "progressive arithmetic coded (SOF10)",
           0xCB: "lossless arithmetic coded (SOF11)", 0xCC: "arithmetic coded (DAC)", 0xDC: "DNL marker"}
REFUSED.update({m: f"hierarchical (marker 0xFF{m:02X})" for m in (0xC5, 0xC6, 0xC7, 0xCD, 0xCE, 0xCF, 0xDE, 0xDF)})
BYTE_BITS = [format(i, "08b") for i in range(256)]


def lookup_table(counts, symbols):
    # A list indexed by the next 16 bits; every entry that starts with a code's bits holds (symbol, length).
    table, code, k = [None] * 65536, 0, 0
    for length in range(1, 17):
        for _ in range(counts[length - 1]):
            if code >= 1 << length:
                raise ValueError("a Huffman table has more codes than its lengths leave room for")
            shift = 16 - length
            table[code << shift:(code + 1) << shift] = [(symbols[k], length)] * (1 << shift)
            code += 1
            k += 1
        code <<= 1
    return table


def read_tables(marker, body, qt, huff):
    # DQT and DHT may each hold several tables back to back.
    while body:
        kind, slot = body[0] >> 4, body[0] & 15
        if marker == 0xDB:
            if kind:
                raise ValueError("16-bit quantization tables (12-bit JPEG) are not supported")
            if slot > 3 or len(body) < 65:
                raise ValueError("a DQT segment is cut short or has a table id above 3")
            qt[slot], body = list(body[1:65]), body[65:]
            continue
        if kind > 1 or slot > 3 or len(body) < 17:
            raise ValueError("a DHT segment is cut short or has a bad table class or id")
        counts = body[1:17]
        total = sum(counts)
        if total > 256 or len(body) < 17 + total:
            raise ValueError("a DHT segment lists more than 256 codes or is cut short")
        huff[kind, slot] = lookup_table(counts, body[17:17 + total])
        body = body[17 + total:]


def read_frame(body):
    # SOF0 or SOF2: precision, height, width, then (id, sampling, table) per component.
    if len(body) < 6:
        raise ValueError("the frame header (SOF) is cut short")
    precision, height, width, n = struct.unpack(">BHHB", body[:6])
    if precision != 8:
        raise ValueError(f"{precision}-bit samples are not supported, only 8-bit")
    if not (width and height):
        raise ValueError("a width or height of 0 (height set later by DNL) is not supported")
    if n not in (1, 3) or len(body) != 6 + 3 * n:
        raise ValueError(f"{n} components are not supported, only 1 (greyscale) or 3 (YCbCr)")
    comps = [dict(id=body[i], h=body[i + 1] >> 4, v=body[i + 1] & 15, tq=body[i + 2]) for i in range(6, 6 + 3 * n, 3)]
    if n == 1:
        comps[0].update(h=1, v=1)  # a lone component is never interleaved, so its factors mean nothing
    hmax, vmax = max(c["h"] for c in comps), max(c["v"] for c in comps)
    for c in comps:
        if not (c["h"] and c["v"]) or hmax % c["h"] or vmax % c["v"] or hmax // c["h"] > 2 or vmax // c["v"] > 2:
            raise ValueError(f"sampling factors {[(c['h'], c['v']) for c in comps]} are not supported")
    if len({c["id"] for c in comps}) != n:
        raise ValueError("two components share an id")
    return width, height, comps


def entropy_data(data, pos):
    # The coded bytes up to the next marker, 0xFF 0x00 turned back into 0xFF, split at each RSTn.
    pieces, rsts, piece = [], [], bytearray()
    while True:
        i = data.find(b"\xff", pos)
        if i < 0 or i + 1 >= len(data):
            raise ValueError("the file ends inside the scan data")
        piece += data[pos:i]
        after = data[i + 1]
        if after == 0:
            piece.append(0xFF)
            pos = i + 2
        elif after == 0xFF:
            pos = i + 1
        elif 0xD0 <= after <= 0xD7:
            pieces.append(piece)
            rsts.append(after - 0xD0)
            piece, pos = bytearray(), i + 2
        else:
            pieces.append(piece)
            return pieces, rsts, i


def idct(coef):
    # The inverse of fdct, M^T F M: down each column that holds anything, then along the rows.
    g = [([sum(DCT[v][y] * col[v] for v in range(8) if col[v]) for y in range(8)], DCT[u])
         for u in range(8) for col in (coef[u::8],) if any(col)]
    return [min(max(int(sum(c[y] * m[x] for c, m in g) + 128.5), 0), 255) for y in range(8) for x in range(8)]


def symbol_at(bits, pos, table):
    entry = table[int(bits[pos:pos + 16], 2)]
    if entry is None:
        raise ValueError("a Huffman code in the scan data matches nothing in its table")
    return entry[0], pos + entry[1]


def received(bits, pos, size, most):
    # The next size bits as a signed value (EXTEND in F.2.2.1); most is what 8-bit samples allow.
    if size > most:
        raise ValueError("a coefficient is bigger than 8-bit baseline allows")
    value = int(bits[pos:pos + size], 2) if size else 0
    if size and value < 1 << (size - 1):
        value -= (1 << size) - 1
    return value, pos + size


def read_block(bits, pos, dc, ac):
    # One baseline block: the DC difference, then (zeros, size) pairs to EOB, in zigzag order.
    coef = [0] * 64
    k = 0
    while k < 64:
        entry = (ac if k else dc)[int(bits[pos:pos + 16], 2)]
        if entry is None:
            raise ValueError("a Huffman code in the scan data matches nothing in its table")
        symbol, n = entry
        pos += n
        run, size = (0, symbol) if k == 0 else (symbol >> 4, symbol & 15)
        if size > (11 if k == 0 else 10):
            raise ValueError("a coefficient is bigger than 8-bit baseline allows")
        if k and not size:
            if run != 15:
                break
            k += 16
            continue
        k += run
        if k > 63:
            raise ValueError("a block has more than 64 coefficients")
        value = int(bits[pos:pos + size], 2) if size else 0
        pos += size
        if size and value < 1 << (size - 1):
            value -= (1 << size) - 1
        coef[k] = value
        k += 1
    return pos, coef


def ac_first(bits, pos, ac, blk, ss, se, al, eobrun):
    # The first pass over a band: like a baseline block's AC part, scaled up by Al, except that an
    # end-of-band can stand for a whole run of blocks with nothing in this band (G.1.2.2).
    if eobrun:
        return pos, eobrun - 1
    k = ss
    while k <= se:
        symbol, pos = symbol_at(bits, pos, ac)
        run, size = symbol >> 4, symbol & 15
        if not size:
            if run == 15:
                k += 16
                continue
            # 2^run blocks end here, this one included, plus run more bits of count.
            eobrun = (1 << run) - 1 + (int(bits[pos:pos + run], 2) if run else 0)
            return pos + run, eobrun
        k += run
        if k > se:
            raise ValueError("a block has more coefficients than its scan's band holds")
        blk[k], pos = received(bits, pos, size, 10)
        blk[k] <<= al
        k += 1
    return pos, 0


def ac_refine(bits, pos, ac, blk, ss, se, al, eobrun):
    # A later pass sends one more bit of each coefficient already known, and places coefficients
    # that only now become 1 or -1 at this precision, counting runs in zeros alone (G.1.2.3).
    p1, m1 = 1 << al, -1 << al
    k = ss
    if not eobrun:
        while k <= se:
            symbol, pos = symbol_at(bits, pos, ac)
            run, size, value = symbol >> 4, symbol & 15, 0
            if size:
                if size != 1:
                    raise ValueError("a refinement scan sent a new coefficient of more than one bit")
                value = p1 if bits[pos] == "1" else m1
                pos += 1
            elif run != 15:
                eobrun = (1 << run) + (int(bits[pos:pos + run], 2) if run else 0)
                pos += run
                break
            while k <= se:
                if blk[k]:
                    if bits[pos] == "1" and not blk[k] & p1:
                        blk[k] += p1 if blk[k] >= 0 else m1
                    pos += 1
                elif run:
                    run -= 1
                else:
                    break
                k += 1
            if value:
                if k > se:
                    raise ValueError("a block has more coefficients than its scan's band holds")
                blk[k] = value
            k += 1
    if eobrun:
        # Inside an end-of-band run only the known coefficients get their bit.
        for k in range(k, se + 1):
            if blk[k]:
                if bits[pos] == "1" and not blk[k] & p1:
                    blk[k] += p1 if blk[k] >= 0 else m1
                pos += 1
        eobrun -= 1
    return pos, eobrun


def own_blocks(c, width, height, hmax, vmax):
    # How many blocks across and down a component's own samples cover, before MCU padding.
    return -(-(-(-width * c["h"] // hmax)) // 8), -(-(-(-height * c["v"] // vmax)) // 8)


ZEROS = (0,) * 64


def read_scan(data, pos, body, frame, qt, huff, interval, coefs, progressive):
    # Decodes one scan into coefs (per component: its quantization table and blocks, zigzag order)
    # and returns where the next marker starts. DOCS.md, "Progressive files", has the four kinds.
    width, height, comps = frame
    ns = body[0] if body else 0
    if not ns or len(body) != 4 + 2 * ns:
        raise ValueError("the SOS segment has the wrong length")
    ss, se, ah, al = body[-3], body[-2], body[-1] >> 4, body[-1] & 15
    if not progressive and (ss, se, ah, al) != (0, 63, 0, 0):
        raise ValueError("a baseline scan must hold all 64 coefficients at full precision")
    if progressive and (se > 63 or ss > se or (ss == 0) != (se == 0) or (ss and ns != 1) or ah > 13 or al > 13):
        raise ValueError(f"a progressive scan with Ss={ss} Se={se} Ah={ah} Al={al} is not valid")
    hmax, vmax = max(c["h"] for c in comps), max(c["v"] for c in comps)
    mcux, mcuy = -(-width // (8 * hmax)), -(-height // (8 * vmax))
    by_id = {c["id"]: c for c in comps}
    scan = []
    for i in range(1, 1 + 2 * ns, 2):
        c = by_id.pop(body[i], None)
        if c is None:
            raise ValueError(f"the scan names component {body[i]}, which is not in the frame or is named twice")
        td, ta = body[i + 1] >> 4, body[i + 1] & 15
        needed = [("quantization", qt, c["tq"], c["tq"])]
        if ss == 0 and not ah:
            needed.append(("DC Huffman", huff, (0, td), td))
        if se:
            needed.append(("AC Huffman", huff, (1, ta), ta))
        for kind, table, key, slot in needed:
            if key not in table:
                raise ValueError(f"component {c['id']} uses {kind} table {slot}, which was never defined")
        if c["id"] in coefs and not progressive:
            raise ValueError(f"component {c['id']} is in two baseline scans")
        # The quantization table is taken when a component is first seen, as libjpeg does.
        # Blocks are made only when the data reaches them, so a header cannot ask for memory alone.
        q, blocks = coefs.setdefault(c["id"], (qt[c["tq"]], {}))
        scan.append((c, huff.get((0, td)), huff.get((1, ta)), blocks, mcux * c["h"]))
    if ns > 1:
        # Interleaved: an MCU holds each component's h by v blocks in turn.
        total = mcux * mcuy
        units = lambda u: [(s, (u // mcux * c["v"] + j) * bw + u % mcux * c["h"] + i)
                           for s, (c, _, _, _, bw) in enumerate(scan) for j in range(c["v"]) for i in range(c["h"])]
    else:
        # A lone component is read block by block, over its own size rather than whole MCUs.
        c, _, _, _, bw = scan[0]
        cbw, cbh = own_blocks(c, width, height, hmax, vmax)
        total = cbw * cbh
        units = lambda u: [(0, u // cbw * bw + u % cbw)]
    interval = interval or total
    pieces, rsts, end_pos = entropy_data(data, pos)
    if len(rsts) != -(-total // interval) - 1 or rsts != [i % 8 for i in range(len(rsts))]:
        raise ValueError(f"expected {-(-total // interval) - 1} restart markers in order, found {len(rsts)} numbered {rsts[:8]}")
    unit = 0
    for piece in pieces:
        bits = "".join(map(BYTE_BITS.__getitem__, piece))
        end = len(bits)
        # Zeros past the end (as libjpeg does) always read as some code, so running out is caught
        # by the check after each block, not mistaken for a bad code. One block reads < 2048 bits.
        bits += "0" * 2048
        pos, pred, eobrun = 0, [0] * ns, 0
        for _ in range(min(interval, total - unit)):
            for s, b in units(unit):
                _, dc, ac, blocks, _ = scan[s]
                blk = blocks.get(b)
                if blk is None:
                    # Inside an end-of-band run an unseen block stays all zeros and is not written.
                    blk = ZEROS if progressive and eobrun else blocks.setdefault(b, [0] * 64)
                if not progressive:
                    pos, zz = read_block(bits, pos, dc, ac)
                    pred[s] += zz[0]
                    zz[0] = pred[s]
                    blk[:] = zz
                elif ss == 0 and not ah:
                    size, pos = symbol_at(bits, pos, dc)
                    diff, pos = received(bits, pos, size, 11)
                    pred[s] += diff
                    blk[0] = pred[s] << al
                elif ss == 0:
                    if bits[pos] == "1":
                        blk[0] |= 1 << al
                    pos += 1
                elif not ah:
                    pos, eobrun = ac_first(bits, pos, ac, blk, ss, se, al, eobrun)
                else:
                    pos, eobrun = ac_refine(bits, pos, ac, blk, ss, se, al, eobrun)
                if pos > end:
                    raise ValueError("the scan data ends in the middle of a block")
            unit += 1
    return end_pos


def to_planes(frame, coefs):
    # Once every scan is in: dequantize each block, inverse DCT it, and lay it in its plane.
    width, height, comps = frame
    hmax, vmax = max(c["h"] for c in comps), max(c["v"] for c in comps)
    mcux, mcuy = -(-width // (8 * hmax)), -(-height // (8 * vmax))
    planes = []
    for c in comps:
        if c["id"] not in coefs:
            raise ValueError(f"component {c['id']} is in the frame but in no scan")
        q, blocks = coefs[c["id"]]
        bw, (cbw, cbh) = mcux * c["h"], own_blocks(c, width, height, hmax, vmax)
        inside = [b for b in blocks if b % bw < cbw and b // bw < cbh]
        if len(inside) != cbw * cbh:
            raise ValueError(f"the scans never reach every block of component {c['id']}")
        plane = [bytearray(cbw * 8) for _ in range(cbh * 8)]
        for b in inside:
            zz = blocks[b]
            coef = [0] * 64
            for k in range(64):
                if zz[k]:
                    coef[ZIGZAG[k]] = zz[k] * q[k]
            samples = idct(coef)
            x0, y0 = b % bw * 8, b // bw * 8
            for r in range(8):
                plane[y0 + r][x0:x0 + 8] = bytes(samples[8 * r:8 * r + 8])
        planes.append((c, plane))
    return planes


def stretch(s):
    # Fancy upsampling, as libjpeg does by default: each new sample is 3/4 the nearer old one
    # and 1/4 the next one out, with the edge sample repeated.
    s = list(s)
    return [v for a, b, c in zip(s[:1] + s[:-1], s, s[1:] + s[-1:]) for v in ((3 * b + a) / 4, (3 * b + c) / 4)]


def to_rgb(frame, planes, is_rgb):
    width, height, comps = frame
    hmax, vmax = max(c["h"] for c in comps), max(c["v"] for c in comps)
    flat = []
    for c, plane in planes:
        cw, ch = -(-width * c["h"] // hmax), -(-height * c["v"] // vmax)
        rows = [list(r[:cw]) for r in plane[:ch]]
        if hmax // c["h"] == 2:
            rows = [stretch(r) for r in rows]
        if vmax // c["v"] == 2:
            rows = list(zip(*map(stretch, zip(*rows))))
        flat.append([v for r in rows[:height] for v in r[:width]])
    rgb = bytearray(3 * width * height)
    if len(flat) == 1:
        rgb[0::3] = rgb[1::3] = rgb[2::3] = bytes(flat[0])
        return bytes(rgb)
    if is_rgb:
        for c, plane in enumerate(flat):
            rgb[c::3] = bytes(min(max(int(v + 0.5), 0), 255) for v in plane)
        return bytes(rgb)
    # JFIF's inverse, the same equations libjpeg uses, rounded and clamped to 0..255.
    y, cb, cr = flat
    rgb[0::3] = bytes(min(max(int(a + 1.402 * (r - 128) + 0.5), 0), 255) for a, r in zip(y, cr))
    rgb[1::3] = bytes(min(max(int(a - 0.344136 * (b - 128) - 0.714136 * (r - 128) + 0.5), 0), 255) for a, b, r in zip(y, cb, cr))
    rgb[2::3] = bytes(min(max(int(a + 1.772 * (b - 128) + 0.5), 0), 255) for a, b in zip(y, cb))
    return bytes(rgb)


def decode(data):
    # Returns width, height and the RGB pixels, row by row, of a baseline or progressive JPEG.
    # Anything else (arithmetic, 12-bit, lossless, broken) is refused with a ValueError.
    data = bytes(data)
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG: it does not start with SOI (FF D8)")
    qt, huff, frame, coefs, scans, interval, pos, transform = {}, {}, None, {}, 0, 0, 2, 1
    while True:
        if pos >= len(data):
            raise ValueError("the file ends before the EOI marker")
        if data[pos] != 0xFF:
            raise ValueError(f"expected a marker at byte {pos}, found 0x{data[pos]:02X}")
        while pos < len(data) and data[pos] == 0xFF:
            pos += 1
        if pos >= len(data):
            raise ValueError("the file ends before the EOI marker")
        marker, start = data[pos], pos - 1
        pos += 1
        if marker == 0xD9:
            break
        if marker in REFUSED:
            raise ValueError(f"{REFUSED[marker]} JPEGs are not supported, only baseline (SOF0) and progressive (SOF2)")
        if marker not in (0xC0, 0xC2, 0xC4, 0xDA, 0xDB, 0xDD, 0xFE) and not 0xE0 <= marker <= 0xEF:
            raise ValueError(f"unknown marker 0xFF{marker:02X} at byte {start}")
        n = int.from_bytes(data[pos:pos + 2], "big")
        if n < 2 or pos + n > len(data):
            raise ValueError(f"the segment at byte {start} runs past the end of the file")
        body, pos = data[pos + 2:pos + n], pos + n
        if marker in (0xDB, 0xC4):
            read_tables(marker, body, qt, huff)
        elif marker == 0xEE and body[:5] == b"Adobe" and len(body) >= 12:
            transform = body[11]  # 0 means the three components are R, G and B, not YCbCr
        elif marker == 0xDD:
            if len(body) != 2:
                raise ValueError("the DRI segment has the wrong length")
            interval = int.from_bytes(body, "big")
        elif marker in (0xC0, 0xC2):
            if frame:
                raise ValueError("the file has two frames")
            frame, progressive = read_frame(body), marker == 0xC2
        elif marker == 0xDA:
            if frame is None:
                raise ValueError("a scan comes before the frame header")
            pos = read_scan(data, pos, body, frame, qt, huff, interval, coefs, progressive)
            scans += 1
    if not scans:
        raise ValueError("the file has no scan (SOS)")
    return frame[0], frame[1], to_rgb(frame, to_planes(frame, coefs), transform == 0)


if __name__ == "__main__":
    argv = sys.argv[1:]
    args = [a for i, a in enumerate(argv) if not a.startswith("-") and (i == 0 or argv[i - 1] not in ("-o", "-q", "--restart"))]
    if not args:
        sys.exit("usage: python jpeg.py IN.png [-o OUT.jpg] [-q 75] [--444] [--grey] [--optimize] [--restart N]\n"
                 "       python jpeg.py -d IN.jpg [-o OUT.png]")
    from PIL import Image  # only to read or write the PNG; the JPEG is all this file
    source = args[0]
    if "-d" in argv:
        out = argv[argv.index("-o") + 1] if "-o" in argv else source.rsplit(".", 1)[0] + ".png"
        packed = open(source, "rb").read()
        try:
            width, height, rgb = decode(packed)
        except ValueError as e:
            sys.exit(f"{source}: {e}")
        Image.frombytes("RGB", (width, height), rgb).save(out)
        print(f"{source}: {len(packed)} bytes -> {width}x{height}, {len(rgb)} bytes of pixels, written to {out}")
        sys.exit()
    out = argv[argv.index("-o") + 1] if "-o" in argv else source.rsplit(".", 1)[0] + ".jpg"
    quality = int(argv[argv.index("-q") + 1]) if "-q" in argv else 75
    image = Image.open(source).convert("RGB")
    packed = encode(image.tobytes(), image.width, image.height, quality, subsample="--444" not in argv,
                    optimize="--optimize" in argv, grey="--grey" in argv,
                    restart=int(argv[argv.index("--restart") + 1]) if "--restart" in argv else 0)
    open(out, "wb").write(packed)
    raw = image.width * image.height * 3
    print(f"{source}: {image.width}x{image.height}, {raw} bytes of pixels -> {len(packed)} bytes "
          f"({raw / len(packed):.1f} to 1), quality {quality}, {'4:4:4' if '--444' in argv else '4:2:0'}"
          f"{', optimized tables' if '--optimize' in argv else ''}")
