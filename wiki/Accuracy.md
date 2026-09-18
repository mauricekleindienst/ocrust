# Accuracy

Every number here comes from the generated corpus — 106 files, 191 pages, all
with exact ground truth — and can be reproduced in about three minutes
([[Evaluation]]). Nothing is cherry-picked, including the bad rows.

## Overall

| metric | value |
|---|---|
| mean character error rate (CER) | **0.045** |
| median CER | **0.013** |
| mean word error rate (WER) | 0.176 |
| mean word recall | 0.836 |
| unexpected failures | **0** |

The gap between mean and median is the whole story: most documents are read
almost perfectly, and a handful of layout-heavy ones drag the mean up.

A CER of 0.013 means about one wrong character in eighty — for scanned German
text, roughly one typo every two lines.

## By document type

| category | mean CER | word recall | ms/page |
|---|---:|---:|---:|
| skewed (−7°…+6°) | 0.000 | 1.000 | 793 |
| born-digital PDF | 0.002 | 0.990 | 610 |
| newspaper, 3 columns | 0.003 | 0.977 | 1090 |
| multi-page TIFF (47 pages) | 0.003 | 0.980 | 774 |
| dark mode / inverted | 0.003 | 0.980 | 615 |
| 60 dpi thumbnail | 0.006 | 0.889 | 428 |
| every raster format (8) | 0.007 | 0.887 | 717 |
| aged, stained, bled-through | 0.012 | 0.856 | 738 |
| 1-bit fax with dropout | 0.014 | 0.867 | 557 |
| multi-page PDF (47 pages) | 0.014 | 0.848 | 682 |
| 3×24 inch receipt strip | 0.054 | 0.891 | 2565 |
| ruled forms / tables | 0.063 | 0.744 | 679 |
| rotated PDFs (/Rotate 90/180/270) | 0.082 | 0.514 | 874 |
| A0 drawing at 300 dpi | 0.151 | 0.868 | 29978 |
| technical drawings | 0.170 | 0.855 | 793 |
| thermal receipts | 0.188 | 0.882 | 390 |

Skew, inversion, aging, fax dithering, low resolution and multi-page containers
are, for practical purposes, solved. What is left is **layout**, not character
recognition — see below.

## By language

| language | files | mean CER | word recall |
|---|---:|---:|---:|
| Polish | 8 | 0.009 | 0.897 |
| English | 14 | 0.011 | 0.906 |
| French | 8 | 0.011 | 0.906 |
| Czech | 4 | 0.019 | 0.856 |
| German | 58 | 0.053 | 0.862 |
| Japanese | 4 | 0.065 | 0.328 |
| Greek | 4 | 0.191 | 0.438 |

Reading these correctly matters:

- **German's 0.053** is not a German problem — the German files include the
  receipts, forms and drawings, which is where the layout losses live. On clean
  German renders CER is 0.001.
- **Japanese word recall (0.328) is meaningless**: Japanese does not separate
  words with spaces, so a whitespace-based word metric cannot work. Its CER of
  0.065 is the number to read.
- **Greek is genuinely the weakest** — 0.191, and recall 0.438. Lookalike letters
  (`Α`/`A`, `Ο`/`O`, `Ρ`/`P`) and accented vowels defeat the shared charset. If
  Greek is your main language, measure first.

## Where it is weak, and why

### Order, not characters

Receipts (CER 0.188) keep a **word recall of 0.882**: the words are all there and
almost all spelled right, but they come out in a different sequence than a human
reads them. A thermal receipt puts the item on the left and the price on the
right; whether that is one line or two is a judgement call, and the engine's
geometric answer sometimes differs from the ground truth's.

The same applies to forms (0.063 / recall 0.744) and drawings (0.170 / recall
0.855), where labels are scattered across a sheet.

The A0 sheet is the same story pointing the other way: **0.151** with word recall
0.868, up from 0.042 before boxes were assembled into lines. Its title block is
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
| Eight page workers slower than one | 0.79× | 1.38× |
| corpus mean | 0.118 | 0.045 |

Then a second pass, assembling lines from boxes: receipts 0.332 → 0.188, forms
0.156 → 0.063, the worst drawing 0.433 → 0.206, and the A0 sheet 0.042 → 0.151 as
described above. Clean pages, newspapers, faxes and multi-page scans did not
move.

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
