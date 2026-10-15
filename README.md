# jpeg

A baseline JPEG encoder written from the bits up, in one Python file with no
dependencies. It writes real `.jpg` files that any viewer opens.

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
- The file around it: SOI, APP0 (JFIF), DQT, SOF0, DHT, SOS, EOI.

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

It is slow, being pure Python: 1.9 seconds for the render at 4:2:0 and 3.2 at
4:4:4, median of five in one process.

## Running it

    python jpeg.py IN.png [-o OUT.jpg] [-q 75] [--444]
    python test_jpeg.py

Pillow reads the PNG on the command line. Everything after the pixels, the
whole JPEG, is this file and the standard library.

## Notes

[DOCS.md](DOCS.md) is what writing it taught me, including why a flat colour
can come back worse at quality 50 than at 25, and the 32 ways I broke the code
on purpose to see whether the tests noticed.
