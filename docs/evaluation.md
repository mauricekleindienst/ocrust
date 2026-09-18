# Evaluating the engine on a real corpus

Claims about OCR quality are worthless without a corpus and an error rate. Two
scripts in this repository produce both, from nothing:

```bash
python scripts/make_corpus.py --out /tmp/corpus       # build the documents
python scripts/evaluate_corpus.py /tmp/corpus -o report
```

The generator needs Pillow and numpy; the engine itself never does.

## What the corpus contains

Documents are generated, not collected, which means **every file ships with the
exact text it contains**. That is what makes error rates possible.

| category | what it simulates |
|---|---|
| `clean` | crisp renders at 200 and 300 dpi, six languages, as image and as image-only PDF |
| `aged` | faded ink, yellowed paper, coffee stains, speckles, scanner blur, bleed-through from the reverse side, repeated JPEG recompression |
| `fax` | 1-bit Floyd–Steinberg dithering at half resolution, with dropout streaks |
| `drawings` | A3 technical drawings: frame, title block, hatching, dimension labels, vertical section labels |
| `forms` | ruled five-column tables |
| `receipts` | narrow thermal strips in a monospaced face, faint print |
| `newspaper` | three-column layout |
| `rotated` | skew from −7° to +6°, and PDFs with `/Rotate 90/180/270` |
| `screenshots` | dark mode, light text on dark background |
| `extremes` | A0 drawing at 300 dpi (~9900×7000 px), a 60 dpi thumbnail, a 3×24 inch receipt |
| `multipage` | 5, 12 and 30-page image-only PDFs and TIFFs, some pages aged |
| `borndigital` | PDFs with a real text layer, which the overlay tool must skip |
| `formats` | the same page as PNG, JPEG, WebP, BMP, GIF, PPM, TGA, TIFF |
| `broken` | truncated PNG, zero-byte PDF, random bytes, header-only PDF, a PNG named `.pdf`, a blank page, a deflate stream |

## What the evaluation measures

- **Accuracy** per file: character error rate, word error rate, and word recall.
  Recall matters because reading order can legitimately differ — a drawing's
  title block, a three-column newspaper — and CER punishes that as if the text
  were wrong.
- **Speed** per file and per page, plus MB/s, with the slowest files listed.
- **Every export format** rendered from one document.
- **The PDF text layer** over every PDF: pages layered, pages skipped, size
  growth, characters outside WinAnsi, and whether the result really is
  searchable.
- **Archive TIFF** output, colour and greyscale.
- **Worker scaling** on a multi-page scan: 1, 2, 4 and 8 page workers.
- **A DPI sweep** (100/150/200/300) showing what resolution buys.
- **Preprocessing on versus off** over the skewed, aged and inverted pages.
- **Batch API** versus scanning one file at a time.
- **Robustness**: files marked `expect_error` must fail cleanly, and anything
  that fails unexpectedly is called out in the report.

Both a Markdown report and the raw JSON are written, so results can be diffed
between runs or plotted.

## What the first run found

The corpus paid for itself immediately. Every one of these was a real defect in
the engine or its defaults, not a measurement artifact:

| finding | fix |
|---|---|
| An A0 drawing at 300 dpi scored CER 0.96: detection scales the page to 960 px, which turns an 8 pt label into four pixels | tiled detection above 4x the working size, with overlap and exact tile ownership |
| Pages rotated a quarter turn came back in column order | the share of tall boxes decides a 90 degree turn, then detection re-runs |
| Upside-down pages read every line correctly but in reverse order | the 180 degree line classifier's own verdict now rotates the page geometry too |
| Ruled tables were read column by column | a column split now needs a gutter of at least 3.5% of the content width, which a table's cell gaps never reach |
| Eight page workers were 20% *slower* than one | page workers and ONNX Runtime threads now divide the cores instead of each claiming all of them |

## Results after the fixes

106 files, 191 pages, 416 MB on four CPU cores, with the shipped defaults:

- **788 ms per page**, 150.6 s for the whole corpus
- **median CER 0.013**, mean 0.054, mean word recall 0.834
- **zero unexpected failures**; the seven deliberately broken files behave as
  designed (five error cleanly, the PNG named `.pdf` is read anyway, the blank
  page returns no text)

| category | mean CER | ms/page | | category | mean CER | ms/page |
|---|---:|---:|---|---|---:|---:|
| skewed | 0.001 | 761 | | multipage (PDF) | 0.014 | 664 |
| newspaper (3 columns) | 0.003 | 1113 | | drawings | 0.170 | 622 |
| multipage (TIFF) | 0.003 | 607 | | forms (tables) | 0.156 | 637 |
| dark mode | 0.003 | 639 | | A0 at 300 dpi | 0.042 | 24801 |
| 60 dpi thumbnail | 0.006 | 431 | | rotated PDFs | 0.082 | 845 |
| every raster format | 0.007 | 660 | | receipts | 0.332 | 394 |
| aged and stained | 0.012 | 706 | | born-digital | 0.002 | 626 |
| 1-bit fax | 0.014 | 594 | | clean | 0.033 | 681 |

What moved after the fixes: A0 drawings 0.96 → 0.04, rotated PDFs 0.79 → 0.08,
drawings 0.47 → 0.17, and the corpus mean 0.118 → 0.054.

What is still weak, and why:

- **Receipts (0.332)** and **forms (0.156)** lose on *order*, not on characters —
  word recall stays at 0.88 and 0.74. Right-aligned prices and table cells end up
  in a different sequence than a human would read them. Proper table structure
  recognition is the fix, and it is not implemented.
- **Drawings (0.170)** are the same story: recall 0.855 with labels scattered
  across a sheet.
- **Greek** is the weakest language in the bundle.

### Speed findings

| measurement | result |
|---|---|
| worker scaling on a 12-page scan | 7.8 s at 1 worker, 6.0 s at 4, 5.8 s at 8 (1.34x) |
| batch API versus one at a time | 1.01x — the same, once threads stopped fighting |
| rasterization resolution | 100 dpi is as accurate as 300 on these pages and 8% faster |
| preprocessing on versus off | identical CER (0.008), 9% slower with it on |

That last row is worth stating plainly: on this corpus, deskewing and inversion
do not improve recognition, because the detector finds rotated boxes and each
line is rectified before it reaches the recognizer. They stay on by default
because they do help *layout* on multi-column pages, and the cost is small — but
`preprocess=False` is a legitimate way to buy 9%.

## Reading the numbers

- A CER of `0.01` means one character in a hundred is wrong — for scanned German
  text that is roughly one typo per two lines.
- Word recall below CER-implied quality usually means umlauts: in German, one
  wrong character inside `Führungsschiene` costs a whole word in recall while
  barely moving CER.
- Image-only PDFs score slightly worse than the same page as PNG, because the
  page is JPEG-compressed on the way in and rasterized again on the way out.
- `broken/png_named_pdf.pdf` is expected to *succeed*: content sniffing beats
  the file extension, which is what a document pipeline needs.
