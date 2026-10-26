# Run: python test_jpeg.py. Every file we write is decoded by Pillow, never by this project,
# and decode() is measured against Pillow's decoding of files Pillow wrote.
import hashlib
import heapq
import io
import math
import os
import random
import subprocess
import sys
import threading

from PIL import Image, JpegImagePlugin

import jpeg

RENDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trace-rays", "render.png")


def make(width, height, pixel):
    return Image.frombytes("RGB", (width, height), bytes(c for y in range(height) for x in range(width) for c in pixel(x, y)))


rng = random.Random(7)
noise = [[tuple(rng.randrange(256) for _ in range(3)) for _ in range(17)] for _ in range(9)]
IMAGES = {
    "flat colour 40x24": make(40, 24, lambda x, y: (200, 100, 50)),
    "gradient 64x48": make(64, 48, lambda x, y: (x * 4, y * 5, 128)),
    # Black strokes on white, like large type: hard edges in luma only.
    "text-like 64x64": make(64, 64, lambda x, y: (0, 0, 0) if (x % 12 < 3 and y % 20 < 15) or (y % 20 in (0, 1, 7) and x % 24 < 12) else (255, 255, 255)),
    # Saturated stripes three pixels wide, so every chroma 2x2 square straddles a different edge.
    "colour stripes 48x32": make(48, 32, lambda x, y: [(230, 20, 20), (20, 20, 230), (20, 200, 20)][(x // 3 + y // 8) % 3]),
    "noise 17x9": make(17, 9, lambda x, y: noise[y][x]),
    # The pattern with the largest AC amplitude there is, about 1,020 at (4, 4); DOCS.md has why it fits.
    "worst block 16x16": make(16, 16, lambda x, y: (255,) * 3 if ((x + 1) // 2 + (y + 1) // 2) % 2 else (0,) * 3),
    "gradient 33x21": make(33, 21, lambda x, y: (x * 7, 255 - y * 12, (x * y) % 256)),
}
if os.path.exists(RENDER):
    IMAGES["trace-rays render 640x360"] = Image.open(RENDER).convert("RGB")
else:
    print("trace-rays render.png not found, skipped it")

failures = 0


def check(ok, message):
    global failures
    if not ok:
        print("FAIL", message)
        failures += 1


def psnr(a, b):
    mse = sum((x - y) ** 2 for x, y in zip(a, b)) / len(a)
    return math.inf if mse == 0 else 10 * math.log10(255 ** 2 / mse)


def theirs_decode(data):
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


# Quality scaling, worked by hand from the IJG formula: 75 halves the table, 25 doubles it.
luma, chroma = jpeg.quant_tables(50)
check(luma == jpeg.LUMA_Q and chroma == jpeg.CHROMA_Q, "quality 50 is not the printed table")
check(jpeg.quant_tables(75)[0][:8] == [8, 6, 5, 8, 12, 20, 26, 31], "quality 75, first luma row")
check(jpeg.quant_tables(25)[0][:4] == [32, 22, 20, 32], "quality 25, first luma row")
check(jpeg.quant_tables(10)[1][0] == 85 and jpeg.quant_tables(10)[1][63] == 255, "quality 10 chroma, or the 255 cap")
check(jpeg.quant_tables(100) == [[1] * 64, [1] * 64], "quality 100 is not all ones")
check(set(jpeg.quant_tables(1)[0]) == {255}, "quality 1 is not all 255")
for bad in (0, 101):
    try:
        jpeg.quant_tables(bad)
        check(False, f"quality {bad} was accepted")
    except ValueError:
        pass
    except Exception as e:
        check(False, f"quality {bad} crashed with {e!r} instead of refusing")

# Zigzag, built a different way: walk the anti-diagonals, alternating direction.
walk = []
for s in range(15):
    cells = [(r, s - r) for r in range(8) if 0 <= s - r < 8]
    walk += [8 * r + c for r, c in (cells if s % 2 else cells[::-1])]
check(jpeg.ZIGZAG == walk, f"zigzag differs from the diagonal walk at {[i for i in range(64) if jpeg.ZIGZAG[i] != walk[i]][:3]}")

# Size classes and their bits, by hand (F.1.2.1): negative values are sent as v - 1.
for v, want in ((0, (0, 0)), (1, (1, 0b1)), (-1, (1, 0b0)), (5, (3, 0b101)), (-5, (3, 0b010)),
                (1023, (10, 1023)), (-2047, (11, 0))):
    check(jpeg.category(v) == want, f"category({v}) = {jpeg.category(v)}, want {want}")


def as_bits(code):
    return format(code[0], f"0{code[1]}b")


# Every run/size pair a block can need is in each AC table exactly once, plus EOB and ZRL.
wanted = sorted([0x00, 0xF0] + [(r << 4) | s for r in range(16) for s in range(1, 11)])
for name, table in (("luma", jpeg.AC_LUMA), ("chroma", jpeg.AC_CHROMA)):
    check(sorted(table[1]) == wanted and sum(table[0]) == 162, f"AC {name} table is missing or repeats a symbol")

# Codes printed in Annex K, tables K.3, K.4 and K.5, copied from the standard, not computed.
dc_l = jpeg.huffman_codes(jpeg.DC_LUMA)
check([as_bits(dc_l[s]) for s in (0, 1, 5, 6, 11)] == ["00", "010", "110", "1110", "111111110"], "DC luma codes")
dc_c = jpeg.huffman_codes(jpeg.DC_CHROMA)
check([as_bits(dc_c[s]) for s in (0, 1, 2, 3, 11)] == ["00", "01", "10", "110", "11111111110"], "DC chroma codes")
ac_l = jpeg.huffman_codes(jpeg.AC_LUMA)
check({s: as_bits(ac_l[s]) for s in (0x00, 0x01, 0x02, 0x03, 0x05, 0x11, 0xF0, 0xFA)} ==
      {0x00: "1010", 0x01: "00", 0x02: "01", 0x03: "100", 0x05: "11010", 0x11: "1100",
       0xF0: "11111111001", 0xFA: "1111111111111110"}, "AC luma codes")


def huffman_cost(counts):
    # Plain Huffman with a heap, no length limit: the total bits is the sum of every merged weight.
    heap, cost = [c for c in counts if c], 0
    heapq.heapify(heap)
    while len(heap) > 1:
        merged = heapq.heappop(heap) + heapq.heappop(heap)
        cost += merged
        heapq.heappush(heap, merged)
    return cost


def huffman_depth(counts):
    # The longest code plain Huffman gives, to show a distribution really needs the 16-bit limit.
    heap = [(c, 0) for c in counts if c]
    heapq.heapify(heap)
    while len(heap) > 1:
        (a, da), (b, db) = heapq.heappop(heap), heapq.heappop(heap)
        heapq.heappush(heap, (a + b, max(da, db) + 1))
    return heap[0][1]


def check_built(name, counts):
    # The rules of K.2, checked from BITS and the code strings, not by rebuilding. The build runs
    # in a thread with a time limit, so one that crashes or loops forever is reported, not fatal.
    result = []
    worker = threading.Thread(target=lambda: result.append(jpeg.build_table(counts + [0] * (256 - len(counts)))), daemon=True)
    worker.start()
    worker.join(10)
    if not result:
        check(False, f"{name}: build_table crashed or did not finish")
        return [0] * 16, b"", math.inf
    bits, values = result[0]
    used = [i for i, c in enumerate(counts) if c]
    check(len(bits) == 16 and sum(bits) == len(values) and sorted(values) == used, f"{name}: symbols {list(values)}, BITS {bits}")
    longest = max(i + 1 for i in range(16) if bits[i])
    # Complete except one slot: the lengths fill the code space but for one code of the longest length.
    check(sum(n << (16 - (i + 1)) for i, n in enumerate(bits)) == (1 << 16) - (1 << (16 - longest)), f"{name}: Kraft sum, BITS {bits}")
    codes = {s: as_bits(c) for s, c in jpeg.huffman_codes((bits, values)).items()}
    check(all(set(c) != {"1"} for c in codes.values()), f"{name}: an all-ones code")
    check(not any(a != b and b.startswith(a) for a in codes.values() for b in codes.values()), f"{name}: not prefix free")
    cost = sum(counts[s] * len(c) for s, c in codes.items())
    return bits, values, cost


# One table by hand. Counts 4, 2, 1 plus the dummy's 1: the dummy and C merge (2), then that and
# B (4), then that and A. So A is 0, B 10, C 110, and 111 is the dummy's, left unused.
bits, values, _ = check_built("hand table", [4, 2, 1])
check(bits == [1, 1, 1] + [0] * 13 and values == bytes([0, 1, 2]), f"hand table: BITS {bits}, values {list(values)}")
# One symbol alone still gets a one-bit code, 0, because the dummy takes 1.
bits, values, _ = check_built("one symbol", [0, 0, 0, 9])
check(bits == [1] + [0] * 15 and values == b"", f"one symbol: BITS {bits}")
# Fibonacci counts from 2, 3: with the dummy's 1, each merge swallows the last, 30 bits deep.
fib = [2, 3]
while len(fib) < 30:
    fib.append(fib[-1] + fib[-2])
check(huffman_depth(fib + [1]) > 16, "the Fibonacci counts do not need the length limit")
_, _, cost = check_built("Fibonacci counts", fib)
check(cost >= huffman_cost(fib), "Fibonacci table beats Huffman, which cannot be")
# With no limit needed, the table costs no less than Huffman and no more than Huffman with the dummy.
for n in (2, 5, 40, 162):
    counts = [rng.randrange(1, 1000) for _ in range(n)]
    _, _, cost = check_built(f"{n} random counts", counts)
    check(huffman_cost(counts) <= cost <= huffman_cost(counts + [1]), f"{n} random counts: {cost} bits is not Huffman's")

# Byte stuffing and the padding of the last byte with ones.
w = jpeg.BitWriter()
w.write(0xFF, 8)
w.write(0x01, 8)
w.write(0b101, 3)
w.flush()
check(bytes(w.out) == b"\xff\x00\x01\xbf", f"stuffing or padding: {bytes(w.out).hex()}")

# One block coded by hand: DC up by 1, then 1, -1 after one zero, 16 zeros, 1, then end of block.
zz = [11, 1, 0, -1] + [0] * 16 + [1] + [0] * 43
w = jpeg.BitWriter()
check(jpeg.encode_block(w, zz, 10, dc_l, ac_l) == 11, "encode_block does not return the DC it sent")
w.flush()
by_hand = "010" + "1" + "00" + "1" + "1100" + "0" + "11111111001" + "00" + "1" + "1010"
by_hand += "1" * (-len(by_hand) % 8)
check(bytes(w.out) == int(by_hand, 2).to_bytes(len(by_hand) // 8, "big"), f"hand-coded block: {bytes(w.out).hex()}")

# Colour: JFIF's equations by hand for white, black and red, then Pillow's own conversion.
for rgb, want in (((255, 255, 255), (255, 128, 128)), ((0, 0, 0), (0, 128, 128)),
                  ((255, 0, 0), (76.245, 84.972, 255.5))):
    got = jpeg.ycbcr(*rgb)
    check(all(abs(g - x) < 0.01 for g, x in zip(got, want)), f"ycbcr{rgb} = {got}")
sample = IMAGES["gradient 33x21"]
pillow_ycc = sample.convert("YCbCr").tobytes()
ours_ycc = [v for i in range(0, len(pillow_ycc), 3) for v in jpeg.ycbcr(*sample.tobytes()[i:i + 3])]
check(max(abs(a - b) for a, b in zip(pillow_ycc, ours_ycc)) <= 1, "ycbcr is more than 1 away from Pillow's")


def textbook(block):
    # The DCT straight from A.3.3, four nested sums, no matrices.
    c = [1 / math.sqrt(2)] + [1] * 7
    return [c[u] * c[v] / 4 * sum(block[8 * y + x] * math.cos((2 * x + 1) * u * math.pi / 16) * math.cos((2 * y + 1) * v * math.pi / 16)
                                  for y in range(8) for x in range(8)) for v in range(8) for u in range(8)]


# A flat block of 127 has DC 8 * 127 and nothing else; one horizontal cosine has only (0, 1),
# which by hand is 1/4 * 1/sqrt(2) * 100 * 8 rows * 4 = 565.685.
f = jpeg.fdct([127] * 64)
check(abs(f[0] - 1016) < 1e-9 and max(map(abs, f[1:])) < 1e-9, "flat block DCT")
f = jpeg.fdct([100 * math.cos((2 * x + 1) * math.pi / 16) for y in range(8) for x in range(8)])
check(abs(f[1] - 565.685) < 0.001 and max(abs(v) for i, v in enumerate(f) if i != 1) < 1e-9, "cosine block DCT")
for _ in range(5):
    block = [rng.uniform(-128, 127) for _ in range(64)]
    check(max(abs(a - b) for a, b in zip(jpeg.fdct(block), textbook(block))) < 1e-9, "DCT differs from the textbook formula")
# Quantizing by hand with the printed luma table: DC 800 / 16 = 50, then the two cosines,
# 565.685 / 11 = 51.4 across and 565.685 / 12 = 47.1 down, in zigzag places 1 and 2.
waves = [100 + 100 * math.cos((2 * x + 1) * math.pi / 16) + 100 * math.cos((2 * y + 1) * math.pi / 16) for y in range(8) for x in range(8)]
check(jpeg.coefficients(waves, jpeg.LUMA_Q) == [50, 51, 47] + [0] * 61, "hand-quantized block")
# Halves round away from zero, as in libjpeg: 8 * -5 / 16 is -2.5, which must become -3, not -2.
check(jpeg.coefficients([-5] * 64, [16] * 64)[:2] == [-3, 0], "-2.5 did not round to -3")


# Now whole files, each decoded by Pillow, measured against the pixels and against Pillow's
# own encoder at the same quality and subsampling.
QUALITIES = (10, 25, 50, 75, 90, 100)
rows, sizes = [], []
default_files = hashlib.sha256()
for label, image in IMAGES.items():
    pixels = image.tobytes()
    big = image.width * image.height > 10000
    for subsample in (True, False):
        scores = []
        for quality in ((25, 50, 75, 90) if big else QUALITIES):
            data = jpeg.encode(pixels, image.width, image.height, quality, subsample)
            default_files.update(data)
            where = f"{label}, q{quality}, {'4:2:0' if subsample else '4:4:4'}"
            try:
                ours = theirs_decode(data)
            except OSError as e:
                check(False, f"{where}: Pillow could not read it: {e}")
                continue
            check(ours.size == image.size and ours.mode == "RGB", f"{where}: Pillow read {ours.size} {ours.mode}")
            check(JpegImagePlugin.get_sampling(ours) == (2 if subsample else 0), f"{where}: Pillow sees the wrong sampling")
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=quality, subsampling=2 if subsample else 0)
            pil = theirs_decode(buf.getvalue())
            # Same tables as libjpeg's at every quality: checks the scaling and the DQT's zigzag order.
            check(ours.quantization == pil.quantization, f"{where}: quantization tables differ from Pillow's")
            mine, other = psnr(pixels, ours.tobytes()), psnr(pixels, pil.tobytes())
            scores.append(mine)
            if "flat" in label:
                # A flat colour is one DC value per block, so it is exact once the DC step is small enough.
                worst = max(abs(a - b) for a, b in zip(pixels, ours.tobytes()))
                check(worst <= max(abs(a - b) for a, b in zip(pixels, pil.tobytes())), f"{where}: {worst} off, more than Pillow")
                check(quality < 75 or worst == 0, f"{where}: a flat colour came back {worst} off")
            else:
                # At 100 both are within one level nearly everywhere; DOCS.md has why ours is a little further.
                check(mine >= other - (0.3 if quality < 100 else 3.5), f"{where}: PSNR {mine:.2f} dB, Pillow's {other:.2f} dB")
                check(len(data) < 1.05 * len(buf.getvalue()) + 40, f"{where}: {len(data)} bytes, Pillow's {len(buf.getvalue())}")
            rows.append((label, quality, subsample, len(data), len(buf.getvalue()), mine, other))
            # Optimized tables change only the coding, so Pillow must decode exactly the same pixels.
            try:
                tuned = jpeg.encode(pixels, image.width, image.height, quality, subsample, optimize=True)
                same = theirs_decode(tuned).tobytes() == ours.tobytes()
            except Exception as e:
                check(False, f"{where}, optimized: {e!r}")
                continue
            check(same, f"{where}: optimized tables decode to different pixels")
            check(len(tuned) < len(data), f"{where}: optimized {len(tuned)} bytes, standard {len(data)}")
            buf = io.BytesIO()
            image.save(buf, "JPEG", quality=quality, subsampling=2 if subsample else 0, optimize=True)
            check(len(tuned) <= len(buf.getvalue()) + 40, f"{where}: optimized {len(tuned)} bytes, Pillow's {len(buf.getvalue())}")
            sizes.append((label, quality, subsample, len(data), len(tuned), len(buf.getvalue())))
        # Not for one-coefficient pictures: their error depends on where they fall between steps.
        if "flat" not in label and "worst" not in label:
            check(all(a < b for a, b in zip(scores, scores[1:])), f"{label}: PSNR does not rise with quality: {scores}")

# Without --optimize every file is byte for byte what the encoder wrote before the option existed.
check(default_files.hexdigest() == "93ed0e5b179ad8802d5d0a028e01bdfddfc48a8081c20b96ad3baad0cb035715",
      "default output changed")
print(f"{'image':26} {'q':>3} {'sub':5} {'ours':>7} {'Pillow':>7} {'ours dB':>8} {'Pillow dB':>9}")
for label, quality, subsample, mine, other, p1, p2 in rows:
    print(f"{label:26} {quality:>3} {'4:2:0' if subsample else '4:4:4':5} {mine:>7} {other:>7} {p1:>8.2f} {p2:>9.2f}")
print(f"{'image':26} {'q':>3} {'sub':5} {'standard':>8} {'optimized':>9} {'Pillow optimized':>16}")
for label, quality, subsample, standard, tuned, pil in sizes:
    print(f"{label:26} {quality:>3} {'4:2:0' if subsample else '4:4:4':5} {standard:>8} {tuned:>9} {pil:>16}")

# Bad input is refused, not encoded into a broken file.
for args in ((b"\0" * 5, 1, 2), (b"", 0, 0), (b"\0" * 3, 1, 1, 0)):
    try:
        jpeg.encode(*args)
        check(False, f"encode accepted {args[1:]}")
    except ValueError:
        pass
    except Exception as e:
        check(False, f"encode{args[1:]} crashed with {e!r} instead of refusing")

# The command line: -q and --444 must reach the file, as Pillow reads it.
IMAGES["noise 17x9"].save("t-in.png")
try:
    run = subprocess.run([sys.executable, jpeg.__file__, "t-in.png", "-o", "t-out.jpg", "-q", "40", "--444"],
                         capture_output=True, text=True)
    check(run.returncode == 0 and "quality 40" in run.stdout, f"command line: {run}")
    written = theirs_decode(open("t-out.jpg", "rb").read())
    check(written.size == (17, 9) and JpegImagePlugin.get_sampling(written) == 0, "command line ignored --444")
    check(list(written.quantization[0]) == jpeg.quant_tables(40)[0], "command line ignored -q")
    run = subprocess.run([sys.executable, jpeg.__file__, "t-in.png", "-o", "t-opt.jpg", "-q", "40", "--444", "--optimize"],
                         capture_output=True, text=True)
    check(run.returncode == 0 and "optimized" in run.stdout, f"command line --optimize: {run}")
    if os.path.exists("t-opt.jpg"):
        tuned = open("t-opt.jpg", "rb").read()
        check(len(tuned) < os.path.getsize("t-out.jpg") and theirs_decode(tuned).tobytes() == written.tobytes(),
              "command line ignored --optimize")
    run = subprocess.run([sys.executable, jpeg.__file__, "t-in.png"], capture_output=True, text=True)
    check(run.returncode == 0 and os.path.exists("t-in.jpg"), "default output name")
    if os.path.exists("t-in.jpg"):
        check(JpegImagePlugin.get_sampling(theirs_decode(open("t-in.jpg", "rb").read())) == 2, "default is not 4:2:0")
finally:
    for name in ("t-in.png", "t-out.jpg", "t-in.jpg", "t-opt.jpg"):
        if os.path.exists(name):
            os.remove(name)
print("the command line writes what it is told to")

# Reading. decode() is tested on files Pillow (libjpeg) wrote, and its pixels are compared with
# Pillow's own decoding of the same file. They differ by a few levels; DOCS.md has why.
def pillow_jpeg(image, **options):
    buf = io.BytesIO()
    image.save(buf, "JPEG", **options)
    return buf.getvalue()


def our_decode(data, where):
    try:
        return jpeg.decode(data)
    except Exception as e:
        check(False, f"{where}: decode raised {e!r}")


readings = []


def against_pillow(data, where, worst=3):
    # Per channel, the largest difference from Pillow's decoding, and the PSNR between the two.
    # The floors sit about a decibel under the lowest I measured; upsampling costs the most.
    floor = 51 if "4:2:" in where or "4:4:0" in where else 57
    got = our_decode(data, where)
    if got is None:
        return
    theirs = theirs_decode(data)
    width, height, rgb = got
    check((width, height) == theirs.size, f"{where}: size {width}x{height}, Pillow {theirs.size}")
    other = theirs.convert("RGB").tobytes()
    if len(rgb) != len(other):
        check(False, f"{where}: {len(rgb)} bytes of pixels, Pillow {len(other)}")
        return
    most = [max(abs(a - b) for a, b in zip(rgb[c::3], other[c::3])) for c in range(3)]
    score = psnr(rgb, other)
    check(max(most) <= worst and score >= floor, f"{where}: off from Pillow by {most}, {score:.2f} dB")
    readings.append((where, most, score))


SAMPLINGS = {0: "4:4:4", 1: "4:2:2", 2: "4:2:0"}
for label, image in IMAGES.items():
    big = image.width * image.height > 10000
    for sub, name in SAMPLINGS.items():
        for quality in ((75,) if big else (10, 50, 90, 100)):
            against_pillow(pillow_jpeg(image, quality=quality, subsampling=sub), f"{label}, q{quality}, {name}")
    grey = image.convert("L")
    # Greyscale has no colour step and no upsampling, so only the IDCTs differ: within 1.
    for quality in ((75,) if big else (10, 90)):
        against_pillow(pillow_jpeg(grey, quality=quality), f"{label}, q{quality}, greyscale", worst=1)
    against_pillow(pillow_jpeg(image, quality=75, optimize=True), f"{label}, q75, 4:2:0, optimize")
# Pillow's keep_rgb writes R, G and B themselves, marked by an Adobe segment, with no colour step.
for label in ("gradient 33x21", "colour stripes 48x32"):
    against_pillow(pillow_jpeg(IMAGES[label], quality=75, keep_rgb=True), f"{label}, q75, 4:4:4, keep_rgb")
# A flat colour at 4:4:4 is one DC value per block, so only rounding can tell the two apart.
for quality in (50, 90, 100):
    against_pillow(pillow_jpeg(IMAGES["flat colour 40x24"], quality=quality, subsampling=0), f"flat colour, q{quality}, 4:4:4, exact", worst=0)
    against_pillow(pillow_jpeg(make(24, 16, lambda x, y: (90, 90, 90)), quality=quality, subsampling=0), f"flat grey, q{quality}, 4:4:4, exact", worst=0)

# Restart markers: DC prediction starts again at each, and Pillow can put one every few MCUs.
for label in ("gradient 33x21", "colour stripes 48x32", "trace-rays render 640x360"):
    if label not in IMAGES:
        continue
    for sub, options in ((2, dict(restart_marker_blocks=1)), (0, dict(restart_marker_blocks=3)), (1, dict(restart_marker_rows=1))):
        data = pillow_jpeg(IMAGES[label], quality=75, subsampling=sub, **options)
        check(b"\xff\xdd" in data and b"\xff\xd1" in data, f"{label}: Pillow wrote no restart markers for {options}")
        against_pillow(data, f"{label}, q75, {SAMPLINGS[sub]}, {', '.join(f'{k}={v}' for k, v in options.items())}")
# Our own encoder's files as well, though never only them.
for label in ("gradient 33x21", "noise 17x9"):
    image = IMAGES[label]
    for subsample in (True, False):
        against_pillow(jpeg.encode(image.tobytes(), image.width, image.height, 75, subsample), f"{label}, ours, q75, {'4:2:0' if subsample else '4:4:4'}")

print(f"{'decoded against Pillow':64} {'max R, G, B':>12} {'dB':>7}")
for where, most, score in readings:
    if "render" in where or "keep_rgb" in where:
        print(f"{where:64} {str(most):>12} {score:>7.2f}")
for kind in ("4:4:4", "4:2:2", "4:2:0", "greyscale"):
    group = [(m, sc) for w, m, sc in readings if kind in w]
    print(f"{kind}: {len(group)} files, largest difference {max(max(m) for m, _ in group)}, lowest {min(sc for _, sc in group):.2f} dB")
print(f"{len(readings)} files, largest difference {max(max(m) for _, m, _ in readings)}, lowest {min(s for *_, s in readings):.2f} dB")


def refused(data, why, words=""):
    # Broken input must raise ValueError with a reason, never anything else and never decode.
    try:
        jpeg.decode(data)
        check(False, f"{why}: decoded without complaint")
    except ValueError as e:
        check(words.lower() in str(e).lower(), f"{why}: refused, but with {str(e)!r}")
    except Exception as e:
        check(False, f"{why}: raised {e!r} instead of ValueError")


def segments(data):
    # Offsets of the first of each marker up to SOS, so a test can patch one segment.
    pos, found = 2, {}
    while data[pos + 1] != 0xDA:
        found.setdefault(data[pos + 1], pos)
        pos += 2 + int.from_bytes(data[pos + 2:pos + 4], "big")
    found[0xDA] = pos
    return found


small = pillow_jpeg(IMAGES["noise 17x9"], quality=75)
at = segments(small)
# A lone component is never interleaved, so marking it 2x2 must change nothing; libjpeg agrees.
grey = pillow_jpeg(IMAGES["gradient 33x21"].convert("L"), quality=75)
patched = bytearray(grey)
patched[segments(grey)[0xC0] + 11] = 0x22
against_pillow(bytes(patched), "gradient 33x21, greyscale marked 2x2 sampling", worst=1)
check(our_decode(bytes(patched), "greyscale marked 2x2") == our_decode(grey, "greyscale"), "a lone component's sampling changed the pixels")
sos = at[0xDA]
scan_start = sos + 2 + int.from_bytes(small[sos + 2:sos + 4], "big")
refused(small[:len(small) // 2], "truncated file", "end")
refused(small[2:], "missing SOI", "SOI")
refused(small[:2] + b"\xff\xf7\x00\x02" + small[2:], "unknown marker 0xFFF7", "0xFFF7")
refused(small[:sos] + b"\xff\xc4\x00\x16\x00" + bytes([3] + [0] * 15) + b"\x00\x01\x02" + small[sos:], "DHT with 3 one-bit codes", "more codes")
refused(small[:sos] + b"\xff\xc4\x01\x14\x00" + bytes([0] * 14 + [2, 255]) + bytes(257) + small[sos:], "DHT with 257 codes", "256")
patched = bytearray(small)
patched[sos + 6] = 0x33
refused(bytes(patched), "scan uses Huffman tables 3, never defined", "never defined")
patched = bytearray(small)
patched[at[0xC0] + 12] = 3
refused(bytes(patched), "frame uses quantization table 3, never defined", "never defined")
refused(pillow_jpeg(IMAGES["noise 17x9"], quality=75, streamtype=2), "Pillow's abbreviated file, no tables", "never defined")
# Ones from the first scan byte on: longer than any DC luma code in Annex K, so it matches nothing.
refused(small[:scan_start] + b"\xff\x00" * 4 + small[scan_start + 8:], "a code that matches nothing", "matches nothing")
patched = bytearray(small)
patched[at[0xC0] + 1] = 0xC1
check(our_decode(bytes(patched), "SOF1") == our_decode(small, "SOF0"), "an 8-bit SOF1 file decoded differently from the same file as SOF0")
for marker, words in ((0xC9, "arithmetic"), (0xC3, "lossless"), (0xC2, "progressive")):
    patched = bytearray(small)
    patched[at[0xC0] + 1] = marker
    refused(bytes(patched), f"SOF0 marked 0xFF{marker:02X}", words)
patched = bytearray(small)
patched[at[0xC0] + 4] = 12
refused(bytes(patched), "12-bit precision", "12-bit")
refused(small.replace(b"\xff\xd9", b""), "no EOI", "ends")
restarts = pillow_jpeg(IMAGES["gradient 33x21"], quality=75, restart_marker_blocks=1)
i = restarts.index(b"\xff\xd1")
refused(restarts[:i] + b"\xff\xd2" + restarts[i + 2:], "restart markers out of order", "restart")
# Any marker may have extra 0xFF fill bytes in front (B.1.1.2), a restart marker too.
check(our_decode(restarts.replace(b"\xff\xd1", b"\xff\xff\xff\xd1"), "fill bytes") == our_decode(restarts, "restarts"), "fill bytes before RST1 changed the pixels")
for n in range(len(small)):
    refused(small[:n], f"cut to {n} of {len(small)} bytes")
print(f"every one of the {len(small)} truncations of a {len(small)}-byte file is refused")
# Cut inside the coded data but with EOI put back, so only running out of bits can tell.
for n in range(scan_start, len(small) - 2):
    refused(small[:n] + b"\xff\xd9", f"scan cut to {n - scan_start} bytes, EOI kept", "ends in the middle")


def tiny(dc, ac, scan):
    # An 8x8 greyscale JPEG by hand: every divisor 1, and symbol k's code is k ones then a zero.
    hand = lambda tc, symbols: bytes([tc << 4] + [1] * len(symbols) + [0] * (16 - len(symbols)) + symbols)
    return b"".join([b"\xff\xd8", jpeg.segment(0xFFDB, b"\x00" + b"\x01" * 64),
                     jpeg.segment(0xFFC0, bytes([8, 0, 8, 0, 8, 1, 1, 0x11, 0])),
                     jpeg.segment(0xFFC4, hand(0, dc) + hand(1, ac)),
                     jpeg.segment(0xFFDA, bytes([1, 1, 0x00, 0, 63, 0])), scan, b"\xff\xd9"])


# DC size 3, bits 101 (5), then EOB, padded with ones: 0 101 0 111. DC 5 is 5 / 8 over 128, so 129.
by_hand = tiny([3], [0x00], b"\x57")
check(our_decode(by_hand, "hand-built 8x8") == (8, 8, b"\x81" * 192) and theirs_decode(by_hand).tobytes() == b"\x81" * 64, "hand-built 8x8 is not 129 everywhere")
# A DC difference of size 12 and an AC value of size 11 do not fit 8-bit samples.
refused(tiny([12], [0x00], b"\x40\x03"), "DC difference of size 12", "bigger")
refused(tiny([0], [0x0B, 0x00], b"\x20\x05"), "AC value of size 11", "bigger")
fuzz, decoded = random.Random(11), 0
for n in range(300):
    target = restarts if n % 2 else small
    broken = bytearray(target)
    for _ in range(fuzz.choice((1, 1, 2, 4))):
        broken[fuzz.randrange(2, len(broken))] ^= 1 << fuzz.randrange(8)
    try:
        width, height, rgb = jpeg.decode(bytes(broken))
        check(len(rgb) == 3 * width * height, f"flip {n}: {width}x{height} but {len(rgb)} bytes")
        decoded += 1
    except ValueError:
        pass
    except Exception as e:
        check(False, f"flip {n}: raised {e!r}")
print(f"300 bit-flipped files: {decoded} decoded to something, the rest refused with ValueError")

# The command line: -d reads a JPEG and writes the PNG Pillow then reads back.
open("t-in.jpg", "wb").write(small)
try:
    run = subprocess.run([sys.executable, jpeg.__file__, "-d", "t-in.jpg", "-o", "t-out.png"], capture_output=True, text=True)
    check(run.returncode == 0 and "17x9" in run.stdout, f"command line -d: {run}")
    if os.path.exists("t-out.png"):
        check(Image.open("t-out.png").convert("RGB").tobytes() == jpeg.decode(small)[2], "command line -d wrote other pixels")
    open("t-bad.jpg", "wb").write(small[:100])
    run = subprocess.run([sys.executable, jpeg.__file__, "-d", "t-bad.jpg"], capture_output=True, text=True)
    check(run.returncode != 0 and "end of the file" in run.stderr and "Traceback" not in run.stderr, f"command line -d on a broken file: {run}")
finally:
    for name in ("t-in.jpg", "t-out.png", "t-bad.jpg", "t-bad.png"):
        if os.path.exists(name):
            os.remove(name)
print("the decoder reads what Pillow writes")

# Greyscale: one component, which Pillow must read as an L picture of the right size, about as close
# to the brightness of the original as Pillow's own greyscale file is, and no bigger.
render = Image.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "trace-rays", "render.png")).convert("RGB")
grey_src = render.convert("L").tobytes()
for opt in (False, True):
    ours = jpeg.encode(render.tobytes(), render.width, render.height, 75, optimize=opt, grey=True)
    back = Image.open(io.BytesIO(ours))
    back.load()
    buf = io.BytesIO()
    render.convert("L").save(buf, "JPEG", quality=75, optimize=opt)
    theirs = Image.open(io.BytesIO(buf.getvalue()))
    check(back.mode == "L" and back.size == render.size, f"grey (optimize {opt}): Pillow read {back.mode} {back.size}")
    close = psnr(back.tobytes(), grey_src) >= psnr(theirs.convert("L").tobytes(), grey_src) - 0.3
    check(close and len(ours) <= len(buf.getvalue()),
          f"grey (optimize {opt}): {len(ours)} bytes at {psnr(back.tobytes(), grey_src):.2f} dB against Pillow's {len(buf.getvalue())}")
print("greyscale files are read by Pillow as one channel, as small as Pillow's own")

# Restart markers change how the bits are framed, never the pixels: Pillow and our decoder must
# read exactly what they read without them, and RST0 to RST7 must come round in order.
image = IMAGES["gradient 33x21"]
for opts in ({}, {"subsample": False}, {"grey": True}, {"optimize": True}):
    plain = jpeg.encode(image.tobytes(), image.width, image.height, **opts)
    for every in (1, 2, 5):
        marked = jpeg.encode(image.tobytes(), image.width, image.height, restart=every, **opts)
        found = [marked[i + 1] - 0xD0 for i in range(len(marked) - 1) if marked[i] == 0xFF and 0xD0 <= marked[i + 1] <= 0xD7]
        side = 16 if opts.get("subsample", True) and not opts.get("grey") else 8
        mcus = -(-image.width // side) * -(-image.height // side)
        check(found == [i % 8 for i in range((mcus - 1) // every)], f"restart {every} {opts}: markers {found}")
        check(Image.open(io.BytesIO(marked)).tobytes() == Image.open(io.BytesIO(plain)).tobytes(), f"restart {every} {opts}: Pillow read other pixels")
        check(our_decode(marked, "restart") == our_decode(plain, "plain"), f"restart {every} {opts}: our decoder read other pixels")
print("restart markers come in order and leave every pixel as it was")

# Progressive and multi-scan files. jpegtran rewrites the scans without touching a coefficient, so
# each file in scans/ must decode to exactly the pixels of the baseline file it was made from.
SCANS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scans")
for name in sorted(os.listdir(SCANS)):
    if name.endswith(".jpg") and "_" in name:
        source = name.split("_")[0] + ".jpg"
        ours = our_decode(open(os.path.join(SCANS, name), "rb").read(), name)
        check(ours is not None and ours == our_decode(open(os.path.join(SCANS, source), "rb").read(), source),
              f"{name} decoded to other pixels than {source}")
# Pillow's progressive files are separate encodes, so they are held to libjpeg's reading instead.
for label in ("gradient 33x21", "noise 17x9", "flat colour 40x24"):
    for sub, kind in ((0, "4:4:4"), (1, "4:2:2"), (2, "4:2:0")):
        against_pillow(pillow_jpeg(IMAGES[label], quality=75, subsampling=sub, progressive=True), f"{label}, q75, {kind}, progressive")
    against_pillow(pillow_jpeg(IMAGES[label].convert("L"), quality=75, progressive=True), f"{label}, q75, greyscale, progressive", worst=1)
# 4:4:0 (luma 1x2): Pillow cannot write it, so this is a 4:2:2 file turned with jpegtran -transpose.
against_pillow(open(os.path.join(SCANS, "440.jpg"), "rb").read(), "jpegtran's 29x37, 4:4:0")
against_pillow(pillow_jpeg(IMAGES["gradient 33x21"], quality=90, progressive=True, optimize=True, restart_marker_blocks=2),
               "gradient 33x21, q90, 4:2:0, progressive, optimized, restarts")
print("progressive and per-component files decode exactly as their baseline sources, and as libjpeg reads Pillow's")


def scans_in(data):
    # (start of each SOS segment, start of the marker after its coded data).
    found, pos = [], 2
    while data[pos + 1] != 0xD9:
        n = int.from_bytes(data[pos + 2:pos + 4], "big")
        if data[pos + 1] != 0xDA:
            pos += 2 + n
            continue
        end = pos + 2 + n
        while not (data[end] == 0xFF and data[end + 1] not in (0, *range(0xD0, 0xD8))):
            end += 1
        found.append((pos, end))
        pos = end
    return found


prog = open(os.path.join(SCANS, "420_successive.jpg"), "rb").read()
sos = scans_in(prog)[0][0]
header_end = sos + 2 + int.from_bytes(prog[sos + 2:sos + 4], "big")
patched = bytearray(prog)
patched[header_end - 2] = 5  # the DC scan's Se: a band from 0 to 5 mixes DC and AC
refused(bytes(patched), "a progressive scan from 0 to 5", "not valid")
patched[header_end - 2], patched[header_end - 1] = 0, 0x0E
refused(bytes(patched), "a progressive scan with Al = 14", "not valid")
split = open(os.path.join(SCANS, "420_per_component.jpg"), "rb").read()
spans = scans_in(split)
refused(split[:spans[-1][0]] + b"\xff\xd9", "a per-component file missing its last scan", "in no scan")
refused(split[:spans[0][1]] + split[spans[0][0]:], "a baseline component scanned twice", "two baseline scans")
# One end-of-band symbol can stand for 32,767 blocks, so blocks are only made when a scan reaches
# them. A header grown to 60000x60000 runs out of bits in the DC scan instead of asking for memory.
plain = open(os.path.join(SCANS, "420_progressive.jpg"), "rb").read()
sof = plain.index(b"\xff\xc2")
refused(plain[:sof + 5] + b"\xea\x60\xea\x60" + plain[sof + 9:], "a progressive header claiming 60000x60000")
# A flat picture's AC scans are all end-of-band runs, so without its DC scans (Ss = 0, the byte
# three from the end of the SOS header) no scan reaches any block.
flat = pillow_jpeg(IMAGES["flat colour 40x24"], quality=75, progressive=True)
spans = scans_in(flat)
ss_of = lambda a: flat[a + 2 + int.from_bytes(flat[a + 2:a + 4], "big") - 3]
ac_only = flat
for a, b in reversed(spans):
    if not ss_of(a):
        ac_only = ac_only[:a] + ac_only[b:]
refused(ac_only, "a progressive file without its DC scans", "never reach")
for n in range(0, len(prog), 7):
    refused(prog[:n], f"progressive file cut to {n} of {len(prog)} bytes")
fuzz, decoded = random.Random(12), 0
for n in range(200):
    broken = bytearray(prog)
    for _ in range(fuzz.choice((1, 2, 4))):
        broken[fuzz.randrange(2, len(broken))] ^= 1 << fuzz.randrange(8)
    try:
        width, height, rgb = jpeg.decode(bytes(broken))
        check(len(rgb) == 3 * width * height, f"progressive flip {n}: {width}x{height} but {len(rgb)} bytes")
        decoded += 1
    except ValueError:
        pass
    except Exception as e:
        check(False, f"progressive flip {n}: raised {e!r}")
print(f"broken progressive scans refused; 200 bit-flipped files: {decoded} decoded, the rest refused with ValueError")

# The progressive encoder sends the very same coefficients in several scans, so Pillow (libjpeg)
# and our decoder must both read it as exactly the pixels of the baseline file, at every layout.
sizes = []
for label, image in IMAGES.items():
    for opts in ({}, {"subsample": False}, {"grey": True}):
        args = (image.tobytes(), image.width, image.height, 75)
        base, prog = jpeg.encode(*args, optimize=True, **opts), jpeg.encode(*args, progressive=True, **opts)
        check(b"\xff\xc2" in prog, f"{label} {opts}: no SOF2 marker in the progressive file")
        check(theirs_decode(prog).tobytes() == theirs_decode(base).tobytes(), f"{label} {opts}: Pillow read our progressive file differently")
        check(our_decode(prog, label) == our_decode(base, label), f"{label} {opts}: our decoder read our progressive file differently")
        sizes.append((label, opts, len(base), len(prog)))
try:
    jpeg.encode(b"\x00" * 3, 1, 1, progressive=True, restart=1)
    check(False, "progressive with restart markers was accepted")
except ValueError:
    pass
render = [(b, p) for label, opts, b, p in sizes if "render" in label and not opts]
print("progressive files read by Pillow and by us as exactly the baseline pixels;",
      f"the render: {render[0][0]} bytes optimized baseline, {render[0][1]} progressive" if render else "")

print("FAILURES" if failures else "ok")
sys.exit(1 if failures else 0)
