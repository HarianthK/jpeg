# jpeg

A baseline JPEG encoder and decoder written from the bits up, in one Python
file with no dependencies. It writes real `.jpg` files that any viewer opens,
and reads baseline files other programs wrote.

    $ python jpeg.py ../trace-rays/render.png -o render.jpg
    ../trace-rays/render.png: 640x360, 691200 bytes of pixels -> 18254 bytes (37.9 to 1), quality 75, 4:2:0

JPEG throws away what the eye is bad at seeing and then packs what is left.
The colour goes to a brightness channel and two colour channels, and the colour
channels are kept at half size. Each 8x8 square becomes 64 cosine amplitudes,
most of the small ones are rounded to zero, and the rest are run-length and
Huffman coded.

## What it does

- RGB to YCbCr with the JFIF equations.
- 4:2:0 chroma subsampling, each colour sample the mean of a 2x2 square, or
  `--444` to keep full colour.
- Pads the picture to whole 8x8 blocks (16x16 with 4:2:0) by repeating the last
  row and column, so odd sizes like 17x9 work.
- The 2-D DCT, as two passes of an 8x8 matrix.
- Quantization with the Annex K tables, scaled by a quality from 1 to 100 with
  the same formula libjpeg uses, so quality 75 here means quality 75 there.
- Zigzag order, DC as differences, AC as (zeros before, size) pairs with
  end-of-block and sixteen-zero codes.
- The standard Annex K Huffman tables, and a 0x00 after every 0xFF in the data.
- `--optimize` (or `optimize=True`), like `jpegtran -optimize`: a first pass
  counts the symbols this picture uses, Huffman tables are built for them by
  the procedure in Annex K.2 (codes at most 16 bits, none all ones), and a
  second pass writes the data with them. Without it the output is byte for
  byte what it was before the option existed.
- `--restart N` (or `restart=N`): a DRI segment, and every N MCUs the byte is
  ended with RST0 to RST7 in turn and DC starts again from zero, so a damaged
  byte spoils only the stretch up to the next marker. The pixels do not change.
- The file around it: SOI, APP0 (JFIF), DQT, SOF0, DHT, DRI when asked, SOS, EOI.

Reading, with `decode(data)` or `-d`:

- Baseline sequential files (SOF0), 8-bit, greyscale or YCbCr at 4:4:4, 4:2:2
  or 4:2:0, and RGB files marked by an Adobe segment.
- DQT, DHT, SOS, DRI and restart markers, byte stuffing and fill bytes; APPn
  and COM segments are skipped.
- Huffman decoding through a 16-bit lookup table, zigzag undone, the inverse
  DCT, libjpeg's "fancy" chroma upsampling (3/4 of the nearer sample, 1/4 of
  the next), and the JFIF equations back to RGB, clamped to 0..255.
- Everything in the file is checked before it is used. A broken file, or a
  progressive, arithmetic coded, lossless or 12-bit one, raises `ValueError`
  with the reason.

## How it compares

`python test_jpeg.py` encodes eight pictures at several qualities, has Pillow
decode every file, and measures how close the pixels came back (PSNR, higher is
better). Beside it, Pillow's own encoder (libjpeg) at the same settings:

| picture | quality | ours | Pillow | ours dB | Pillow dB |
| --- | --- | --- | --- | --- | --- |
| ray-traced render 640x360, 4:2:0 | 25 | 9,187 | 9,178 | 35.74 | 35.71 |
| | 50 | 12,929 | 12,903 | 38.43 | 38.43 |
| | 75 | 18,254 | 18,286 | 40.79 | 40.71 |
| | 90 | 29,940 | 30,179 | 44.05 | 43.98 |
| ray-traced render 640x360, 4:4:4 | 75 | 21,669 | 21,876 | 41.35 | 41.24 |
| text-like 64x64, 4:2:0 | 75 | 2,370 | 2,382 | 33.31 | 33.39 |
| colour stripes 48x32, 4:4:4 | 75 | 1,399 | 1,410 | 32.52 | 32.37 |
| noise 17x9, 4:4:4 | 75 | 880 | 889 | 22.96 | 23.14 |

From quality 25 to 90, never more than 0.18 dB behind libjpeg and never more
than 3% bigger, on every picture in the test. Both use the same tables, which
Pillow confirms by reading them out of our files. Sixteen bytes of every file
are headers only: Pillow writes six table segments where this writes two. At
quality 100, ours is a little less exact on hard black and white edges; DOCS.md
has the numbers.

With `--optimize`, against the standard tables and against Pillow's own
`optimize=True` at the same quality and subsampling (whole files, in bytes):

| picture | quality | standard | optimized | Pillow optimized |
| --- | --- | --- | --- | --- |
| ray-traced render 640x360, 4:2:0 | 25 | 9,187 | 7,483 | 7,459 |
| | 50 | 12,929 | 11,587 | 11,561 |
| | 75 | 18,254 | 17,350 | 17,409 |
| | 90 | 29,940 | 29,141 | 29,453 |
| ray-traced render 640x360, 4:4:4 | 75 | 21,669 | 19,779 | 19,993 |
| text-like 64x64, 4:2:0 | 75 | 2,370 | 1,931 | 1,945 |
| colour stripes 48x32, 4:4:4 | 75 | 1,399 | 771 | 793 |
| noise 17x9, 4:4:4 | 75 | 880 | 549 | 567 |

On the render that is 5% smaller at quality 75 and 19% at quality 25. Every
optimized file is decoded by Pillow to exactly the same pixels as the
standard one, because only the coding changed. The pass costs no measurable
time: the render took a median 2.11 seconds standard and 1.97 optimized,
alternating five of each in one process, with more spread between runs (1.86
to 2.81) than between the two.

It is slow, being pure Python: 1.9 seconds for the render at 4:2:0 and 3.2 at
4:4:4, median of five in one process.

### Reading

The decoder is tested on files Pillow wrote (and a few of this encoder's, but
never only those), and its pixels are compared with Pillow's own decoding of
each. They are never more than 3 levels
apart, because libjpeg rounds its inverse DCT, upsampling and colour
conversion in integers. On the render at quality 75:

| Pillow's file | values equal to Pillow's | largest difference | PSNR between the two |
| --- | --- | --- | --- |
| greyscale | 99.38% | 1 | 70.24 dB |
| 4:4:4 | 99.11% | 3 | 67.49 dB |
| 4:2:2 | 91.50% | 3 | 58.69 dB |
| 4:2:0 | 86.71% | 3 | 56.77 dB |

Over all 131 files in the test (127 of them Pillow's, 4 this encoder's) the
lowest is 51.90 dB, and a flat colour at 4:4:4 comes out exactly equal. Restart markers, `optimize=True`, odd sizes and
`keep_rgb=True` are among them. Decoding the render takes 1.27 seconds at
4:2:0, against about 1.2 milliseconds for Pillow.

## Running it

    python jpeg.py IN.png [-o OUT.jpg] [-q 75] [--444] [--grey] [--optimize] [--restart N]
    python jpeg.py -d IN.jpg [-o OUT.png]
    python test_jpeg.py

`--optimize` builds Huffman tables for the picture instead of using the
standard ones: smaller files, the same pixels. `-d` decodes a JPEG and writes
a PNG. Pillow reads or writes the PNG on the command line; the whole JPEG, both
ways, is this file and the standard library. `python test_jpeg.py` took about a
minute here (60 to 63 seconds).

## Notes

[DOCS.md](DOCS.md) is what writing it taught me, including why a flat colour
can come back worse at quality 50 than at 25, why the decoder's pixels differ
from Pillow's, and the 75 ways I broke the code on purpose to see whether the
tests noticed.
