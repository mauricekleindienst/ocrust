# Evaluation

Claims about OCR quality are worthless without a corpus and an error rate. Two
scripts in the repository produce both, from nothing:

```bash
python scripts/make_corpus.py --out /tmp/corpus         # build the documents
python scripts/evaluate_corpus.py /tmp/corpus -o report # measure everything
```

The generator needs Pillow and numpy. The library itself never does.

Output: `report.md` to read and `report.json` to diff or plot.

## Why generated, not collected

Every generated file ships with **the exact text it contains**. That is what
makes character error rates possible at all — a folder of real scans has no
ground truth, so it can only produce anecdotes.

The generator deliberately abuses the documents: faded ink, coffee stains,
bleed-through from the reverse side, repeated JPEG recompression, fax dithering
with dropout streaks, skew, quarter turns, dark mode, A0 sheets, 60 dpi
thumbnails, and seven files that are outright broken.

## What the corpus contains

106 files, 191 pages, 416 MB.

| category | what it simulates |
|---|---|
| `clean` | crisp renders at 200 and 300 dpi, six languages, as image and as image-only PDF |
| `aged` | faded ink, yellowed paper, stains, speckles, scanner blur, bleed-through, JPEG generations |
| `fax` | 1-bit Floyd–Steinberg dithering at half resolution, with dropout streaks |
| `drawings` | A3 technical drawings: frame, title block, hatching, dimensions, vertical section labels |
| `forms` | ruled five-column tables |
| `receipts` | narrow thermal strips in a monospaced face, faint print |
| `newspaper` | three-column layout |
| `rotated` | skew from −7° to +6°, and PDFs with `/Rotate 90/180/270` |
| `screenshots` | dark mode, light text on a dark background |
| `extremes` | A0 at 300 dpi (~9900×7000 px), a 60 dpi thumbnail, a 3×24 inch receipt |
| `multipage` | 5, 12 and 30-page image-only PDFs and TIFFs, some pages aged |
| `borndigital` | PDFs with a real text layer, which the overlay tool must skip |
| `formats` | the same page as PNG, JPEG, WebP, BMP, GIF, PPM, TGA, TIFF |
| `broken` | truncated PNG, zero-byte PDF, random bytes, header-only PDF, a PNG named `.pdf`, a blank page, a deflate stream |

Ground truth is written to `ground_truth.json` next to the files.

## What the evaluation measures

- **Accuracy** per file: character error rate, word error rate and word recall.
  Recall is reported because reading order can legitimately differ — a drawing's
  title block, a three-column newspaper — and CER punishes that as if the
  characters were wrong.
- **Speed** per file and per page, plus MB/s, with the slowest files listed.
- **Every export format**, rendered from one document, with sizes and times.
- **The PDF text layer** over every PDF: pages layered, pages skipped, size
  growth, characters outside WinAnsi, and whether the result is really searchable.
- **Archive TIFF** output, colour and greyscale.
- **Worker scaling**: 1, 2, 4 and 8 page workers on a 12-page scan.
- **A DPI sweep** (100/150/200/300).
- **Preprocessing on versus off** over the skewed, aged and inverted pages.
- **Batch API** versus one file at a time.
- **Robustness**: files marked `expect_error` must fail cleanly, and anything
  that fails unexpectedly is called out.

`--quick` skips the sweeps. `--models DIR` evaluates a different bundle — which
is how you compare a candidate model against the shipped one on identical input.

## The metrics

| metric | definition |
|---|---|
| CER | Levenshtein distance over characters ÷ ground-truth length, after whitespace normalization |
| WER | the same over whitespace-separated words |
| word recall | share of ground-truth words present in the output, **order-insensitive** |

Reading them together is the point:

- low CER, high recall → correct
- **high CER, high recall** → the words are right, the *order* is not: columns,
  tables, scattered labels
- low recall → text genuinely missing or mangled
- recall is meaningless for Japanese and Chinese, which do not separate words with
  spaces; use CER there

## What the first run found

Five real defects, none of them measurement artifacts:

| finding | fix |
|---|---|
| An A0 drawing at 300 dpi scored CER 0.96: detection scales the page to 960 px, turning an 8 pt label into four pixels | tiled detection above 4× the working size, with overlap and exact tile ownership |
| Pages rotated a quarter turn came back in column order | the share of tall boxes decides a 90° turn, then detection re-runs |
| Upside-down pages read every line correctly but in reverse order | the 180° classifier's verdict now rotates the page geometry too |
| Ruled tables were read column by column | a column split now needs a real gutter, which a table's cell gaps never reach |
| Eight page workers were 20% *slower* than one | page workers and ONNX Runtime threads divide the cores instead of each claiming all |

Results, before → after: A0 0.96 → 0.042, rotated PDFs 0.79 → 0.054, drawings
0.47 → 0.170, corpus mean 0.118 → 0.040. Worker scaling went from 0.79× to 1.33×.

Two later passes came out of the same corpus. Assembling lines from boxes took
receipts 0.332 → 0.183 and forms 0.156 → 0.063, and cost the A0 sheet
0.042 → 0.147 — the trade-off is spelled out on [[Accuracy]]. Restoring swallowed
word spaces took the corpus median from 0.013 to 0.006 and word recall from 0.836
to 0.892, with image-only PDFs going 0.014 → 0.004.

The current numbers are on [[Accuracy]] and [[Performance]].

## Evaluating your own documents

The evaluator reads a `ground_truth.json` at the root of the corpus, keyed by
path relative to it:

```json
{
  "invoices/0001.png": {
    "lines": ["RECHNUNG Nr. 2026-04-1187", "Betrag: 5.726,88 EUR"],
    "language": "de",
    "category": "invoice",
    "pages": 1
  },
  "invoices/broken.pdf": {"category": "broken", "expect_error": true}
}
```

`lines` is the ground truth, joined with newlines. `category` groups the summary
table, `language` groups the per-language table, `pages` is checked against what
was actually read, and `expect_error: true` marks a file that *must* fail.

```bash
python scripts/evaluate_corpus.py /path/to/your/corpus -o report
```

Files without an entry are still scanned, timed and checked for crashes — they
just do not contribute to the error rates. That makes the script useful as a
smoke test over a real archive, before you have any ground truth at all.

## Benchmarking against other engines

```bash
python scripts/benchmark.py page.png --runs 5
python scripts/benchmark.py scan.pdf --only ocrust rapidocr
```

Times one image or single-page PDF with `ocrust` and with whichever of
`rapidocr`, `pytesseract`, `easyocr` and `paddleocr` happen to be importable in
the same environment. Install nothing else and it reports `ocrust` alone.
