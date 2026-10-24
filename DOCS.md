# What I learned writing a JPEG encoder and decoder

## Colour is split before anything is thrown away

The eye sees fine detail in brightness and only coarse detail in colour. So the
first step moves each pixel from RGB to YCbCr: Y is brightness, a weighted sum
that leans on green, and Cb and Cr are how far blue and red stand from it. JFIF
fixes the equations and keeps all three at full range, 0 to 255. White is
(255, 128, 128) and pure red is (76.2, 85.0, 255.5); the test checks those by
hand, and checks the whole conversion against Pillow's own to within one level.

Then 4:2:0 keeps one Cb and one Cr for every 2x2 square of pixels, the mean of
the four. That is half the data gone before the DCT has started, and on a
photograph it costs little. On the ray-traced render at quality 75, 4:2:0 is
18,254 bytes against 21,670 for full colour, for 0.56 dB less PSNR. On a
picture built to defeat it, saturated stripes three pixels wide, it is a
disaster: 13.9 dB against 32.5, and it stays at 14 dB even at quality 100,
because no quantization setting can bring back colour that was averaged away.

With 4:2:0 the unit of the file is a 16x16 square (an MCU): four luma blocks,
left to right and top to bottom, then one Cb block and one Cr block. The
picture is padded out to whole MCUs by repeating the last row and column. The
padding is never shown, but it is coded: repeating the edge makes the padded
block smooth, where filling it with something else makes a sharp step that
costs bits and rings back into the real pixels.

## Why 8x8 and the DCT

The DCT turns 64 pixel values into 64 amplitudes of 2-D cosine patterns, from
the flat average (DC) up to a fine checkerboard. It loses nothing by itself: it
is a rotation, and with floats it runs backwards exactly. What it buys is that
on real pictures nearly all the energy lands in a few low-frequency numbers, and
the high ones are small. A flat block of 127 becomes DC 1016 and 63 zeros. One
horizontal cosine of amplitude 100 becomes a single number, 565.685, which is
1/4 times 1/sqrt(2) times 100 times 8 rows times 4 (the sum of cos squared over
8 samples); the test has both, worked by hand.

The 8x8 size is a compromise. Bigger blocks compact better but cost more and
smear any edge across more pixels; smaller ones compact too little. The edges
between blocks are where JPEG's blockiness comes from at low quality, because
each block is rounded on its own.

The formula has four nested sums, 64 multiplies per output and 4,096 per block.
It is separable, so this does rows then columns, 1,024 multiplies. The test
keeps the four-sum textbook formula as a separate function and checks the fast
one against it on random blocks to 1e-9. Even so the DCT is where the time goes:
about 240 microseconds a block, so about 1.3 of the 1.9 seconds the render
takes at 4:2:0 (median of five, in one process).

## Quantization is where the loss is

Every amplitude is divided by a number from a table and rounded, and that is the
only step that loses anything on purpose. The tables in Annex K of the standard
have small divisors at the top left and big ones at the bottom right, so the
fine detail is cut the hardest. The chroma table is mostly 99.

The quality number is not in the standard. It is the Independent JPEG Group's
formula: below 50 the table is scaled by 5000/q percent, above 50 by 200 - 2q,
and the result is clamped to 1..255. So 50 is the printed table, 75 halves it,
and 100 is all ones. Because this matches libjpeg, the test can ask Pillow to
read the tables out of our file and out of its own at the same quality, and
they must be equal at every quality tried. That one check caught every scaling
mistake I made on purpose, and the zigzag ones too.

Quality 100 is not lossless. All the divisors are one, but the colour
conversion and the DCT output are still rounded to whole numbers, so most
pixels come back exact and some come back one level off.

### A flat colour is not always better at higher quality

I expected a flat colour to come back nearly perfect at any quality. It did
not: a 200, 100, 50 square came back 5 levels off at quality 10, and its PSNR
was 36.4 dB at quality 10, 49.9 at 25, and then 45.9 at 50, worse than 25. A
flat block is only a DC value, so the error is just how far that value sits from
the nearest multiple of the DC divisor, and that does not shrink smoothly as the
divisor does. Pillow's encoder gives exactly the same numbers. The test now asks
that a flat colour is no further off than Pillow's and exact from quality 75 up,
and leaves it out of the "PSNR rises with quality" check.

### Rounding a half, and the float noise in front of it

I first rounded halves away from zero, as libjpeg does, and wrote a check that
-2.5 became -3. Swapping in Python's `round`, which rounds halves to even, did
not fail it, because the float DCT of that block was a hair below -2.5, not
-2.5. On every test picture at four qualities the two gave byte-identical files,
so I decided rounding did not matter, switched to `round`, and wrote that down.

It did matter. To check that AC values never outgrow their size class (below), I
added the block with the largest AC amplitude there is, a checkerboard that puts
everything at frequency (4, 4). Its amplitude is exactly -1020, and at quality 25
the divisor there is 136, so the quotient is exactly -7.5. Pillow's file came
back perfect. Ours came back at 29.5 dB, because the float DCT said
-1019.9999999999992, which rounds to -7 whichever rule is used, and -7 times 136
is 68 short. So the fix is two parts: round the DCT output to six decimals to
snap off the float noise, then round halves away from zero. Going back to
`round` now fails the -2.5 check and that block, and dropping the snap fails
that block alone.

The lesson I take is that "byte-identical on my test pictures" only covered
pictures where exact halves never came up, and real ones rarely make them. The
pictures that do are the artificial, exact ones, which is a reason to keep some.

## Zigzag and run lengths

After quantizing, most of the 64 numbers are zero, and the zeros cluster at the
high frequencies. Reading the block in zigzag, along the anti-diagonals from the
top left, puts the low frequencies first and the zeros together at the end. The
table is hard-coded; the test builds it a second way, by walking the diagonals,
and compares.

The DC value is sent as the change from the previous block of the same channel,
because neighbouring blocks have similar averages. Each AC value that is not
zero is sent as a pair (how many zeros before it, its size class) and then its
bits. A run of more than 15 zeros needs a ZRL symbol, meaning sixteen zeros,
and when the rest of the block is zero an EOB ends it early.

A value is sent as its size class s, the number of bits in its magnitude, then
s bits: the value itself if positive, and value - 1 in s bits if negative. So 5
is size 3, bits 101, and -5 is size 3, bits 010, the first one's complement.
That is in the test by hand.

The tables only go up to size 10 for AC values and 11 for DC differences, and
that is enough without any clamping. The biggest an AC amplitude can get from
samples between -128 and 127.5 is at the (4, 4) checkerboard, where every cosine
product is 0.5: 64 samples times 0.5 times about 128, over 4, is 1,022, inside
size 10's limit of 1,023. DC lies between -1,024 and 1,020, so a difference is
at most 2,044, inside size 11's 2,047. The checkerboard is in the test.

## Huffman

The symbols (DC size classes, AC run and size pairs) are Huffman coded. Annex K
gives four tables, DC and AC for luma and chroma, built from statistics of a set
of test pictures. They are not optimal for any one picture, but they are what
most encoders use by default, Pillow included unless asked to optimize, which is
why the coded data here comes out within 3% of Pillow's from quality 25 to 90.
With `--optimize` it builds its own; see below.

As in DEFLATE, a table is sent as counts: how many codes of each length 1 to 16,
then the symbols in order. Both sides build the same canonical codes from that.
The test checks codes printed in the standard (luma EOB is 1010, ZRL is
11111111001, DC size 11 is 111111110), not codes computed by another copy of my
own loop, and checks each AC table has all 162 symbols exactly once.

## Byte stuffing

Markers in a JPEG are 0xFF and a code byte. The coded data is arbitrary bits, so
it can contain 0xFF too, and a decoder would take it for a marker. So after every
0xFF byte in the data the encoder writes 0x00, and the decoder drops it. The last
byte is padded with one bits.

It is easy to think 0xFF would be rare. It needs eight one bits lined up with a
byte, and long runs of ones come from long codes. The flat colour never has one,
the render at quality 25 has 11, at 75 has 89 and at 100 has 667. With stuffing
switched off, Pillow refused the files with "broken data stream". With the last
byte padded with zeros instead of ones, Pillow read every file without
complaint; only the hand-built checks of the bit writer noticed. A decoder does
not look at the padding, so a check through the decoder can never catch it.

## Checking it with someone else's decoder

The rule from the gzip project holds here: if my own decoder read my own files,
a misunderstanding shared by both would pass. So every file is decoded by Pillow
(libjpeg), and the test measures the pixels it got back against the originals,
at six qualities and both subsamplings, on a flat colour, two gradients, a
text-like black and white pattern, colour stripes, 17x9 noise, the
checkerboard block above and the ray-traced render. PSNR must rise with quality
(except for the flat colour and the checkerboard, which are one coefficient
each), must be within 0.3 dB of Pillow's own encoder at the same settings (3.5
dB at quality 100, below), and the file must be no more than 5% bigger than
Pillow's.

At quality 100 ours is behind Pillow on hard edges: 63.0 dB against 65.9 on the
black and white pattern, 54.1 against 54.6 on the stripes at 4:4:4. Every
pixel there is either exact or one level off; ours has 405 off by one out of
12,288 values, Pillow's 207. My guess is that libjpeg's integer forward DCT and
the integer inverse DCT Pillow decodes with are a matched pair whose rounding
partly cancels, and a float DCT does not share it. I have not tested that. On
smooth gradients at quality 100 ours is the smaller file by up to 21% (1,997
bytes against 2,518) at slightly higher PSNR, which may be the same rounding
from the other side.

Every check was shown to fail by breaking the code on purpose, one breakage at a
time, on a copy, and running the tests against it:

| breakage | caught by |
| --- | --- |
| zigzag entries 1 and 2 swapped | diagonal walk; quantization tables differ from Pillow's; PSNR against Pillow |
| no byte stuffing | bit writer by hand; Pillow could not read the file |
| DC not differenced | hand-coded block; flat colour further off than Pillow; PSNR |
| quality scaling 200 - q instead of 200 - 2q | quality 50, 75 and 100 tables by hand; tables differ from Pillow's |
| scaling below 50 off by one (5000 / (q + 1)) | quality 25, 10 and 1 tables by hand; tables differ from Pillow's |
| 4:2:0 chroma taken one row down | PSNR against Pillow (gradients, stripes) |
| 4:2:0 chroma taken one column right | PSNR against Pillow |
| padding with the first column, not the last | PSNR and size against Pillow, only on 17x9 and 33x21 |
| rounding toward zero | -2.5 to -3; flat colour further off than Pillow; PSNR |
| halves rounded to even (`round`) | -2.5 to -3; PSNR on the checkerboard block |
| float noise not snapped before rounding | PSNR on the checkerboard block (only that) |
| DCT with C(0) = 1 | flat, cosine and textbook DCT checks; hand-quantized block |
| DCT transposed | cosine block; textbook formula; PSNR |
| colour coefficient 0.587 as 0.578 | YCbCr by hand; YCbCr against Pillow's; flat colour |
| negative values sent as v, not v - 1 | size classes by hand; hand-coded block; PSNR |
| canonical codes not shifted between lengths | codes printed in Annex K; hand-coded block; PSNR |
| an AC luma symbol duplicated | AC table completeness |
| ZRL counting 15 zeros, not 16 | hand-coded block; PSNR on stripes |
| EOB sent even after a last nonzero value | PSNR (4 dB); PSNR rises with quality |
| last byte padded with zeros | bit writer by hand; hand-coded block (not Pillow) |
| luma blocks in an MCU column first | PSNR against Pillow |
| DQT written in natural order | tables differ from Pillow's |
| no level shift | flat colour 205 off; PSNR |
| `-q` ignored on the command line | command line checks |
| image size not validated | bad input refused (it crashed with IndexError) |
| quality range not validated | quality 0 and 101 refused |
| `encode_block` returning the previous DC | its return value; PSNR |
| SOF width and height swapped | Pillow read the wrong size |
| SOF saying 1x1 luma sampling with 4:2:0 data | Pillow sees the wrong sampling; PSNR |
| `--444` ignored | command line reads the sampling back |
| default output name wrong | default output name |
| quantization table read in zigzag places | hand-quantized block; PSNR |

Three of these first made the test crash instead of report: no stuffing made
Pillow raise inside the loop, a missing symbol raised KeyError in the code
lookup before the completeness check ran, and an unvalidated size raised
IndexError where the check only caught ValueError. Each still failed the run,
but a crash stops every later check, so the loop now reports a file Pillow
cannot read and carries on, the completeness check runs first, and the input
checks report any other exception as a failure.

The padding breakage is the weakest catch. Repeating the first column instead of
the last only changes pixels nobody sees. It made 11 checks fail, all on the
17x9 and 33x21 pictures, through the extra ringing and bits it causes there.

## Optimized Huffman tables

`--optimize` does what `jpegtran -optimize` and libjpeg's `optimize_coding`
do. The coefficients are worked out once and kept. A first pass runs them
through the same `encode_block` that writes the file, but with a stand-in for
each code table that counts every symbol asked for and writes nothing. So the
counts cannot disagree with what the second pass sends: there is no second
copy of the run-length logic to drift. Then four tables are built from the
counts, and the second pass writes the data and the DHT with them.

### Why per-image tables help

The Annex K tables were built from someone else's pictures. They give every
one of the 162 AC symbols a code, used or not, and their lengths match average
statistics, not these. On the ray-traced render at quality 75 and 4:2:0 the
coded data went from 17,645 bytes to 17,007 (3.6%), and the DHT from 420 bytes
to 154, because it lists only the symbols used. At quality 25 the data went
from 8,578 to 7,171 (16%): low quality makes far more zeros and EOBs than the
standard tables expect, so their lengths are further off. On a small picture
the table is most of the saving. The flat colour drops from 636 bytes to 279,
and 279 is exactly 16 bytes under Pillow's optimized file, the same header
difference as before, so the tables and data come to the same size as
libjpeg's.

### The 16-bit limit

DHT sends how many codes have each length from 1 to 16, so no code may be
longer. Plain Huffman has no such limit. Counts that grow like Fibonacci
numbers make each merge swallow the last one, and the tree goes one level
deeper per symbol. The test uses 30 counts starting 2, 3, 5: with the dummy's 1
added, a plain Huffman built with `heapq` in the test is 30 deep.

Annex K.3 fixes the lengths afterwards. While there is a code longer than 16,
take two sibling codes at that length. One moves up into their parent's place.
The other goes beside a code from the longest shorter length that has one,
which becomes two codes one bit longer. Each step keeps the code space exactly
full. The Fibonacci table comes out as BITS 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0,
1, 1, 2, 15. The render did not need it, but came close: its longest code was
13 bits at quality 25 and 15 bits at quality 90, both in the luma AC table.

The arithmetic in that step is easy to get wrong in a way that never ends:
adding one leaf instead of two below made the loop spin forever, so the test
builds the tables in a thread with a ten second limit and reports a build
that does not finish.

### The all-ones code

The standard forbids a code made only of one bits, so that the ones used to
pad the last byte (and the 0xFF before a marker) can never read as a whole
code. libjpeg's answer, which is in K.2, is a dummy symbol 256 with count 1.
Ties between equal counts go to the higher symbol, so the dummy is always
taken first and ends up deepest. After the lengths are counted, one code is
taken off the longest length. Canonical codes run upwards, so the code removed
is the last one, the all-ones code. The dummy is never sent and never listed.

That gives three properties the test checks from the outside, on a table
worked by hand, one symbol alone, the Fibonacci counts, and random counts of
2, 5, 40 and 162 symbols:

- every symbol with a count is listed once, and no other;
- the Kraft sum of the lengths is 1 minus one code of the longest length, so
  the code is complete except one slot;
- no code is all ones, and none is the start of another.

The hand table is counts 4, 2, 1. With the dummy's 1: the dummy and C merge
into 2, that and B into 4, that and A into 8. So A is 0, B is 10, C is 110,
and 111 is the dummy's, left unused. The one-symbol table gets the code 0 and
not an empty code, because the dummy gives it something to merge with.

The test also bounds the cost of each table with a separate Huffman: at least
what plain Huffman on the counts needs, and with no limit needed, no more than
plain Huffman on the counts plus the dummy.

### Breaking it on purpose

As before, one breakage at a time, on the encoder, with the test run against
it and the encoder restored after each:

| breakage | caught by |
| --- | --- |
| K.3 length limit not applied | symbols lost from the Fibonacci table; Fibonacci cost below Huffman |
| no dummy symbol, nothing removed | hand table; all-ones code; Kraft sum; one symbol gets no code; Pillow could not read 54 files |
| luma blocks counted into the chroma tables | the second pass found symbols with no code (KeyError) |
| Cr blocks not counted | the same, on 51 files |
| DHT written from the standard tables | Pillow decoded different pixels (92 files) |
| `optimize` silently ignored | optimized file not smaller than the standard one |
| K.3 adds one leaf below, not two | the Fibonacci build did not finish |
| dummy taken from the shortest length | Kraft sum; hand table; cost against Huffman |
| DC prediction carried over from the counting pass | Pillow decoded different pixels; missing symbols |
| `--optimize` ignored on the command line | command line check |
| optimize on by default | the hash of every default file; optimized not smaller |
| ties go to the lower symbol | nothing |

The last one passed, and I left it. The point of the tie rule is that the dummy
ends up deepest, so the code taken off the longest length is its own. With the
other rule the dummy can sit higher up, and a real symbol is then listed at a
length it was not built with. But the removal works on the count of lengths,
not on the dummy, so what is sent is still a valid code with the all-ones slot
free, and every check on it still holds. It can only cost bits. I kept the
libjpeg rule because then the lengths are exactly the tree's.

The first three runs taught me about the test, not the code. The missing dummy
first crashed the test with `max()` of an empty list on the one-symbol table,
and the K.3 slip hung it, which is why the table checks now run in a thread
with a time limit and report either as a failure. Counting from the wrong
component never makes a bad file: the second pass stops with KeyError on a
symbol its table does not have, and the whole-file loop reports that and
carries on. With no dummy, the command line check is still the one place a
file Pillow cannot read stops the run, but it is the last check.

To show the default output is unchanged, the test hashes every file it writes
without `optimize` and compares the hash with the one I recorded from the
encoder before the option existed. The coefficients are now worked out before
any are coded, but the order and the bits are the same.

## Reading it back

`decode(data)` returns the width, the height and the RGB pixels of a baseline
JPEG, and `python jpeg.py -d IN.jpg -o OUT.png` writes them out (Pillow only
writes the PNG). It reads SOF0 with 8-bit samples, one component (greyscale)
or three (YCbCr) at 4:4:4, 4:2:2 or 4:2:0, the DQT, DHT, SOS and DRI segments,
restart markers and byte stuffing, and skips APPn and COM. The steps are the
encoder's run backwards: Huffman codes to (zeros, size) pairs, zigzag undone,
times the table, inverse DCT, chroma upsampled, YCbCr to RGB.

### Testing only on other people's files

The rule from the encoder is turned around, as it was for inflate in the
deflate project: the decoder is tested on files Pillow (libjpeg) wrote, and
its pixels are compared with Pillow's own decoding of the same file. If it
were tested on this project's files, an encoder and a decoder that shared a
misreading of the standard would agree with each other and pass. The files
cover every picture in the test at qualities 10, 50, 90 and 100 (the render at
75), each of Pillow's three subsamplings, greyscale, `optimize=True`, restart
markers every MCU, every three MCUs and every MCU row (Pillow 12.3 calls the
options `restart_marker_blocks` and `restart_marker_rows`), odd sizes 17x9 and
33x21, and the ray-traced render re-encoded by Pillow. It reads this
project's own files too, but never only them.

### The trust boundary

An encoder only sees pixels it was handed. A decoder reads whatever bytes
arrive, so every length, count and table id in the file is checked before it
is used, and a broken file raises `ValueError` with a reason. The test has a
hand-built case for each: a cut file, no SOI, an unknown marker, a DHT with
three one-bit codes, a DHT with 257 codes, Huffman and quantization tables
that were never defined (one of them Pillow's own abbreviated file, written
with `streamtype=2`), a run of ones that matches no code, restart markers out
of order, and a DC difference and an AC value too big for 8-bit samples. Then
every one of the 773 truncations of a 773-byte Pillow file, the scan cut at
each of its 148 bytes with the EOI put back, and 300 seeded random bit flips
across two files. Of the flips, 191 still decode to a picture of the right
size and every other one is refused with `ValueError`, never anything else.

A file can also be valid and simply not something this reads. Arithmetic
coded, lossless, hierarchical, extended (SOF1) and 12-bit files are refused by
name, for example "lossless (SOF3) JPEGs are not supported, only baseline
(SOF0) and progressive (SOF2)". The test checks that wording on a real file
with its SOF0 marker or precision byte changed.

Sizes in the header are not trusted either. One flipped bit can turn a width of
17 into 32,785, so a block is only made when a scan's data reaches it, and a
header that promises more than the data holds runs out of bits before it has
cost much memory. Progressive files make this harder, see below.

### Running out of bits, and a misleading message

Each restart interval's coded bytes are turned into a string of bits, with
padding after the end so that peeking 16 bits ahead is always safe, and after
each block the reader checks it has not gone past the real end. I first
padded with ones. Then I broke the decoder by removing that end check, and no
test failed: a truncated file had lost its EOI, so it was refused before
decoding started. So I added the scan cut short with the EOI put back. With
ones as padding, all but one of those cuts were refused with "a Huffman code
matches nothing", because the all-ones code is the one every table leaves out. That
is true and useless to someone with a cut-off download. libjpeg pads with
zeros, and the all-zeros code always exists, so running out reads as ordinary
codes until the block check says "the scan data ends in the middle of a
block". With zeros, all 148 cuts give that message, and removing the end check
fails all 148.

### Restart markers

DRI sets an interval in MCUs. After each interval the encoder pads to a whole
byte, writes RST0 to RST7 in turn, and starts every DC prediction again from
zero, so damage in one interval cannot spread into the next. The reader finds
the markers first, while it removes the stuffing, so each interval is decoded
from its own bits, and it checks there are exactly as many markers as the
interval needs, in order. Pillow's `restart_marker_blocks=1` on the render
writes 919 of them, one between each pair of its 920 MCUs. With the DC
prediction carried across a marker, the render came back at 8.65 dB against
Pillow's decoding, and only the restart files failed. A marker may have extra
0xFF fill bytes in front of it, and the test puts two before RST1.

### Progressive files

A progressive file sends the same quantized coefficients as a baseline one,
in several passes instead of block by block, so a viewer can show a blurry
picture early and sharpen it. Each scan (Annex G) covers a band of zigzag
positions, Ss to Se, at a precision: Al is how many low bits are still left
out, and Ah is how many the previous pass left out, 0 on a first pass. Four
kinds of scan follow from that:

- DC first (Ss = Se = 0, Ah = 0): the DC differences as in baseline, shifted
  up by Al. It may interleave all components.
- DC refinement (Ss = 0, Ah > 0): one raw bit per block, the next DC bit down.
- AC first (Ss > 0, Ah = 0): the band's (zeros, size) pairs, shifted by Al,
  one component per scan. An end-of-band code carries a count, 2^r plus r more
  bits, of blocks in a row that have nothing in this band, so one symbol can
  stand for up to 32,767 blocks.
- AC refinement (Ss > 0, Ah > 0): the hard one. Every coefficient already
  non-zero gets one correction bit, which moves it one step of 2^Al away from
  zero. Coefficients that become non-zero only at this precision are sent as
  a run and a sign, and the run counts only the zeros, skipping over (and
  correcting) the non-zero ones on the way. Inside an end-of-band run the
  known coefficients still get their correction bits.

So the decoder no longer turns a scan straight into pixels. Every scan writes
into a store of quantized coefficients per component, in zigzag order, and
only after EOI is each block dequantized, inverse transformed and laid into
its plane. Baseline files go the same way, which also lets them send one
scan per component (each read block by block over the component's own size,
not whole MCUs), something the old decoder refused. The quantization table
is taken when a component is first seen, as libjpeg does.

The memory rule above needed rethinking. A progressive header can ask for
60000x60000 pixels, and its AC scans can then cover the whole picture with a
few end-of-band symbols, never paying a bit per block. So blocks are made only
when a scan writes to one (an unseen block inside an end-of-band run is read
as zeros and left unmade), and at the end every block of each component must
have been reached. A real file always does that, since its DC scan codes every
block with at least one bit, so a header that promises more than the data
holds runs out of bits instead. The test pins both halves: the 60000x60000
header is refused, and a flat picture's file without its DC scans, whose AC
scans are nothing but end-of-band runs, is refused with "the scans never
reach every block". Making blocks inside end-of-band runs, or dropping the
reach check, lets that file decode.

The strongest test is lossless. `jpegtran` rewrites the scans of a file
without touching a coefficient, so every rewritten file must decode to
exactly the bytes of the baseline file it came from, with no tolerance at
all. `scans/` holds a 37x29 picture at 4:2:0 and 4:4:4 and greyscale, each
rewritten with jpegtran's own progressive script (with restart markers every
block too), one baseline scan per component (`per_component.txt`), spectral
selection only (`spectral.txt`), and a deeper script of successive
approximation with three DC passes, two AC bands and refinements down to
Al = 0, with a restart every two blocks (`successive.txt`). The files are made
with, for example, `jpegtran -scans scans/successive.txt -restart 2B -outfile
scans/420_successive.jpg scans/420.jpg`, so the test needs no jpegtran itself.
Pillow's own `progressive=True` files are separate encodes, so those are held
to Pillow's reading within 3 levels, as every other file is. The first
version passed the lossless files on its first run; three breakages (a
refinement bit that ignores the sign, an end-of-band run one block too long,
DC refinement dropped) each fail them.

Reading every scan into a store first costs baseline files a little: 1.11
seconds against 1.01 for the render at quality 75, medians of five runs
alternating the old and new decoder in one process.

### Why it differs from Pillow by a few levels

Three steps round differently. libjpeg's default inverse DCT is in integers
(`JDCT_ISLOW`) and this one is in floats. libjpeg converts colour with integer
tables and this uses the JFIF equations in floats. And libjpeg rounds the
upsampled chroma to whole numbers before converting it, where this keeps the
fractions. On the render at quality 75, the share of the 691,200 values that
come out exactly equal to Pillow's:

| file | exact | 1 off | 2 off | 3 off | PSNR between the two |
| --- | --- | --- | --- | --- | --- |
| greyscale | 99.38% | 0.62% | 0 | 0 | 70.24 dB |
| 4:4:4 | 99.11% | 0.79% | 0.09% | 2 values | 67.49 dB |
| 4:2:2 | 91.50% | 8.40% | 0.10% | 5 values | 58.69 dB |
| 4:2:0 | 86.71% | 13.17% | 0.12% | 9 values | 56.77 dB |

The jump at 4:2:2 is the chroma rounding. To check that, I rounded the
upsampled chroma the way libjpeg's `h2v1_fancy_upsample` does, adding 1 or 2
before dividing by 4, and 4:2:2 went to 685,436 exact out of 691,200, 99.17%,
the same as 4:4:4. I kept the fractions, which are closer to the arithmetic,
and the test allows for the difference instead.

Over all 131 files in the test (127 written by Pillow, 4 by this
encoder), no value is more than 3 levels from
Pillow's, and the lowest PSNR between the two decodings is 51.90 dB (the
colour stripes at quality 100, 4:2:2). The test asks for at most 3 levels
everywhere, at least 57 dB at 4:4:4 and 51 dB with subsampling (each about a
decibel under the lowest measured), within 1 level for greyscale, and exactly
Pillow's values for a flat colour and a flat grey at 4:4:4, where only the DC
value is decoded and both decoders agree to the level.

Upsampling is libjpeg's "fancy" kind, its default: each new chroma sample is
3/4 of the nearer old sample and 1/4 of the next one out, with the edge
sample repeated. Plain replication, each chroma sample copied to its 2x2
square, is simpler, and on the smooth render it still gets 53.74 dB against
Pillow, but it is up to 17 levels off at colour edges, and on the colour
stripes it falls to 17.39 dB. Either way the bound catches it.

Three-component files are YCbCr unless an Adobe APP14 segment says transform
0, which means R, G and B. Pillow writes that with `keep_rgb=True`, and the
test reads two such files.

It is slow: the render takes 1.27 seconds at 4:2:0, 1.47 at 4:4:4 and 0.44 in
greyscale (median of five, alternating, in one process), against about 1.2
milliseconds for Pillow. The inverse DCT skips columns that are all zero.

### Breaking the decoder on purpose

As with the encoder, one breakage at a time on a copy, with the reading tests
run against it:

| breakage | caught by |
| --- | --- |
| zigzag not undone | bound against Pillow (109 files) |
| DC prediction not reset at a restart marker | bound against Pillow, only the 9 restart files |
| byte stuffing not removed | bound against Pillow; "ends in the middle of a block" on clean files |
| chroma taken from the next MCU's block | bound against Pillow (69 files) |
| IDCT transposed | bound against Pillow, greyscale too |
| Cr to red 1.42, not 1.402 | bound against Pillow (53 files); flat colour exact |
| Cb to green 0.334136, not 0.344136 | bound against Pillow (23 files); flat colour exact |
| replication, not fancy upsampling | bound against Pillow at 4:2:2 and 4:2:0 (47 files) |
| fancy upsampling with the neighbours swapped | bound against Pillow (47 files) |
| negative values not extended | bound against Pillow, every file |
| ZRL counting 15 zeros | bound against Pillow (42 files) |
| no level shift | bound against Pillow, every file |
| inverse DCT output not clamped | bound against Pillow (42 files) |
| canonical codes not shifted between lengths | "matches nothing" on every file |
| Huffman overflow check removed | the DHT with three one-bit codes (by its message) |
| 256-code limit removed | the DHT with 257 codes (by its message) |
| end-of-data check removed | at first nothing; then all 148 cut scans with EOI kept |
| DC and AC size limits removed | at first nothing; then the two hand-built tiny files |
| restart marker order not checked | restart markers out of order |
| DRI ignored | every restart file refused |
| SOI not checked | missing SOI (by its message) |
| unknown markers skipped | unknown marker 0xFFF7 |
| refinement bit that ignores the sign | the lossless progressive files |
| end-of-band run one block too long | the lossless progressive files |
| DC refinement bit dropped | the lossless progressive files |
| blocks made inside end-of-band runs | a flat picture without its DC scans |
| check that the scans reach every block removed | a flat picture without its DC scans |
| precision not checked | 12-bit precision decoded |
| a lone component keeps its 2x2 factors | greyscale marked 2x2 against Pillow and against itself |
| chroma size rounded down, not up | 17x9 and 33x21 at 4:2:2 and 4:2:0 |
| SOF width and height swapped | sizes against Pillow |
| fill bytes before a marker not skipped | at first nothing; then fill bytes before RST1 |
| Adobe transform ignored | the two `keep_rgb` files |
| `-d` ignored on the command line | command line checks |
| a broken file raising past the command line | command line on a broken file |

Three breakages passed at first, and each one was a hole in the tests, not in
the code. The end check is the story above. The size limits were never
reached by any real file, so the test now builds two 8x8 files by hand, with
one code per table and the bits written out, that use a DC difference of size
12 and an AC value of size 11. The same builder makes a valid file by hand: DC
size 3, bits 101, so DC 5, which is 5/8 over 128 and rounds to 129 in every
pixel, and Pillow agrees. Fill bytes were handled twice over, by the scan
reader and by the marker loop, so removing one made no difference until a
fill byte sat in front of a restart marker, inside the scan.

Some catches are only by the error message. With the Huffman overflow check
removed, a table with three one-bit codes is still refused, but later and for
a wrong reason ("more than 64 coefficients"): assigning to a slice past the
end of a Python list grows the list instead of failing, so the bad table was
built without complaint. That is why the refusal checks look at the reason,
not only at the exception type.

## What it does not do

The encoder writes no progressive files and no EXIF. The decoder refuses
arithmetic coded, lossless, hierarchical, extended (SOF1) and 12-bit files,
sampling other than 1x or 2x the chroma (4:1:1), and four components (CMYK). It ignores EXIF orientation and ICC profiles. A file with
no JFIF or Adobe segment and components named R, G and B is read as YCbCr,
where libjpeg would read it as RGB.
