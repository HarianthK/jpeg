# Run: python test_jpeg.py. Every file we write is decoded by Pillow, never by this project.
import io
import math
import os
import random
import subprocess
import sys

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
rows = []
for label, image in IMAGES.items():
    pixels = image.tobytes()
    big = image.width * image.height > 10000
    for subsample in (True, False):
        scores = []
        for quality in ((25, 50, 75, 90) if big else QUALITIES):
            data = jpeg.encode(pixels, image.width, image.height, quality, subsample)
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
        # Not for one-coefficient pictures: their error depends on where they fall between steps.
        if "flat" not in label and "worst" not in label:
            check(all(a < b for a, b in zip(scores, scores[1:])), f"{label}: PSNR does not rise with quality: {scores}")

print(f"{'image':26} {'q':>3} {'sub':5} {'ours':>7} {'Pillow':>7} {'ours dB':>8} {'Pillow dB':>9}")
for label, quality, subsample, mine, other, p1, p2 in rows:
    print(f"{label:26} {quality:>3} {'4:2:0' if subsample else '4:4:4':5} {mine:>7} {other:>7} {p1:>8.2f} {p2:>9.2f}")

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
    run = subprocess.run([sys.executable, jpeg.__file__, "t-in.png"], capture_output=True, text=True)
    check(run.returncode == 0 and os.path.exists("t-in.jpg"), "default output name")
    if os.path.exists("t-in.jpg"):
        check(JpegImagePlugin.get_sampling(theirs_decode(open("t-in.jpg", "rb").read())) == 2, "default is not 4:2:0")
finally:
    for name in ("t-in.png", "t-out.jpg", "t-in.jpg"):
        if os.path.exists(name):
            os.remove(name)
print("the command line writes what it is told to")

print("FAILURES" if failures else "ok")
sys.exit(1 if failures else 0)
