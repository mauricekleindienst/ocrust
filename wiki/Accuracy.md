# Accuracy

Every number here comes from the generated corpus — 132 files, 217 pages, all
with exact ground truth — and can be reproduced in about three minutes
([Evaluation](Evaluation.md)). Nothing is cherry-picked, including the bad rows.
The figures cover the 118 files in a language the bundled model can actually
write; the four Greek and four Vietnamese pages are scored separately, for the
reason below.

## Overall

| metric | value |
|---|---|
| mean character error rate (CER) | **0.026** |
| median CER | **0.003** |
| mean word error rate (WER) | 0.083 |
| mean word recall | 0.927 |
| unexpected failures | **0** |

The gap between mean and median is the whole story: most documents are read
almost perfectly, and a handful of layout-heavy ones drag the mean up.

A CER of 0.006 means about one wrong character in 170 — for scanned German text,
roughly one typo every four lines.

## By document type

| category | mean CER | word recall | ms/page |
|---|---:|---:|---:|
| newspaper, 2 and 3 columns | 0.000 | 1.000 | 585 |
| skewed (−7°…+6°) | 0.000 | 1.000 | 773 |
| 4×24 inch receipt strip | 0.000 | 1.000 | 3216 |
| every raster format (8) | 0.001 | 0.977 | 699 |
| born-digital PDF | 0.002 | 0.990 | 615 |
| multi-page TIFF (47 pages) | 0.003 | 0.983 | 636 |
| dark mode / inverted | 0.003 | 0.980 | 670 |
| multi-page PDF (47 pages) | 0.004 | 0.957 | 681 |
| aged, stained, bled-through | 0.006 | 0.937 | 714 |
| 60 dpi thumbnail | 0.006 | 0.889 | 433 |
| 1-bit fax with dropout | 0.008 | 0.939 | 576 |
| unruled full-page price list | 0.013 | 0.977 | 506 |
| image-only PDF, clean render | 0.042 | 0.792 | 693 |
| rotated PDFs (/Rotate 90/180/270) | 0.054 | 0.884 | 919 |
| ruled forms / tables | 0.063 | 0.744 | 648 |
| A0 drawing at 300 dpi | 0.147 | 0.895 | 28309 |
| technical drawings | 0.170 | 0.855 | 602 |
| thermal receipts | 0.183 | 0.941 | 339 |

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

Reading these correctly matters:

- **German's 0.049** is not a German problem — the German files include the
  receipts, forms and drawings, which is where the layout losses live. On clean
  German renders CER is 0.001.
- **Japanese word recall (0.328) is meaningless**: Japanese does not separate
  words with spaces, so a whitespace-based word metric cannot work. Its CER of
  0.065 is the number to read.
- **Greek is not in the table any more**, and that is the finding. It scored
  0.180 with recall 0.536 while reporting confidence 0.93 — because a fifth of
  the language is unwritable with the bundled charset, not because the pages were
  hard. Greek is no longer offered as covered; `lang="el"` now fails naming the
  21 characters it cannot emit. The four pages stay in the corpus, scored in
  their own section of the [evaluation report](../docs/evaluation-report.md).

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

Reading order is geometric: an XY-cut that looks for columns before rows, joins
the boxes that share a baseline into one line, and reads a table's row across its
cells. It reads the corpus's two- and three-column pages exactly (CER 0.000).
Where a document's own structure is ambiguous the answer is too, and a title
block is exactly that. The structure itself does not depend on the order the text
came out in — it is recovered separately, below.

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

Then a fourth: **columns are looked for before rows**, and a row of wide cells is
not a column break. Three-column pages went 0.003 → 0.000, two-column pages from
being read straight across the gutter to 0.000, and a page that is nothing but a
price list from two columns to four. Every document that was already in the
corpus reads exactly as it did — the mean moved 0.040 → 0.036 because the corpus
grew by the layouts it had been missing, which is also why these three bugs
lasted as long as they did. (It reads 0.026 now because Greek and Vietnamese,
which the bundle cannot write, no longer count toward it, and because the
orientation classifier stopped turning single lines over — see below.)

## Robustness

### A fixture that was not what it said

The corpus has had a `fax` category since the beginning — "1-bit dithered fax at
half resolution", the format a scanned archive actually stores. It was saved as
24-bit RGB. The generator dithered the *look* of bilevel and then converted back,
so no file in the corpus was ever a real 1-bit image, and this was the result of
handing the reader one:

```text
unsupported input: TIFF page 1: unsupported pixel layout (Gray(1))
```

Every bilevel TIFF failed — Group 4, LZW, uncompressed, one page or many. Packed
rows arrive eight pixels to the byte and were measured as though there were one
byte each, so the length check rejected them. The same bits as a PNG read fine,
which is why it looked like a format nobody used rather than a bug.

The fax fixtures are now genuinely mode-1 and CCITT-coded, there are three more
bilevel files in `formats/`, and the reader unpacks 1-, 2- and 4-bit grey
honouring `PhotometricInterpretation` — a fax is usually WhiteIsZero, and
reading that tag wrong inverts the page. The size difference is the reason the
format exists: the same page is 6 KB bilevel against 11.6 MB as RGB.

### Specks on a fax

At fax resolution (100 dpi) a heading is 14 pixels tall, and a page covered in
lone black specks — dirty glass, a noisy line — made the detector drop whole
lines. On a probe page with 4 % of its pixels flipped, both the header and the
footer line went missing, together with half the body text. A 3×3 median filter
brings them back, and also erodes the one-pixel strokes that resolution is made
of.

What runs now touches only pixels whose eight neighbours are all of the other
colour, and only on a page where such pixels make up at least 0.1 %: fax pages
carry 2.5 %, every other page in the corpus at most 0.012 %, so everything else
stays byte-identical. The fax category went from CER 0.014 to **0.008** and word
recall from 0.867 to **0.939**; no other category moved.

### One line, turned over on its own

A page that arrives upside down is fixed by the orientation classifier, which
asks the `cls` model whether a line crop reads the right way up. It samples eight
crops and, when they agree, applies their verdict to the page — but when the
sample *disagreed* it used to fall back to deciding line by line. On a
`/Rotate 90` PDF that made one line of eleven disagree with its neighbours, and
it was turned over on its own:

```text
truth:  Zahlbar innerhalb 30 Tagen ohne Abzug.
got:    ahz n   h ug
```

Detection was never wrong — the box matched the one on the page that read
correctly to within four pixels — and that line's confidence dropped to 0.67
while the ten around it stayed above 0.98, so the page average of 0.963 hid it.
A page is upside down as a whole, so the verdict is now the page's: when the
sample disagrees every crop is scored and the majority decides for all of them.
Rotated PDFs went from CER 0.054 to **0.002** as a category, `/Rotate 90` and
`/Rotate 180` now read character for character, and the corpus median moved
0.006 → 0.003.


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

## Columns

A page's columns are read one after another, and they are looked for **before**
the page is cut into rows. That order matters: a page set on one baseline grid
has white space between every pair of lines, so cutting by rows first divides the
columns into fragments and reads each row straight across the gutter — which
turns two columns of prose into a line that says
`Der Stadtrat hat beschlossen, Anwohner fordern mehr Raum`.

A corridor is only taken for a column break when it looks like one:

- it runs the full height of the region and is wide — at least two line heights,
  and 3.5% of the width;
- it divides a region that covers most of the page's width. Columns split the
  page; a drawing's title block is labels down one side and values down the
  other, and reading it that way is not reading it;
- every slice it leaves is a column of text: three baselines at least, and one
  box on each. A table's row puts a box in every cell, which is what tells a
  price list's name column from a page column even when both are wide;
- the slices are near enough the same width. A page's columns are; a label and
  the value beside it are not.

Two corridors are tried together before three, so a three-column page is found as
three columns rather than rejected as a bad two-way split.

## Tables

A block whose cells line up into columns comes back as a table: `Page.tables`,
`Document.tables`, cells with their row, column and box, a Markdown pipe table
from `render("markdown")` and `Table.to_csv()`.

There is no table model and no ruling lines are read. Two things say where the
cells are. The detector returns one box per cell when the columns are far enough
apart, and the layout keeps those boxes on the row it joins them into
(`Line.segments`). Inside a box, the recognizer's own word positions show the
gaps. Either way a cell is a run of words, and a column is a stretch of the page
that row after row puts ink in.

**How wide a gap has to be is measured, not assumed.** Word spaces and column
gutters are both gaps, and how wide each is depends entirely on the document:

| corpus document | word spaces | column gutters |
|---|---:|---:|
| receipt | 0.71–0.87 × text height | 2.47–4.98 |
| dense invoice | 0.33–0.66 | 0.83–2.18 |
| a page of prose | 0.50–0.84 | none |

No multiple of the text height sits between both pairs, so the threshold is twice
the quarter-point of every word gap on the page. A page with no table has all its
gaps within a factor of two of that point — the corpus's degraded scans run from
18 to 36 pixels and nothing else — so nothing is split and no table is found,
which is the right answer.

**A page that is nothing but a table** has no prose to measure: its cells hold a
word each, so almost every gap on it is a column gap and the quarter point lands
among them. A nine-row price list came back as two columns instead of four. There
the gaps fall into two groups — word spaces well below, gutters well above — and
the threshold is taken from the widest jump between them. Prose has no such jump,
its gaps crowd together, and there the quarter point stands. A jump can only
lower the threshold, never raise it, so it finds cells and never loses them.

**What keeps prose out** is not the threshold but three guardrails, each of which
a real document passes and a page of sentences does not:

- three rows and two columns, at least;
- the gaps have to fall in the same places row after row — one gap in one place
  is where a sentence happened to be split;
- most rows have to carry more than one cell. Lines of similar length leave their
  last word a little further out on every line, and three of those look like a
  right-hand column; what gives them away is the lines between them carrying no
  cells at all.

Over the 114 documents of the corpus that finds 24 tables: the four ruled forms
as 5×4, the four receipts as 8×2 (items and totals are one column structure), the
eight engineering drawings' title blocks as 5×2 — `TITEL`, `WERKSTOFF`,
`MASSSTAB`, `ZEICHNUNGS-NR` and the rest, which is a table and reads like one —
and the eight unruled price lists as 9×4. Nothing at all in the newspapers, the
letters, the screenshots, the faxes, the clean pages or the degraded scans.

**What it does not do.** It does not read row spans: a cell belongs to the row its
baseline is on. It does not see a header cell that spans two columns and has
further header cells beneath it. And a table whose columns are separated by less
than twice its own word spacing is read as text, because at that point nothing in
the geometry distinguishes it from a paragraph.

## Which number to trust

`confidence` and `quality` answer different questions, and the difference is
measurable. Over the 112 ground-truth files of the corpus:

| | ranks pages by error rate | range across the corpus |
|---|---:|---|
| `doc.confidence` | Spearman −0.47 | 0.916 … 0.997 |
| `doc.quality` | **Spearman −0.71** | 0.893 … 0.988 |

**`confidence` is the recognizer's certainty about the characters it emitted.**
It is good at that and badly suited to anything else. Per line it is nearly
exact: a line's mean character probability lands within 1.2% of the share of
that line's text that is actually right, and only 49 of 2027 lines in the corpus
fall below 95% precision. But a page's error rate is made of something the
recognizer never sees — text the detector missed, a column read out of order, a
label broken into fragments — so the worst page in the corpus (CER 0.206) is
reported at 98.8% confident, above the median.

**`quality` estimates how much of a page is right.** It is fitted from the shape
of the output: characters per line, the tenth-percentile line confidence, mean
line height relative to the page, mean confidence and the weakest line's margin.
Held out a third of the files twenty times, it ranked better than confidence on
16 splits out of 20.

```python
doc = ocrust.scan("scan.tiff")
if doc.quality is not None and doc.quality < 0.96:
    queue_for_human_review(doc)          # catches every bad page in the corpus
```

On the corpus, that threshold flags 41 of 112 pages, and among them all 18 with a
character error rate of 10% or worse. It is a wide net, and the width is a known
bias: the estimate reads short lines as fragmentation, so a page of two columns
of six short lines is marked down even where every character on it is right.
Tighten it to 0.97 for 57 flagged, or loosen it to 0.94 for the obvious failures
only — 24 flagged, and one of the 18 bad pages slips through.

`confidence` stays the right number for dropping junk *lines* — it is what
`drop_score` and `--min-confidence` filter on:

```python
for line in doc.lines:
    if line.confidence < 0.7:
        highlight(line.box)
```

`quality` is `None` for a page with fewer than five lines. Every file the
weights were fitted on carries at least that many, so a shorter page would be
extrapolation — and the features that matter most, how long the lines are and
how tall they stand relative to the page, read a two-line letter in large print
as a fragmented drawing. Saying nothing is the honest answer where there is no
evidence; the command line then prints the page and line counts and no quality
figure.

The estimate is narrow: 0.893 to 0.987 over the corpus. It ranks pages well and
it does not claim to say "this page is 20% wrong". Widening it needs more ground
truth than 118 files. Re-fit it for your own documents with
`scripts/collect_quality.py` and `scripts/fit_quality.py`, which print their own
held-out numbers.

## Measuring your own documents

Averages over someone else's corpus do not bind your invoices. The evaluator
works on any folder that carries ground-truth text files:

```bash
python scripts/evaluate_corpus.py /path/to/your/corpus -o report
```

See [Evaluation](Evaluation.md) for the layout it expects and the metrics it reports.
