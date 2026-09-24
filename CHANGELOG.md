# Changelog

## 0.2.7 — 2026-09-24

Bugs found in 0.2.6 by an independent review, in rounds: each round's findings
were reproduced, fixed with a test that fails without the fix, and handed back
to be checked — including the fixes of the round before. Term-search accuracy
on the independent set is unchanged: 99.8 % precision, 99.8 % recall.

236 Rust unit tests, 10 Rust end-to-end tests, 366 Python tests.

### Fixed

- **An unreadable PDF could still end a batch.** A PDF, image, model or
  runtime error — and the panic 0.2.6 turned into an error — reached Python as a
  plain `RuntimeError`, which no per-file handler caught: `ocrust find` with
  `--pages`, a single file or standard input, `ocrust scan` of a batch, and
  `Ocr.scan_each` with `pages` stopped at the first such file. Every engine
  error of that kind is now an `OcrustError`, from Rust, so the file is
  reported unreadable and the batch goes on. The documented batch loops catch
  `OcrustError` beside `OSError` and `ValueError`.
- **`--resume` corrupted its own report.** It appended to the half-written last
  line an interruption leaves, gluing the next record onto it. The fragment is
  now cut off and its file read again; a last record that is whole but lacks
  its line end is kept. A file that is not a report of ocrust is refused and
  left untouched.
- **A resumed run's exit status ignored the report.** Files the earlier run had
  found something in, or could not read, now count.
- **The layout joined a suspended hyphen**: "Vor- / und Nachnamen" became
  "Vorund Nachnamen", "Sicherheits- / und Brandschutz" "Sicherheitsund", in
  every output format. A line-end hyphen followed by und, oder, sowie, bis,
  noch, wie, bzw., u. … and another word is kept, and so is one after a number
  ("2- / und 3-Zimmer"); "Kon- / to 12345" is still "Konto 12345".
- **A term was found in another word.** With the default `fuzzy`, `Adler` was
  found in `Radler`, `Sadler` and `Adlers`, `Radler` in `Adler`,
  `Hafenstraße 120` in `Hafenstraße 12` — one edit each — although the
  documentation promises whole words. A letter too many or too few at an edge,
  that no reading can place inside the word, now rules the hit out; for a
  number any digit too many or too few at its edge does. A misreading inside
  the word (`Opperation`) still counts, and so does a first or last letter the
  scan lost from a word longer than twelve letters.
- **A mark beside a word hid it**: `ORKA™` and `Sentinel X4™` were not found,
  since the Unicode fold turned the mark into letters glued to the word. ™, ®
  and ℃ now stand beside words; a raised or lowered digit is a digit set
  apart, so `Adler¹` is `Adler` with a footnote and `m²` still reads `m2`.
- **A regex ignored `whole_words`**: `KD-\d{6}` was found in `KD-1234567` and
  `XKD-123456`. A footnote mark after a match does not count as more word.
- **A reader that left early changed the verdict**: `ocrust find … | head -1`
  exited 0 when a file had tripped the gate, so a `set -o pipefail` gate
  passed — also when only standard error was cut off (`2>&1 | head`). The
  exit status is what was found.
- **An output that could not be written** ended `ocrust scan`, `pdf`, `ocr` and
  `tiff` with a traceback after all the OCR was done, and ended a `scan` batch.
  It is one line now: exit 2, or in a `scan` batch that file's failure while
  the rest go on. `tiff --sidecar` with a stream as `-o` is refused.
- **`case = true` let spellings and edits through**: `MUELLER` and `mueller`
  were hits for `Müller`, `ADLLER` for `Adler`. Case is now checked letter by
  letter along the alignment; an umlaut written decomposed (`u` + U+0308)
  counts as the umlaut.
- **`near` and `not_near` were judged in one reading order only**, the one the
  duplicate check kept; on a two-column page the order that puts `Codename`
  above `Falke` was thrown away. `near` now holds if either order puts the word
  beside the term, and `not_near` rules a hit out if either does. `not_near`
  also sees the word the hit is part of, but not the hit's own letters.
- **A word hyphenated at a line end**, which the layout joins into one line, was
  reported `exact` with a box around its second half only. It is `hyphenated`
  now, with a box on each line; `Document.search` matches carry the same in
  the new `Match.boxes`.
- **A regex missed a number broken after its own dash** (`KD-` / `123456`).
- **A malformed profile crashed** with a traceback and exit 1 instead of saying
  what is wrong: `[term]` for `[[term]]`, a list of strings for `term`, a JSON
  list or `null`, a profile that is not UTF-8. `markings` inside a term, and a
  category or name that is not a string, were accepted silently. All are errors
  with exit 2 now.
- **Two `--terms` files naming one term** counted every occurrence twice; it is
  an error, as within one profile.
- **`fuzzy = N` allowed fewer edits than documented**: "never more than a
  third" was "less than a third", so `fuzzy = 1` did nothing for a three-letter
  term.
- **`ocrust find --markings` did not default to `--fail-on any`** as documented.
  **A grade gate without `--markings`** (`--fail-on geheim`) could never trip;
  it turns markings on now. **A severity gate without terms** (`ocrust vs
  --fail-on high`) could never trip either; it is refused.
- **`-o` naming one of the inputs destroyed it** before it was read, in `ocrust
  find` and `ocrust scan` — also through a hard link; it is refused.
- **`-o` through a link replaced the link**, and `-o /dev/stdout` or a named pipe
  was replaced by a file — or, in `ocrust scan`, taken for a folder. The report
  is written through them now; standard output through the stream the process
  already has, so a shell's `>>` is appended to, not truncated. `ocrust scan`
  of several files into an existing plain file is refused instead of failing
  with a traceback.
- **`-o` naming a folder**, or a report that cannot be written, failed with a
  traceback after the whole scan; it is refused before scanning.
- **A shard with no files** left no report for `-f json` and `-f csv`.
- **Bad numbers** — `--workers -1`, `--max-pixels -1` or larger than any
  image, `--pages ,` — failed per file with exit 1, and `--threads 100000` was
  accepted and grew the process to gigabytes; all are exit 2 with the range now.
- `ss` read for `ß` was reported `exact`; it is a `spelling`.
- A phrase with `½` or `℃` in it was not found in identical text, and `case =
  true` was thrown off by such a character.
- `Ocr.scan_each` and `scan_many` read one path given as a string as a sequence
  of characters, and one numpy image as a sequence of rows; a generator of
  sources was read to its end before the first result; paths given as strings
  came back as `Path` objects.
- An inline `# comment` in a phrase list became part of the phrase, and a
  `near` or `not_near` word without a letter or digit (`€`) was found
  everywhere; it is an error now.
- A phrase joined across two columns the layout read as one line was `exact`;
  it is `split`.
- A box drawn backwards made the column reading order loop for ever.
- Help texts and pages still gave 4 as the default number of workers.

## 0.2.6 — 2026-09-23

Search profiles: `ocrust find` looks for whatever a profile names — code names,
people, companies, compound words, part and customer numbers — and finds it
however the scan broke it up. Large jobs use every core, split between
machines and survive an interruption.

235 Rust unit tests, 10 Rust end-to-end tests, 300 Python tests.

### Added

- **`ocrust find FILES --terms profil.toml`**, `Document.find(profile)` and
  `ocrust.terms`. A profile is TOML, JSON or one phrase per line; each term has
  phrases or a regular expression, a category, a severity (info … critical),
  its own fuzziness, case and word-boundary rule, the page zones it counts in
  (header, body, footer) and words it must or must not stand near. A mistake in
  a profile stops the run with the file, the term and what is wrong.
- **Found however broken.** A page is matched as one stream of letters and
  digits with every space, dash and line break taken out, so a word
  letter-spaced on a stamp, hyphenated at a line end, over two lines, two table
  cells or two columns, glued to the next or split by a stray space reads as the
  word; what was taken out is kept to hold hits to word boundaries (`Adler` is
  not in `Radler`) and to say per hit how it was broken. OCR look-alikes (`0`
  for `O`, `rn` for `m`) and `ae` for `ä` cost almost nothing, real edits count
  against the term's budget, and one- and two-letter words (an initial, a house
  number) must be read as written. A regex also sees a number broken over two
  lines or printed in a form's comb fields (`K D - 4 3 8 3 0 0`).
- **Columns.** Each page is also read column by column — cut where it is
  emptiest, between columns and between sections, again and again — so a phrase
  from the foot of one newsletter column to the head of the next, or wrapped
  inside a table cell, is found although the layout read the page a row at a
  time.
- **Reports and gates**: text, JSON, JSON Lines or CSV, every hit with page,
  boxes, zone, severity, how it was broken and a score. Exit status 3 when a
  file has a hit at or above `--fail-on` (a severity, `any`, `none`, or with
  `--markings` a grade; repeatable). `ocrust vs --terms` adds a profile to the
  classification scan.
- **`--shard K/N`** gives each of N runs a fixed part of the files, by a hash of
  the path below the walked folder, so N machines with a share mounted anywhere
  cover every file once; **`--resume`** continues an interrupted JSON Lines
  report, which is written as the run goes; **`--threads`** sets the threads a
  page worker uses.
- **`Ocr.scan_each(sources)`**: a parallel scan a chunk at a time, each input
  with its document or its exception, in input order — results arrive while the
  batch runs, memory is bounded, and an unreadable file no longer ends it.
- `scripts/make_terms_holdout.py` and `scripts/evaluate_terms.py`: a test set
  written independently of the matcher, and the measurement below.

### Improved

- **Every page worker has a model session of its own.** Sessions stopped at four
  whatever the number of workers, so on a sixteen-core machine sixteen workers
  queued for four sessions of one thread each. Now each worker has one and the
  cores are divided between them. Batches default to one worker per core, at
  most 16 (was 4). Measured on four cores only, where one process with four
  workers takes 45.3 s for 36 pages against 56.7 s with one.

### Fixed

- **One page could end a whole batch.** A table's header row could be adopted by
  the table below it while still belonging to the one above, and the two
  overlapping tables made the layout panic (`split index (is 5) should be <= len
  (is 4)`); in `scan_many` and every batch command that ended the run. A header
  row now goes to one table only, the layout skips any table run that overlaps
  another, and a panic anywhere in reading a document becomes that document's
  error, so the batch goes on.

### Measured

`scripts/evaluate_terms.py` on 84 files, 96 pages — letters, faxes, invoices,
minutes, e-mails, contracts, forms, slides, data sheets and two-column
newsletters, some blurred, faint, skewed, JPEG-compressed or stamped — with 411
expected hits of 34 terms and 62 decoys (`Hafenstraße 120` for `Hafenstraße
12`, `Kranichsteiner Straße` for `Kranich`):

| | precision | recall | files entirely right |
|---|---:|---:|---:|
| first run, blind | 98.7 % | 95.4 % | 64 |
| after the fixes it prompted | 99.8 % | 99.8 % | 82 |

The set was written by someone who had not seen the matcher; its first run is
the honest figure, the second is no longer blind. Still counted wrong: `über
orka` read as `überorka`, and a `NOX` 65 letters from its `not_near` word where
the profile's window is 50. Matching takes about 11 ms a page.

## 0.2.5 — 2026-09-23

Finding classified documents: `ocrust vs` and `Document.markings()` say which
files are marked VS-NUR FÜR DEN DIENSTGEBRAUCH, VS-VERTRAULICH, GEHEIM or STRENG
GEHEIM — or carry a NATO, EU, national, TLP or company marking — and tell a
grade stamped on a page from a sentence that mentions one. Two changes to
reading came out of it: speckled fax pages are cleaned, and a coloured stamp
across the text can be read by its colour.

231 Rust unit tests, 10 Rust end-to-end tests, 250 Python tests.

### Added

- **`ocrust vs`** scans files in parallel and reports per file the grade, the
  pages it is on, where on them (header, footer, stamp, form field, e-mail
  subject), anything else marked, and the pages of a marked document that
  carry no grade. Exit status 3 when a file is marked at or above `--fail-on`
  (default `vs-nfd`), else 1 when a file could not be read — its grade is
  unknown — else 0. Reports as text, JSON, JSON Lines or CSV, every finding
  with its page, box, confidence and the reason it was judged a marking.
- **`Document.markings()`** and `ocrust.markings.inspect(doc, file=path)`,
  the same in Python: a `MarkingReport` with the highest grade on a common 1–4
  scale, TLP and company markings beside it, and every finding as evidence.
  What it covers: the German grades in every spelling and OCR reading
  (`VS – NUR FÜR DEN DIENSTGEBRAUCH`, `Verschlußsache`, `G E H E I M`,
  `V5-NFD`, `GEHElM`); NATO, EU, Austria, Switzerland (in German, French and
  Italian), the US with its banner caveats, the UK, France and Italy; DDR and
  older German grades; TLP 1.0 and 2.0; company markings; "amtlich
  geheimgehalten", the classification-term line and the US classification
  block as lower bounds; explicit OFFEN and UNCLASSIFIED. Checked against VSA
  2023 Anlage IV, InfoSiG, ISG/ISV, IGI 1300, the EU and NATO policies and TLP
  2.0.
- **A marking is told from a mention.** A marking stands alone, in the header
  or footer band, or beside a page number, copy number or field label; a
  mention sits among a sentence's lowercase words, is negated, is part of a
  word ("VS-NfD-Zulassung"), sits in a table, continues a wrapped line, or is
  on a page listing three or more grades or stamped MUSTER. Ordinary words
  (GEHEIM, VERTRAULICH, INTERN) count only in capitals; VERTRAULICH and INTERN
  are resolved by the document's country.
- **The file's own label**: a Microsoft Information Protection sensitivity
  label in a file's metadata counts as a marking; a grade in the file name,
  which the VSA asks for, is reported as a hint.
- **`read_stamps=True`** reads a page's coloured ink a second time on its own.
  A red stamp across a paragraph was not read at all — the detector saw one
  tangle of strokes, and the lines it crossed came out garbled — and is now a
  block of kind `"stamp"`. A page without coloured ink costs about 1 % more (12
  ms), a page with a stamp about 13 %; `ocrust vs`
  turns it on.
- **`tick_boxes=True`** finds the tick boxes on a page and whether each is
  ticked, as blocks of kind `"tick_box"` reading `☒` or `☐`. The recognizer
  reads the words beside a box and hardly ever the box, so "☐ offen ☒ VS-NfD
  ☐ VS-VERTRAULICH" lost the choice; the grade beside the ticked box is now the
  marking and the others are options. About 40 ms a page; `ocrust vs` turns it
  on.
- `scripts/make_vs_corpus.py` and `scripts/evaluate_vs.py`: 186 generated
  files with ground truth, and the measurement below; `scripts/make_vs_holdout.py`,
  109 more, written independently of the detector to measure it honestly.

### Improved

- **Speckled pages are cleaned before detection.** At fax resolution a heading
  is 14 px tall, and specks on 2–4 % of a page made the detector drop whole
  lines, a probe page's header and footer marking among them. Only pixels whose
  eight neighbours are all of the other colour are replaced, and only on pages
  where they cover at least 0.1 %, so strokes are never thinned and clean pages
  stay byte-identical. Fax: CER 0.014 → **0.008**, word recall 0.867 →
  **0.939**; no other category moved. Over the 118 corpus files in a language
  the bundle can write: mean CER **0.026** (unchanged), WER 0.083 → 0.081, word
  recall 0.927 → **0.929**.

### Measured

Two generated test sets, measured with `scripts/evaluate_vs.py`:

| set | documents | precision | recall | documents entirely right |
|---|---:|---:|---:|---:|
| development corpus (the rules were written on it) | 186 | 100 % | 100 % | 186 |
| independent set, **before** the fixes it prompted | 109 | 95.8 % | 92.0 % | 101 |
| independent set, after them | 109 | 100 % | 100 % | 108 |

The independent set was written by someone who had not seen the detector —
new templates, new wording, printed e-mails, forms, slides, TV listings — to
measure it on documents it was not tuned for. Its first result is the honest
figure; the second is no longer blind. The one document still counted wrong
names its UNCLASSIFIED banner, which the set expected to go unnamed. After the
fixes every grade level on both sets is right and none of the 110 files without
a marking raises a grade; the detector adds under 10 ms a page to the OCR.

## 0.2.4 — 2026-09-23

Six bugs, two of which lost data without saying so. All six were reproduced
before they were fixed, and each has a test that fails against 0.2.3.
Recognition is untouched: accuracy over the corpus is identical to the digit.

222 Rust unit tests, 10 Rust end-to-end tests, 129 Python tests.

### Fixed

- **A folder scan overwrote its own output.** Output files were named after the
  input's file name alone, and a folder is walked recursively, so
  `archive/2024/rechnung.pdf` and `archive/2025/rechnung.pdf` both became
  `out/rechnung.txt`. The second replaced the first while the summary reported
  "done: 2 files". A walked folder is now mirrored — `out/2024/rechnung.txt`,
  `out/2025/rechnung.txt` — and files at the top of the folder or named on the
  command line stay flat as before. What mirroring cannot separate,
  `rechnung.pdf` beside `rechnung.png`, is refused by name before anything is
  read; the rest of the batch still runs, and the exit status is 1. `--watch`
  applies the same rule to everything in the watched folders, not only to what
  is new.

- **`--skip-existing` skipped documents that were never read.** With the
  collision above, the 2025 invoice was reported as "already written" and never
  scanned. And because outputs were written straight onto the target, an
  interrupted run left a truncated file that `--skip-existing` then treated as
  finished on every run after. Outputs are now written to a hidden temporary
  file and moved into place in one step: a file that exists is complete.

- **`ocrust ocr` on a password-protected PDF reported success.** It exited 0,
  printed "0 of 0 pages layered", and wrote a byte-for-byte copy of the input.
  lopdf does not refuse a document it cannot decrypt — it returns it still
  encrypted, every page unreadable — and zero pages took the path meant for a
  document whose pages all have text already. A locked PDF is now an error that
  says what to do, on every command, and a PDF with no readable page is never a
  pass-through.

- **A 9 KB file could take 1.8 GB of memory.** A mostly blank 12000 × 12000 CCITT
  TIFF did exactly that in 40 seconds, and the TIFF decoder's own limit let one
  fourteen times larger through. PDF pages were capped at 4000 pixels a side;
  image files had no cap at all. Images are now refused on the size they
  declare, before any of it is decoded — in 0.4 seconds rather than 40 — above a
  `max_pixels` limit that defaults to Pillow's decompression-bomb threshold
  (178 956 970) and so still admits an A0 sheet at 300 dpi.

- **A damaged PDF crashed the documented batch loop.** The error contract says
  an unreadable file raises `IOError` or `ValueError`, so
  `except (IOError, ValueError)` skips it; a PDF with a valid header and nothing
  after it raised a bare `RuntimeError` instead. Damaged, empty and locked PDFs
  are now `ValueError` from `scan` and `read`, like every other unreadable file.
  `ocr_pdf` still reports every failure as `OcrustError`, as it always has.

- **Search missed a decomposed query.** `search("Grüße")` found nothing when the
  query arrived with `ü` as `u` plus a combining diaeresis, as it does from
  macOS input and some PDF copy-paste. The query is normalized to composed
  Unicode first; recognized text already is.

### Added

- **`--password` and `password=`** for PDFs that need one to open, on `scan`,
  `pdf`, `ocr` and `tiff`. The `OCRUST_PASSWORD` environment variable does the
  same without leaving the password in shell history or the process list. A PDF
  protected by an owner password alone — the usual bank statement — opens
  without one, as it did. A text layer added to a locked PDF is written without
  the password, and `ocrust ocr` says so. The password never appears in a
  configuration's debug output.

- **`--max-pixels` and `max_pixels=`**, the limit above; `0` removes it.

## 0.2.3 — 2026-09-20

A converter, and the format work that made it worth having: a bilevel TIFF — the
way scanned archives and faxes are actually stored — could not be read at all
until this release, and four formats were offered that nothing could open.

Accuracy over the 118 corpus files in a language the bundle can write: mean CER
**0.026**, median 0.003, WER 0.083, word recall **0.927**.

218 Rust unit tests, 10 Rust end-to-end tests, 121 Python tests.

### Added

- **`ocrust pdf` converts anything readable into one searchable PDF.** It took a
  single file; it now takes files, folders and patterns together:

  ```bash
  ocrust pdf archive/ -o archive.pdf                  # a folder, recursively
  ocrust pdf fax.tiff photo.jpg old.pdf -o one.pdf    # mixed kinds, in order
  ```

  Images in any offered format, multi-page TIFFs and existing PDFs can be mixed
  in one command, and the pages come out in the order the inputs were given. Each
  page keeps its own size, computed from its pixels and `--dpi`, instead of being
  stretched onto a common sheet. A folder is walked recursively, sorted and
  filtered to readable suffixes, so the same folder gives the same PDF twice
  running. More than one input means `-o` is required, since where one file goes
  is not something to guess.

  Pages are compressed as they are scanned, the same as for a single file, so a
  hundred inputs cost the memory of one.

  It does not preserve an input PDF's vector content: every page is re-rendered,
  which is what lets different formats sit on equal footing. `ocrust ocr` remains
  the lossless path for a PDF whose pages must stay exactly as they are.

  New: `Engine::to_searchable_pdf_many` in Rust, `Ocr.searchable_pdf_many` and
  the module-level `ocrust.searchable_pdf_many` in Python, both returning the PDF
  and its page count.

### Fixed

- **A bilevel TIFF could not be read at all.** Mode-1 TIFF is what a scanned
  archive and every CCITT fax is stored as, and handing the reader one produced:

  ```text
  unsupported input: TIFF page 1: unsupported pixel layout (Gray(1))
  ```

  Group 4, LZW, uncompressed, one page or many — all of them. Packed rows arrive
  eight pixels to the byte and were measured as though there were one byte each,
  so the length check rejected them before anything was decoded. The same bits
  as a PNG read fine, which is what made it look like a format nobody used.

  The reader now unpacks 1-, 2- and 4-bit grey, honouring
  `PhotometricInterpretation`: a fax is usually WhiteIsZero, and reading that tag
  wrong inverts the page. A test decodes the same 16×4 page written both ways
  round and asserts the two come out pixel for pixel identical, which a decoder
  that ignored the tag — or inverted twice — would fail.

  It survived every release because the fixture lied. The corpus's `fax`
  category, "1-bit dithered fax at half resolution", was saved as 24-bit RGB:
  the generator dithered the look of bilevel and converted back. The fax
  fixtures are now genuinely mode-1 and CCITT-coded, and `formats/` carries
  three more bilevel files. The same page is 6 KB bilevel against 11.6 MB as
  RGB, which is the whole reason archives use it.

### Removed

- **Four formats that were advertised and could not be opened.** `dds`, `exr`,
  `ico` and `avif` are no longer offered. None of them was a missing feature
  flag: the DDS decoder takes no uncompressed surface and DXT wants dimensions
  in multiples of four (an A4 page at 200 dpi is 1654×2338), an ICO frame stored
  as PNG has to be RGBA so the common 256-pixel icon fails, and AVIF is not
  compiled in. `avif` was only ever in the Rust list, which is the other half of
  the problem.

### Changed

- **The list of readable suffixes has one home.** It was kept in three places —
  the Rust reader, the Python package and the CLI's directory filter — and they
  had already drifted. Python now derives it from the reader through
  `supported_suffixes()`, so a format cannot be offered by one and refused by
  another. The corpus carries a file in every suffix on the list, including the
  `.hdr` that Pillow cannot write and the generator now encodes itself.

  Accuracy over the 118 corpus files in a language the bundle can write: mean CER
  **0.026**, median 0.003, WER 0.083, word recall **0.927**.

## 0.2.2 — 2026-09-20

An honesty release. Nothing about how pages are read changed except for one bug,
and that bug is the reason the rest was found: chasing a single shredded line
through a rotated PDF meant auditing the language table, which turned out to be
claiming two languages the bundled model cannot write. `ocrust languages` now
reports 26 covered rather than 27, and the corpus figures cover only the
languages that are actually claimed.

Accuracy over the 108 files in a language the bundle can write: mean CER
**0.029**, median **0.003**, WER 0.089, word recall **0.922**. The median halved
against 0.2.1 because of the orientation fix; the mean moved because Greek and
Vietnamese stopped being averaged in with the languages that work.

216 Rust unit tests, 8 Rust end-to-end tests, 116 Python tests.

### Fixed

- **A single line was turned upside down on a page that was not.** The
  orientation classifier samples eight line crops and, when they agree, applies
  their verdict to the whole page. When the sample *disagreed* it fell back to
  deciding crop by crop — and one line of eleven on a `/Rotate 90` PDF then
  disagreed with its neighbours and was rotated alone:

  ```text
  truth:  Zahlbar innerhalb 30 Tagen ohne Abzug.
  got:    ahz n   h ug
  ```

  Detection was never at fault: the box matched the one on the page that read
  correctly to within four pixels, and German is fully covered by the charset.
  The line's own confidence fell to 0.67 while the ten around it stayed above
  0.98, so the page average of 0.963 hid it.

  A page is upside down as a whole, which is what the module always documented,
  so the verdict is now the page's: when the sample disagrees every crop is
  scored and the majority decides for all of them, with an even split going to
  the stronger evidence. `/Rotate 90` and `/Rotate 180` pages now read character
  for character, the rotated-PDF category went from CER 0.054 to **0.002**, and
  the corpus median moved 0.006 → 0.003.

- **Alphabets that named one case of a letter but not the other.** `Ả` hid behind
  `ả` in Vietnamese, `Ĺ Ŕ` behind `ĺ ŕ` in Slovak, `Í Î Ó Ú` behind their
  lowercase in Italian, and Serbian's capital `Џ` was a second `Д`. Estonian
  never declared `š ž` at all. Only the Vietnamese one was hiding a real gap in
  the bundle, but text set in the undeclared case walked past the check in every
  case. A test now derives the pairs and fails on the next one.

### Removed

- **Greek is no longer claimed as a supported language.** `ocrust languages` now
  reports 26 covered, not 27, and `lang="el"` fails naming the characters the
  model cannot emit.

  The bundled PP-OCRv6 charset holds all 24 plain Greek letters in both cases and
  not one accented vowel — no `ά έ ή ί ό ύ ώ`, no dialytika, and no final sigma
  `ς`. Greek cannot be written without them: every polysyllabic word carries an
  accent, and `ς` ends a large share of them. What came out instead was
  accent-stripped text with Latin lookalikes standing in, `Μηχανουργεία
  Θεσσαλονίκης` as `Mηχανουργεα Θεσσαλονη` and `Σύνολο` as `Σúνoλo`, at CER 0.180
  and a reported confidence of 0.93 — the exact failure the language check exists
  to prevent, waved through because the check was asked the wrong question.

  The language table declared Greek as those 48 plain letters, so the coverage
  check found nothing missing. It now declares monotonic Greek in full, 69
  characters, of which the bundle has 48: coverage comes out at 70%, below the
  threshold for a near miss, and Greek moves to where Russian and Korean already
  were — known, refused with its missing characters listed, and covered again by
  itself the moment a Greek-capable recognizer is installed. A unit test pins
  that the plain letters alone do not count as coverage.

- **Vietnamese is no longer offered as a near miss.** It was reported at "98%,
  missing `ạ ả`" — measured against 21 of the 146 letter forms the language
  actually has. Quốc ngữ is 12 vowel letters × 5 tones in both cases, and the
  bundle carries 58 of them: no tone mark on any circumflex, breve or horn vowel,
  so `ấ ầ ẩ ẫ ậ ắ ằ ẳ ẵ ặ ế ề ể ễ ệ ố ồ ổ ỗ ộ ớ ờ ở ỡ ợ ứ ừ ử ữ ự` and their
  capitals are all absent. The marks are the language — `phai`, `phái` and
  `phải` are three words — and the vowel was dropped outright rather than
  substituted: `Phải trả` came back as `Phi tr`, `Tổng cộng` as `Tng cng`, at
  confidence 0.98. The entry now declares the alphabet, which puts Vietnamese at
  55%, and `lang="vi"` refuses naming all 88 characters instead of two.

  Accuracy figures now cover the 108 corpus files in a language the bundle can
  write: mean CER **0.029**, median 0.003, WER 0.089, word recall **0.922**. No
  document reads differently for this reason — the Greek and Vietnamese pages
  stay in the corpus and are scored in a section of their own in the evaluation
  report, so the cost of the gap stays on the record instead of being averaged
  into the languages that work.

### Added

- **Characters a language accepts but does not require are now reported instead
  of ignored.** `ẞ` is absent from the bundle, and German stays covered: the
  canonical uppercase of `ß` is `SS`, `ẞ` has only been permitted since 2017, and
  what the recognizer returns for `STRAẞE` is `STRAßE` — the right letters with
  one in the wrong case, not a corrupted word. Requiring it would have refused
  German over a character German obliges nobody to use, so `ocrust languages`
  says so instead:

  ```console
  covered, with a substitution (the language does not require these):
    de (German): no ẞ
  ```

  New: `Ocr.optional_language_gaps()` in Python, `Engine::optional_language_gaps`
  in Rust, and `Coverage::missing_optional`, which never counts against coverage.

## 0.2.1 — 2026-09-19

Three layout bugs, all found by probing pages the corpus did not have.
Accuracy over the documents that were already there is unchanged file for file;
the corpus itself grew by the shapes that were missing, and over all 112 files
with ground truth it now reads mean CER 0.036, median 0.006, WER 0.107 and word
recall 0.904.

### Fixed

- **A page laid out in columns was read across them** when its columns sat on one
  baseline grid — a newspaper, a journal paper, anything set on a leading that
  leaves white space between every pair of lines. The XY-cut took the horizontal
  gap first, which cut the columns into rows, and the two columns of each row
  were then joined into one line: `Der Stadtrat hat beschlossen, Anwohner fordern
  mehr Raum`. Columns are now looked for before rows, and only where they are a
  property of the page: a wide corridor that runs the height of the region, with
  a column of text on either side of it — several baselines tall, one box on each
  — and columns of near enough the same width. A drawing's title block is two
  such sides and is not two columns, so a corridor has to divide a region that
  covers most of the page's width. Three-column and two-column corpus pages now
  read at CER 0.000; they read at 0.003 and as nonsense before.
- **A table's rows were split into columns** when its cells were wide enough that
  neither side of a gutter looked narrow: a price list came back as every article
  name, then every figure. What says the gutter runs through the rows rather than
  between columns is that one side puts several boxes on each baseline, which is
  a row of cells and never a column of text.
- **A page that is nothing but a table measured its own gutters as word spaces.**
  Its cells hold a word each, so almost every gap on the page is a column gap and
  even the quarter point lands among them; a nine-row price list came back as two
  columns instead of four. Where the gaps fall into two groups — word spaces well
  below, gutters well above — the threshold is now taken from the stretch between
  them, and the quarter point stands in only where there is no such stretch,
  which is what a page of prose looks like. The highest stretch wins, not the
  widest: a heading whose two words sit further apart than the rows beneath it
  opens one of its own, and a threshold taken from that cuts the heading into
  cells and hands it to the table as a header row. The estimate is never widened
  by this either, so no table that was found before is lost: the corpus still
  finds the same 16, plus the 8 new price lists as 9×4.

### Changed

- The corpus generator's `columns` option now spreads the lines over the columns
  instead of filling each to the bottom of the page, so a page asked for in three
  columns is in three columns. It was not, which is why nothing in the corpus
  exercised column layout and why the bug above survived. It also refuses to draw
  a line wider than its column — that caught `extremes/long_receipt.png`, whose
  invoice lines ran off the edge of the strip while its ground truth claimed the
  text was there (CER 0.054 → 0.000 once the strip is wide enough for them).
- New corpus fixtures for the shapes that were missing: an unruled full-page
  price list (PNG and PDF) and a two-column page set on one baseline grid.
- `scripts/collect_quality.py` matched a recognized line against at most three
  ground-truth lines in a row. The layout joins a table row into one line, so a
  five-column form's rows scored as though most of each row had been invented.
  Matching runs of up to five puts the gap between a line's confidence and its
  measured precision at 1.2% over the corpus rather than 2.2%, and at 1.4% rather
  than 12.2% over the forms.
- A CLI test built an engine it expected to fail, which it did not on a machine
  with `OCRUST_MODELS_DIR` set — the model search falls back to the environment.
  The test now clears it.

## 0.2.0 — 2026-09-19

Accuracy is unchanged throughout: the corpus run reproduces mean CER 0.040,
median 0.006, WER 0.120 and word recall 0.892 file for file, and a drawing's
recognized text is byte-identical to 0.1.0's. What is new reads the same
characters and does more with them; what changed is memory and output size; and
every fix below is a bug that produced a wrong box, a wrong page, a crash or a
build that would not compile.

### Changed

- **Peak memory no longer grows with the length of a document.** `ingest` used to
  rasterize every page into one list before the first line was recognized, so a
  scan cost about 10 MB per page: measured on a synthetic A4 invoice at 200 dpi,
  413 MB for one page, 999 MB for 40 and 1811 MB for 120, which is more than a
  laptop has by page 400. Pages are now decoded on demand and dropped as they are
  read — a PDF is parsed once and rendered page by page, sharing one render cache;
  a TIFF's directories are walked forward; the searchable-PDF path compresses each
  page as it is scanned and keeps the JPEG; the TIFF path encodes each page into
  the archive and lets it go. The same three scans now cost 233 MB, 249 MB and
  289 MB, and the PDF and TIFF conversions 260 MB each.

  ONNX Runtime is also told not to keep an allocation arena or plan tensor reuse.
  Both assume the tensor shapes repeat; pages are all different sizes, so the
  arena accumulates blocks it never reuses. That is the difference between 584 MB
  and 249 MB over a 40-page scan, for about 10% more time at one page worker and
  none at four (where it is 935 MB against 2010 MB). `Ocr(memory="fast")` or
  `--memory fast` buys the time back. Recognized text is identical either way.

- **`ocrust tiff` compresses by default** (LZW, understood by every TIFF reader
  since 1992). A 40-page colour archive was 442 MB and is now 3.7 MB, pixel for
  pixel identical. `--compression deflate` packs a little tighter,
  `--compression none` restores the old behaviour.

### Added

- **Tables.** A block whose cells line up into columns comes back as a table:
  `Page.tables`, `Document.tables`, `Block.table`, cells carrying their row,
  column, span, box and confidence, a Markdown pipe table from
  `render("markdown")`, and `Table.to_csv()` / `.as_rows()` / `.row_text()`. No
  table model and no ruling lines are read, so the wheel stays one
  self-contained artifact. Two things say where the cells are: the detector
  returns one box per cell when the columns are far enough apart, and the layout
  now keeps those boxes on the row it joins them into (`Line.segments`); inside a
  box, the recognizer's word positions show the gaps. A column is then a stretch
  of the page that row after row puts ink in.

  **How wide a gap has to be is measured rather than assumed**, because word
  spaces and column gutters are both gaps and their widths depend entirely on the
  document: a corpus receipt's word spaces run to 0.87 of the text height while
  its narrowest gutter is 2.5 times it, and a dense invoice has spaces at 0.66 and
  gutters from 0.83. No multiple of the text height sits between both pairs, so
  the threshold is twice the quarter-point of every word gap on the page. A page
  with no table has all its gaps within a factor of two of that point, so nothing
  is split and no table is found.

  Three guardrails keep prose out: three rows and two columns at least, gaps that
  fall in the same places row after row, and most rows carrying more than one
  cell. Over the 102-file evaluation corpus that finds 16 tables — the four ruled
  forms as 5x4, the four receipts as 8x2, the eight engineering drawings' title
  blocks as 5x2 — and nothing at all in the newspapers, letters, screenshots,
  faxes, clean pages or degraded scans. It does not read row spans, and a table
  whose columns are closer than twice its own word spacing is read as text.
  `Ocr(tables=False)` / `--no-tables` turns it off.

- **Batch ergonomics.** `--skip-existing` leaves inputs whose output file is
  already there, comparing against the file the run *would* write so a text pass
  does not make a later `-f markdown` pass think it is finished — a batch
  interrupted at file 300 of 400 picks up where it stopped. `-` reads one document
  from stdin, for `curl … | ocrust scan -`. `--watch` keeps running and scans
  whatever appears in the input directories, waiting until a file stops growing
  because half a PDF is not a PDF. And `ocrust completions bash|zsh|fish|powershell`
  prints a completion script generated from the argument parser itself, so it
  lists the flags this version has rather than the ones it had when someone last
  remembered; zsh and fish also get each flag's help text and the value sets for
  `--format`, `--compression` and the rest.

- **`Page.quality` and `Document.quality`: an estimate of how much of a document
  is right.** `confidence` answers a narrower question than people read into it.
  Over the evaluation corpus it spans 0.916 to 0.997 while the character error
  rate spans 0 to 0.206, and the worst page in the set is reported at 98.8% —
  above the median. That is not the recognizer being wrong: per line its
  confidence lands within 1.4% of the share of that line's text that is actually
  right. A page's error rate is simply made of what recognition never sees — text
  the detector missed, a column read out of order, a label broken into fragments.
  The new estimate is fitted from the shape of the output (characters per line,
  the tenth-percentile line confidence, mean line height relative to the page,
  mean confidence, the weakest line's margin) against 100 ground-truth files:
  **Spearman −0.75 against the error rate, where confidence manages −0.46**,
  better on 20 of 20 held-out splits, and it catches all 18 pages with a 10% or
  worse error rate when flagging below 0.96. `confidence` keeps its meaning and
  its callers — `drop_score` and `--min-confidence` still filter lines on the
  recognizer's own score. `Line.margin` exposes the runner-up distance the
  estimate is partly built from, and `scripts/collect_quality.py` with
  `scripts/fit_quality.py` re-fit the weights for another corpus. It is `None`
  for a page with fewer than five lines: every file it was fitted on has at
  least that many, and a two-line letter in large print reads to the features
  like a fragmented drawing, so the honest answer there is nothing at all.

- **A command line that reads like one.** Rust orange (`#F74C00`) is the accent —
  what was written, the `done:` total, the version, the language scripts — with
  green, amber and red kept for what they mean: how much a number can be trusted.
  Colour is used sparingly, switched off for a pipe (`NO_COLOR` honoured,
  `FORCE_COLOR` respected), and stepped down through 256 and 16 colours for
  terminals that cannot do 24-bit. A red `ocrust:` on errors, dim labels in
  `doctor`, `languages` and `models`, and long paths folded on their separators
  so a diagnostic never runs off the screen. Counts in English (`1 page, 2 lines`, not
  `1 page(s)`), durations that turn into seconds and minutes when they should,
  sizes in kB or MB, a `done:` total after a batch, a hint when `ocr` skipped
  every page because it already had text, long messages folded to the terminal
  width, and `--help` that ends with the seven commands people actually type.

### Fixed

- **GPU builds did not compile.** `--features cuda`, `coreml` or `directml`
  failed with *could not find `CUDAExecutionProvider` in `ep`*: the provider
  types are `ort::ep::CUDA`, `CoreML` and `DirectML` in the ONNX Runtime binding
  this release pins. Nothing in CI built them, so nothing noticed — the workflow
  now runs `cargo check --all-features`.
- **`tensorrt`, `rocm`, `openvino` and `webgpu` did nothing.** The features
  existed and pulled in the provider, but no code ever registered it. `device="auto"`
  now offers every provider the build was compiled with, TensorRT first because it
  falls back to CUDA by itself.
- **A page filter that matches nothing is an error.** `scan(pdf, pages=[99])` on a
  three-page document returned an empty document, which reads exactly like a blank
  scan; it now says *the document has no page 100*. An in-memory image honours
  `pages` as well, instead of ignoring the filter.
- **Word boxes follow the line.** Character positions were mapped onto the
  *bounding box* of the detection polygon, so on a tilted line every word got a
  box as tall as the whole line and offset along it. They are now interpolated
  along the quad's own edges. Vertical lines — whose crop the recognizer reads
  standing up — had their words spread left to right across the page instead of
  bottom to top, and a crop the orientation classifier turned around had them
  mirrored to the wrong end of the line.
- **Markdown no longer eats a minus sign.** A list item's leading `-` was trimmed
  unconditionally, so `- Gutschrift` and `-19,90 EUR` in one block turned a credit
  into a charge. A `-` counts as a bullet only when a space follows it, and the
  exporter and the block classifier now share one definition of what a bullet is.
- **Deskewing could exceed its own limit.** The coarse angle search stepped in
  whole degrees without checking them against `max_skew_deg`, so a limit below one
  degree still allowed a one-degree rotation.
- **Recursive glob patterns are expanded.** `ocrust scan 'archive/**/*.pdf'` and
  `scan_many(["archive/**/*.pdf"])` matched nothing, because the pattern was
  anchored at its parent directory (`archive/**`) and only its last component was
  globbed. Both now glob from the last directory that is spelled out.
- **`--pages 1-x` is a message, not a traceback**: a malformed page spec exits
  with *'1-x' is not a page or a page range* — and with code 2, which the
  documented table reserves for a bad argument, rather than 1.
- **A de-hyphenated paragraph is no longer a heading.** Joining a word across a
  line break unions the two line boxes, and the result read as one line of twice
  the page's median height: short hyphenated paragraphs came out of the Markdown
  exporter as `## `. The classifier now measures the detection polygon, which
  de-hyphenation does not touch.
- **An engine that cannot be built printed a Python traceback.** `ocrust scan x
  --lang klingon`, a missing model directory, an unknown device: the error was
  raised while building the engine, which no command guarded, so the user got a
  stack trace instead of a sentence. One handler in `main` reports it as one line.
- **`--progress` garbled a log.** It rewrote its line with a carriage return
  whatever stderr was, so a redirected run came out as one long line. On a
  terminal it still rewrites in place; into a pipe or a file it prints a line per
  page.
- **`-q` works on `ocr`, `pdf` and `tiff`**, not only `scan`; the wiki's own
  `find | xargs -P4 ocrust ocr` recipe had no way to silence the per-file
  summary. A `--sidecar` file is written through the same retrying path as every
  other output, so a network share hiccup no longer fails it alone.
- **`ocrust scan book.pdf | head -1` no longer ends in a traceback.** A reader
  that closes the pipe early is its own business; the CLI flushes stdout while it
  can still see the error and exits quietly instead of raising `BrokenPipeError`,
  returning 1 and printing *Exception ignored* on the way out. `Ctrl-C` during a
  long scan prints *interrupted* and exits 130 rather than a stack trace.
- **`ocrust languages` crashed on a Windows console.** A redirected stdout there
  is cp1252, and the command prints the Vietnamese characters the model is
  missing: `UnicodeEncodeError: 'charmap' codec can't encode character '\u1ea1'`.
  Both output streams are now UTF-8 with unrepresentable characters replaced, so
  a diagnostic can no longer take the command down.
- **`lang="zh_hant"` silently meant Simplified Chinese.** A tag written with an
  underscore is now the same tag as one written with a hyphen, so it no longer
  falls through to its primary subtag.
- **`pages` was ignored for numpy and PIL input.** `scan(array, pages=[7])` read
  the array as page 1 and reported success; the selection is now honoured there
  too, as is `progress`.
- **`Match.page` is the page's own index**, so a hit found while scanning a subset
  of a PDF reports the page number of the source document. `Document.to_dict()` no
  longer raises on a document built in Python rather than by the engine.
- A recognition model whose output has no character classes, or a row of NaNs, is
  reported as a model error instead of panicking inside the decoder. A batch that
  comes back the wrong size is an error instead of silently empty lines, and a
  class the dictionary does not cover no longer widens the character before it.
- **`ocrust install-models` failed wherever a `GITHUB_TOKEN` exists.** The token
  is sent to GitHub hosts so that a private model repository works — but a token
  that does not cover *this* repository makes `raw.githubusercontent.com` answer
  404 for a public file, which is every GitHub Actions job and many shells. An
  authenticated download that fails is now retried without the token; the
  authenticated error is still the one reported when both fail. Verified end to
  end: 31.7 MB fetched and checksum-verified.
- Model manifests are validated before anything is written: a bundle or file name
  has to be a plain name (a `../` in one would have written outside the model
  cache), the checksum has to be 64 hex digits, and a download stops at the size
  the manifest declares instead of reading a mirror until memory runs out.
  Concurrent installs no longer share one `.part` scratch file.

## 0.1.0

First release.

### Engine

- Text detection (DB), 180° text-line orientation classification and CTC
  recognition from the PP-OCR family, run through ONNX Runtime.
- Bundled **PP-OCRv6** models: 18 708 character classes, complete coverage of 27
  languages.
- Preprocessing: EXIF orientation, auto-inversion of light-on-dark pages, skew
  estimation and correction, rescaling, optional denoise and contrast stretch.
- Layout: XY-cut reading order with column detection, paragraph grouping,
  de-hyphenation across line breaks, heading and list classification, per-word
  boxes derived from CTC character positions.

### Input

- Images: PNG, JPEG, WebP, BMP, GIF, PNM/PBM/PGM/PPM, TGA, DDS, HDR, OpenEXR,
  QOI, ICO. Formats without magic bytes are resolved from the file name.
- Multi-page TIFF (every page) and PDF (every page, rasterized by the pure-Rust
  `hayro` renderer).
- In memory: `bytes`, `numpy` arrays and PIL images.

### Output

- Plain text, Markdown, JSON with full geometry and confidences, hOCR, ALTO XML,
  CSV.
- Searchable PDF built from images.
- **OCR text layer added to an existing PDF**, preserving its pages, images and
  compression; pages that already contain text are skipped, `/Rotate` is handled
  and inherited resources are kept.
- Multi-page TIFF, colour or greyscale.

### Languages

- 35 languages known, with per-language alphabets cross-checked against
  PaddleOCR's dictionaries.
- `lang=` is a check, not a hint: building an engine fails when the recognition
  model cannot spell a requested language, naming the missing characters.
- `ocrust languages` reports covered and nearly covered languages.

### Line assembly and speed

- **Boxes on one baseline now become one line.** A detector returns boxes, not
  lines: a receipt's item and its right-aligned price are two boxes, and so are
  the cells of a table row. Joining them fixed receipts (CER 0.332 → 0.188),
  ruled forms (0.156 → 0.063) and drawings (0.197 on the worst sheet), while clean
  pages, newspapers, faxes and multi-page scans stayed exactly as they were. It
  costs one category: the A0 sheet's title block is now read row-wise, which its
  ground truth lists field by field (0.042 → 0.147, word recall 0.895) — see
  `docs/evaluation.md`.
- Preprocessing now **earns its keep**: because lines are assembled from
  baselines, deskewing decides whether a skewed page's line is assembled
  correctly. `preprocess=False` went from free to costing ten times the error rate
  on skewed, aged and inverted pages (CER 0.004 against 0.040).
- Whether a wide gap belongs to one row is decided by **repeating columns**: a
  table or price list puts cells at the same x positions row after row, a
  drawing's labels merely share a height. The decision is taken per region, so a
  drawing's title block can be tabular while the sheet around it is not.
- A column split now also requires **two lines on each side** and a side that is
  wide relative to the gutter, which is what separates a newspaper's columns from
  a table's cells.
- **Fewer calls per page**: the line orientation classifier asks about a sample of
  crops and applies a unanimous verdict to the rest instead of classifying every
  line, and the skew estimate works on a smaller probe with a coarser first pass.
  A clean 200 dpi A4 page takes about 660 ms on four cores; the corpus median is
  669 ms per page.

### Searchable in every script

- **PDF text layers are no longer limited to WinAnsi.** A line with CJK, Cyrillic
  or Greek in it is written with a Type0 font whose two-byte codes are UTF-16 code
  units, plus a `ToUnicode` CMap; Western text keeps the base-14 font it had. No
  font file is embedded, because the layer is invisible and no glyph outline is
  ever drawn — so the wheel stays free of a bundled font and its license, and the
  added objects cost about nine kilobytes per document.
- A Japanese scan now extracts as Japanese: `unmappable_chars` went from 340 over
  the corpus to **0**, and the font objects are added only to documents that need
  them, so a German invoice contains exactly what it did before.

### Looking things up

- **`doc.search("gesamtbetrag")`** returns every hit with its page and the box
  around the matching *words*, so it can be highlighted. Case-insensitive by
  default (OCR case is not reliable enough to search on), with `regex=True`,
  `case=True` and `whole_words=True` when you need them. It falls back to the line
  box when word boxes are off.
- **`progress=`** on `Ocr.scan` is called after every page with
  `(page, total_pages, lines)`, and `ocrust scan --progress` prints it. Raising
  inside the callback aborts the scan, and the exception is the one you raised.
- `scan_many` accepts directories and glob patterns like the CLI does. Files you
  name yourself stay exactly as given, duplicates included, so one document comes
  back per input; only expanded files are de-duplicated.

### Word spaces the recognizer swallowed

- **Missing spaces are restored from the pixels.** A CTC recognizer emits a space
  only when it is confident about the space class, and on a JPEG-compressed scan
  or a fax it drops them: `Gesamtbetrag: 5.726,88 EUR` came back as
  `Gesamtbetrag:5.726,88EUR`. Each recognition crop now also yields a column ink
  profile, and a gap becomes a space when it is wide on the CTC timeline, blank on
  the paper, and allowed by typography. The timeline alone is not enough — it
  leaves just as wide a gap after a capital `M` or between a doubled `mm`.
- Measured over the whole corpus: **mean CER 0.045 → 0.040, median 0.013 → 0.006,
  WER 0.176 → 0.120, word recall 0.836 → 0.892**, with no category worse. The
  biggest movers are the ones that go through a lossy encoder: image-only PDFs
  0.014 → 0.004 (recall 0.848 → 0.957), rotated PDFs 0.082 → 0.054 (recall
  0.514 → 0.884), aged scans 0.012 → 0.006, every raster format 0.007 → 0.001. A
  receipt's WER fell from 0.382 to 0.059.
- The decoder also keeps the *run* of timesteps a class occupies instead of only
  its first step, so `m` is wider than `i`. That is what makes the gaps mean
  something, and it makes per-word boxes more accurate as a side effect.
- `Ocr(rec_space_gap=0)` turns the pass off.

### Batches from the shell

- `ocrust scan` accepts **directories** (walked recursively, filtered to readable
  extensions) and **glob patterns**, which is what `ocrust scan '*.pdf'` needs on
  Windows, where the shell expands nothing. Inputs are sorted and de-duplicated,
  so a batch writes the same output twice in a row, and a directory with nothing
  readable in it is an error rather than a silent success.

### Models and offline installs

- The model manifest points at this repository's own `models/ppocrv6/` on
  `raw.githubusercontent.com`, so `ocrust install-models` and the repository are
  the same source.
- Model downloads authenticate with `OCRUST_GITHUB_TOKEN` or `GITHUB_TOKEN` when
  one is set, which is what a private repository or a GitHub Enterprise mirror
  needs. The token is sent only to GitHub hosts — never to a mirror URL out of a
  manifest — and that is unit-tested.

### Packaging and licensing

- **Apache-2.0 in the box.** The repository ships the license text and a NOTICE
  naming the bundled PP-OCR models, their upstream and their checksums, and both
  travel inside both wheels — a redistribution of someone else's Apache-2.0 work
  should say so where it lands, not only in a README.
- Project metadata points at this repository: homepage, source, documentation,
  changelog and issues, in `pyproject.toml` and in the crates.
- README images use absolute URLs, so the page renders on PyPI as well as on
  GitHub. `twine check` passes for both distributions.
- The release job publishes **two** projects, `ocrust` and `ocrust-models`, in
  separate steps: trusted publishing mints a token for one project at a time, so
  uploading them together would have been rejected on the first tag.

### Network shares

- **Reads retry through a dropped connection.** Scanning `\\fileserver\scans` is
  not a local read: SMB and NFS time out, reset and answer "the network name is
  no longer available" for reasons that have nothing to do with the file. Reads
  in the engine and writes from the CLI now retry twice with backoff. "Not
  found" and "permission denied" are answers, not hiccups, and are never
  retried; the Windows redirector's own codes (network name deleted, unexpected
  network error, out of system resources) are.
- `Ocr(io_retries=…)` and `--io-retries N` tune it; `0` fails on the first error.
- **An unreachable share says why.** `Path.exists()` answers *False* both for a
  missing file and for a server nobody can reach, so the CLI asks again and
  reports the real reason: `\\fileserver\scans: [WinError 53] The network path
  was not found` instead of a bare "no such file".
- UNC paths, mapped drives and POSIX mounts work everywhere a path does,
  including recursive directory walks and `'\\server\share\*.pdf'` patterns,
  which `ocrust` expands itself because the Windows shell does not.

### A test application

- `examples/app.py`: a local web app with **no dependencies beyond the library**
  — standard-library `http.server` and a hand-rolled multipart reader, no Flask,
  no Streamlit. Drop in a scan, a photo or a PDF and see the text, the line boxes
  over the image, all six export formats, search with highlighted hits, mean
  confidence and per-page timing, and a one-click searchable PDF.
- It doubles as a small HTTP API (`POST /scan`, `POST /scan?q=`, `POST /pdf`,
  `GET /languages`, `GET /health`), so it is also the quickest way to check an
  installation. `POST /pdf` returns an `X-Ocrust-Report` header, so a PDF that
  already had text reads as "nothing added" instead of looking like a failure.
- Covered by `tests/test_example_app.py` over real HTTP, and exercised by
  `scripts/dev_e2e.sh`, so the example cannot quietly rot.

### Documentation

- A logo (`assets/ocrust.svg`) at the top of the README and the wiki.
- A 15-page wiki under `wiki/`: installation, quickstart, the Python API, the
  CLI, PDF workflows, languages, models, performance, accuracy, architecture,
  evaluation, troubleshooting, contributing and the roadmap.

### Found by the corpus, fixed

- **Large-format sheets.** An A0 drawing at 300 dpi scored a 96% character error
  rate: detection scaled the whole sheet to 960 px, leaving 8 pt labels four
  pixels tall. Detection now runs in overlapping tiles above four times the
  working size, with each tile owning exactly its half of every overlap
  (CER 0.96 → 0.04).
- **Sideways pages.** Pages rotated a quarter turn were recognized but returned
  in column order. The share of tall boxes now decides the turn, and detection
  re-runs on the straightened page.
- **Upside-down pages.** Every line was read correctly but in reverse order. The
  180-degree line classifier's verdict now rotates the page geometry as well
  (CER 0.75 → 0.11).
- **Ruled tables** were read column by column, because a table's cell gaps looked
  like page columns to the XY-cut. A column split now also has to be at least
  3.5% of the content width.
- **Thread oversubscription.** Page workers each asked ONNX Runtime for every
  core, so eight workers were 20% *slower* than one. Workers now divide the
  cores; a 12-page scan went from 8.5 s to 5.8 s with four of them. The default
  is one worker, which is fastest for a single page.
- **Text-layer alignment.** The PDF overlay assumed OCR coordinates matched the
  rendered page, which deskewing and rescaling silently broke. The overlay now
  scans without geometry changes, and rotated lines get a rotated baseline.

### Evaluation

- `scripts/make_corpus.py` generates a torture-test corpus with ground truth:
  aged and stained scans, 1-bit faxes, A3 technical drawings with title blocks
  and vertical labels, ruled forms, thermal receipts, three-column newspapers,
  skewed and `/Rotate`-d pages, dark-mode screenshots, an A0 sheet at 300 dpi,
  multi-page PDFs and TIFFs, born-digital PDFs and deliberately broken files.
- `scripts/evaluate_corpus.py` reports character error rate, word error rate and
  word recall per category and language, plus throughput, worker scaling, a DPI
  sweep, preprocessing on/off, every export format, the PDF text layer, archive
  TIFF output and robustness against broken input.

### Packaging

- Prebuilt abi3 wheel, Python 3.9 and up. No Tesseract, PaddlePaddle, PyTorch,
  Poppler or PDFium.
- Installing needs no compiler and no admin rights: prebuilt abi3 wheels, and
  ONNX Runtime is loaded at run time from the `onnxruntime` wheel. A source build
  is pure Rust as well, except for the optional model downloader, whose TLS stack
  (`ring`) compiles C — `--no-default-features --features pdf` avoids it.
- Models ship as the `ocrust-models` wheel or are fetched from
  `raw.githubusercontent.com` with SHA-256 verification. No other hosts are ever
  contacted.
- CLI: `scan`, `ocr`, `pdf`, `tiff`, `languages`, `models`, `install-models`,
  `doctor`.
