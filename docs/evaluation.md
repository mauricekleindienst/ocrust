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
between runs or plotted. The last full run is checked in as
[`evaluation-report.md`](evaluation-report.md).

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

- **median 669 ms per page** (mean 823, which the A0 sheet dominates), 157.3 s for
  the whole corpus
- **median CER 0.006**, mean 0.040, mean WER 0.120, mean word recall 0.892
- **zero unexpected failures**; the seven deliberately broken files behave as
  designed (five error cleanly, the PNG named `.pdf` is read anyway, the blank
  page returns no text)

| category | mean CER | ms/page | | category | mean CER | ms/page |
|---|---:|---:|---|---|---:|---:|
| skewed | 0.000 | 744 | | multipage (PDF) | 0.004 | 673 |
| every raster format | 0.001 | 638 | | 60 dpi thumbnail | 0.006 | 412 |
| born-digital | 0.002 | 620 | | aged and stained | 0.006 | 713 |
| newspaper (3 columns) | 0.003 | 1131 | | 1-bit fax | 0.014 | 547 |
| multipage (TIFF) | 0.003 | 623 | | clean | 0.033 | 663 |
| dark mode | 0.003 | 604 | | forms (tables) | 0.063 | 628 |
| rotated PDFs | 0.054 | 885 | | A0 at 300 dpi | 0.147 | 30290 |
| image-only PDF (clean) | 0.042 | 677 | | drawings | 0.170 | 583 |
|  |  |  | | receipts | 0.183 | 406 |

What moved, in the order the fixes landed: A0 drawings 0.96 → 0.042 (tiling),
rotated PDFs 0.79 → 0.054, drawings 0.47 → 0.170, forms 0.156 → 0.063, receipts
0.332 → 0.183 (line assembly), image-only PDFs 0.014 → 0.004 and word recall
0.836 → 0.892 (space restoration), and the corpus mean 0.118 → 0.040.

What is still weak, and why:

- **Receipts (0.183)**, **forms (0.063)** and **drawings (0.170)** lose on
  *order*, not on characters. Their word-level numbers say it plainly: a receipt's
  WER is 0.059 with recall 0.941, so the words are right and their sequence is
  not. Right-aligned prices and scattered labels end up in a different order than
  a human would read them. Proper table structure recognition is the fix, and it
  is not implemented.
- **The A0 sheet went the other way** when lines started being assembled from
  boxes: 0.042 before, **0.147** now, with word recall 0.895. Nothing is misread;
  the title block's two-column fields are joined into rows, and on that sheet the
  resulting sequence differs from the ground truth's. Capping the merge gap so
  that the A0 block stays split was tried and made the A3 drawings much worse
  (0.197 → 0.391), because there the row-wise join is what matches. Both
  behaviours are defensible readings of a title block, which is the honest
  argument for treating table structure as unfinished rather than tuned.
- **Greek** is the weakest language in the bundle (0.180, recall 0.536).

### Speed findings

| measurement | result |
|---|---|
| worker scaling on a 12-page scan | 7.9 s at 1 worker, 6.0 s at 4, 6.6 s at 8 (1.33x) |
| batch API versus one at a time | 0.99x — the same, once threads stopped fighting |
| rasterization resolution | 100 dpi is as accurate as 300 on these pages and 19% faster |
| preprocessing on versus off | CER 0.004 with, 0.040 without; 8% faster without |

That last row changed sign when line assembly landed, and the reason is worth
recording: boxes are now joined into lines by their baselines, and on a skewed
page the baselines are only horizontal *after* deskewing. Preprocessing used to
be a wash (identical CER) because every line was rectified individually before
recognition; now it also decides whether a line is assembled correctly. On the
skewed, aged and inverted pages, turning it off costs **ten times** the error rate
to save 8% of the time. Leave it on.

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
