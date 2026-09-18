# Accuracy

Every number here comes from the generated corpus — 106 files, 191 pages, all
with exact ground truth — and can be reproduced in about three minutes
([[Evaluation]]). Nothing is cherry-picked, including the bad rows.

## Overall

| metric | value |
|---|---|
| mean character error rate (CER) | **0.040** |
| median CER | **0.006** |
| mean word error rate (WER) | 0.120 |
| mean word recall | 0.892 |
| unexpected failures | **0** |

The gap between mean and median is the whole story: most documents are read
almost perfectly, and a handful of layout-heavy ones drag the mean up.

A CER of 0.006 means about one wrong character in 170 — for scanned German text,
roughly one typo every four lines.

## By document type

| category | mean CER | word recall | ms/page |
|---|---:|---:|---:|
| skewed (−7°…+6°) | 0.000 | 1.000 | 744 |
| every raster format (8) | 0.001 | 0.977 | 638 |
| born-digital PDF | 0.002 | 0.990 | 620 |
| newspaper, 3 columns | 0.003 | 0.977 | 1131 |
| multi-page TIFF (47 pages) | 0.003 | 0.983 | 623 |
| dark mode / inverted | 0.003 | 0.980 | 604 |
| multi-page PDF (47 pages) | 0.004 | 0.957 | 673 |
| aged, stained, bled-through | 0.006 | 0.937 | 713 |
| 60 dpi thumbnail | 0.006 | 0.889 | 412 |
| 1-bit fax with dropout | 0.014 | 0.867 | 547 |
| image-only PDF, clean render | 0.042 | 0.792 | 677 |
| rotated PDFs (/Rotate 90/180/270) | 0.054 | 0.884 | 885 |
| 3×24 inch receipt strip | 0.054 | 0.891 | 2594 |
| ruled forms / tables | 0.063 | 0.744 | 628 |
| A0 drawing at 300 dpi | 0.147 | 0.895 | 30290 |
| technical drawings | 0.170 | 0.855 | 583 |
| thermal receipts | 0.183 | 0.941 | 406 |

Skew, inversion, aging, fax dithering, low resolution and multi-page containers
are, for practical purposes, solved. What is left is **layout**, not character
recognition — see below.

## By language

| language | files | mean CER | word recall |
|---|---:|---:|---:|
| Polish | 8 | 0.004 | 0.954 |
| French | 8 | 0.006 | 0.945 |
| Czech | 4 | 0.008 | 0.942 |
| English | 14 | 0.008 | 0.936 |
| German | 58 | 0.049 | 0.926 |
| Japanese | 4 | 0.065 | 0.328 |
| Greek | 4 | 0.180 | 0.536 |

Reading these correctly matters:

- **German's 0.049** is not a German problem — the German files include the
  receipts, forms and drawings, which is where the layout losses live. On clean
  German renders CER is 0.001.
- **Japanese word recall (0.328) is meaningless**: Japanese does not separate
  words with spaces, so a whitespace-based word metric cannot work. Its CER of
  0.065 is the number to read.
- **Greek is genuinely the weakest** — 0.180, and recall 0.536. Lookalike letters
  (`Α`/`A`, `Ο`/`O`, `Ρ`/`P`) and accented vowels defeat the shared charset. If
  Greek is your main language, measure first.

## Where it is weak, and why

### Order, not characters

Receipts (CER 0.183) keep a **word error rate of 0.059 and word recall of
0.941**: the words are all there and almost all spelled right, but they come out
in a different sequence than a human reads them. When a document's CER is ten
times its WER, the problem is order. A thermal receipt puts the item on the left and the price on the
right; whether that is one line or two is a judgement call, and the engine's
geometric answer sometimes differs from the ground truth's.

The same applies to forms (0.063 / recall 0.744) and drawings (0.170 / recall
0.855), where labels are scattered across a sheet.

The A0 sheet is the same story pointing the other way: **0.147** with word recall
0.895, up from 0.042 before boxes were assembled into lines. Its title block is
now read row-wise, and that sheet's ground truth lists the fields one per line.
Capping the merge gap so the A0 block stays split was tried, and it made the A3
drawings much worse (0.197 → 0.391), because there the row-wise join is what
matches. Both readings of a title block are defensible — which is the argument
for building real table structure rather than tuning a threshold.

This is a real limitation with a real fix: **table-structure recognition**, which
is not implemented. Reading order today is geometric — an XY-cut with baseline
merging and column detection. It handles a three-column newspaper (CER 0.003)
and fails to infer that a form's cells are a grid.

If order does not matter for your use case — indexing, search, keyword extraction
— use `word recall` as your metric, and these categories look very different.

### Rotated PDFs

Recall 0.514 at CER 0.082: the text is read, but a page rotated a quarter turn
has ambiguous reading order, and the first line is often not where the ground
truth says it is. Before the rotation fix this was CER 0.79, so this is what
"much better, still imperfect" looks like.

### What was fixed, and by how much

The corpus paid for itself on its first run. Each of these was a real defect:

| defect | before | after |
|---|---:|---:|
| A0 drawing at 300 dpi: detection scaled 8 pt labels to four pixels | 0.96 | 0.042 |
| Quarter-turned PDFs read in column order | 0.79 | 0.082 |
| Drawings: labels merged across the sheet | 0.47 | 0.170 |
| Ruled tables read column by column | 0.33 | 0.063 |
| Eight page workers slower than one | 0.79× | 1.33× |
| corpus mean | 0.118 | 0.040 |

Then a second pass, assembling lines from boxes: receipts 0.332 → 0.183, forms
0.156 → 0.063, the worst drawing 0.433 → 0.206, and the A0 sheet 0.042 → 0.147 as
described above.

Then a third: **spaces the recognizer swallowed** are restored from the column ink
of each crop. A JPEG-compressed scan would return `Gesamtbetrag:5.726,88EUR`; it
now returns the spaces. That moved everything that goes through a lossy encoder —
image-only PDFs 0.014 → 0.004 (recall 0.848 → 0.957), rotated PDFs 0.082 → 0.054
(recall 0.514 → 0.884), aged scans 0.012 → 0.006, raster formats 0.007 → 0.001 —
and no category got worse.

## Robustness

Seven deliberately broken files, and every one behaves as designed:

| file | outcome |
|---|---|
| `truncated.png` | `ValueError: unexpected end of file` |
| `empty.pdf`, `garbage.pdf`, `deflate_stream.bin` | `ValueError: unsupported input` |
| `header_only.pdf` | `RuntimeError: pdf error: could not parse PDF` |
| `png_named_pdf.pdf` | **read anyway** — content sniffing beats the extension |
| `blank.png` | no text, no error, no invented lines |

Nothing panics, nothing hangs, nothing returns plausible nonsense. A blank page
is especially worth calling out: the deskew estimator has an ink-fraction guard,
because an earlier version confidently rotated an empty page by 12°.

## Confidence is usable

Mean line confidence tracks quality closely enough to route documents:

| category | confidence |
|---|---:|
| clean, born-digital, receipts | 0.99 |
| aged, fax | 0.98 |
| Greek | 0.93 |

```python
doc = ocrust.scan("scan.tiff")
if doc.confidence < 0.9:
    queue_for_human_review(doc)

for line in doc.lines:
    if line.confidence < 0.7:
        highlight(line.box)
```

## Measuring your own documents

Averages over someone else's corpus do not bind your invoices. The evaluator
works on any folder that carries ground-truth text files:

```bash
python scripts/evaluate_corpus.py /path/to/your/corpus -o report
```

See [[Evaluation]] for the layout it expects and the metrics it reports.
