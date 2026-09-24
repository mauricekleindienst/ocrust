# Search profiles

`ocrust find` looks through scanned documents for whatever a profile names —
project code names, people, companies, compound words, part numbers, customer
numbers — and finds them the way the scan actually reads them: letter-spaced on
a stamp, hyphenated at a line end, broken over two lines, two table cells or
two columns, glued to the next word, split by a stray space, misread (`0` for
`O`, `rn` for `m`), or spelled with `ae` for `ä`.

```bash
ocrust find akten/ --terms profil.toml
```

```text
6 hits           akten/brief.pdf  Jürgen Weißmüller (p. 1, spelling) · Geheimhaltungsvereinbarung (p. 1) · Projekt Adler ×3 (p. 1, glued, spaced, split) · Kundennummer (p. 1, regex)
-                akten/rechnung.png
done: 2 files, 1 with hits, 1 clean, 2 pages, 2.4 s
```

That letter read `ProjektAdler`, `Pr o j e k t A d l e r`, `Projekt` / `Adler`
over two lines and `Juergen Weissmueller`; the same page's `Radler`,
`Adlerhorst` and a five-digit `KD-12345` were not reported.

## The profile

TOML (Python 3.11+, or `pip install tomli`), JSON with the same structure, or a
plain text file with one phrase per line.

```toml
[settings]                      # defaults for every term, all optional
fuzzy = "auto"                  # real edits tolerated on top of OCR look-alikes
case = false                    # case-sensitive?
whole_words = true              # must start and end on a word boundary
severity = "medium"             # info | low | medium | high | critical
markings = false                # also report VS-NfD, GEHEIM, TLP … (ocrust vs)

[[term]]
name = "Projekt Adler"          # the name in reports; defaults to the first phrase
match = ["Projekt Adler", "Operation Adler"]   # one phrase or several
category = "Projekte"
severity = "high"
not_near = ["Radsport"]         # skip it when one of these is within `window`

[[term]]
name = "Kundennummer"
regex = 'KD-\d{6}'              # a Python regular expression
severity = "low"

[[term]]
name = "Teil FS-220"
match = "FS-220"
fuzzy = 0                       # FS-221 is another part

[[term]]
name = "Entwurf im Kopf"
match = "Entwurf"
zones = ["header"]              # header, body, footer: where it counts
near = ["Vermerk"]              # only when one of these is within `window`
window = 80                     # letters to either side (default 60)
```

A phrase is matched case-insensitively, with umlauts, `ß` and accents folded,
and every space, dash, underscore and line break inside it optional:
`Projekt-Adler`, `Projekt_Adler`, `ProjektAdler` and `Projekt` / `Adler` over two
lines are all `Projekt Adler`. With `whole_words` (the default) a hit must start
and end on a word boundary, so `Adler` is not in `Radler` or `Adlerhorst` — nor
in `Adlers`: a letter too many at the edge makes a longer word, not a misread
`Adler`, whatever `fuzzy` allows. For inflected forms list them
(`match = ["Adler", "Adlers"]`) or set `whole_words = false`.

With `case = true` every letter read in place of one of the term's must have
its case, spellings included: `Mueller` is `Müller`, `MUELLER` is not.

A mistake in a profile — an unknown key, a severity that does not exist, a
regex that does not compile, two terms with one name (also across two
`--terms` files), `[term]` written for `[[term]]` — stops the run with the
file, the term and what is wrong, rather than becoming a term that silently
never matches. In a plain phrase list `#` starts a comment, at the start of a
line or after a space.

## How broken can it be

Each page is matched as one stream of letters and digits with every space,
dash and line break taken out, so all of the breakage below reads as the same
word; what was taken out is kept aside to enforce word boundaries and to say,
per hit, how it was broken (`how` in the report):

| `how` | The scan read | For |
|---|---|---|
| `exact` | `Projekt Adler`, `Projekt-Adler` | `Projekt Adler` |
| `spaced` | `P r o j e k t  A d l e r` | letter-spaced on a stamp or heading |
| `hyphenated` | `Geheimhaltungs-` / `vereinbarung` | a word hyphenated at a line end, with a box on each line |
| `split` | `Projekt` / `Adler` | a phrase over two lines, cells or columns |
| `glued` | `ProjektAdler` | a space the scan lost |
| `broken` | `Geheimhaltungs vereinbarung` | a space the scan added |
| `ocr` | `Pr0jekt AdIer`, `Weißrnüller` | look-alike characters: 0/O/Q, 1/l/I, 5/S, 8/B, 6/G, 2/Z, rn/m, vv/w |
| `spelling` | `Juergen Weissmueller` | `ae`/`oe`/`ue` for an umlaut, `ss` for `ß` |
| `fuzzy` | `Geheimhaltungsvereinbrung` | real edits, within the term's budget |
| `regex` | `KD-123456`, `KD-12` / `3456`, `KD-` / `123456` | a regular expression; a number broken over a line is still one number |

Look-alikes and spellings cost almost nothing; real edits (a letter wrong,
missing or extra) count against `fuzzy`:

| `fuzzy` | Real edits allowed |
|---|---|
| `"auto"` (default) | none up to 4 letters, one up to 8, two beyond |
| `0` / `"off"` | none — for part numbers, short codes and names that differ by a letter |
| `1`, `2`, `3` | that many, but never more than a third of the term |

Each hit carries a `score` — 1.0 for a clean read, lower the more had to be
forgiven — so a report can be sorted by how sure it is.

## `ocrust find`

```bash
ocrust find akten/ --terms profil.toml                    # text, one line per file
ocrust find akten/ --term "Projekt Adler" --term Falke    # phrases on the command line
ocrust find akten/ --terms profil.toml --fail-on high     # exit 3 only from `high` up
ocrust find akten/ --terms profil.toml -f csv -o hits.csv # every hit with page, box, how
ocrust find akten/ --terms profil.toml --markings         # and VS-NfD, GEHEIM, TLP … too
ocrust vs akten/ --terms profil.toml                      # the same, from the other side
```

| Option | Meaning |
|---|---|
| `--terms FILE` | a profile; repeat to combine several |
| `--term PHRASE` | a phrase with the default settings; repeatable |
| `--fail-on LEVEL` | exit 3 when a file has a hit at this severity or above, or a grade (`vs-nfd` …) — which turns `--markings` on; `any` (the default: any hit, or any marking that restricts reading) or `none`; repeat to combine |
| `--markings` | also report classification markings, as `ocrust vs` does |
| `-f text\|json\|jsonl\|csv`, `-o FILE` | report format and destination |
| `--shard K/N`, `--resume` | split a large job, and continue one — see below |
| `--workers`, `--pages`, `--dpi`, `--password`, `--max-pixels` | as for `ocrust scan` |

Exit status: **3** a file has a hit at or above `--fail-on`, **1** none has but
a file could not be read, **0** every file read and nothing found, **2** a bad
argument or profile. After `--resume` it answers for the whole report,
including the files the earlier run read.

In the JSON report each file has a `terms` object with its hits; in CSV each hit
is a row with `kind = term`, the term in `label`, the category in `scheme`, the
severity in `severity`, how it was broken in `reason` and the score in
`confidence`.

## Large jobs, in parallel

Files are scanned in parallel, a chunk at a time, with one page worker per core
by default (at most 16): every worker has a model session of its own and the
cores are divided between them, so no worker waits for another and no core is
asked for twice.

On one machine, one process is fastest — measured on four cores over 36 pages:

| Layout | Seconds |
|---|---:|
| 1 process, 4 workers | **45.3** |
| 2 processes × 2 workers (`--shard`) | 52.6 |
| 1 process, 1 worker | 56.7 |
| 4 processes × 1 worker (`--shard`) | 67.0 |

Four processes of one worker each lose because every one of them believes it has
all four cores; if several processes must share a machine, give each
`--threads cores/processes`.

Across machines, `--shard K/N` gives each run a fixed part of the files, decided
by a hash of each file's path below the folder that was walked — the same file
lands in the same part on every machine, whatever the share is mounted as, and
the N parts together cover every file once:

```bash
# on four machines, each with the share mounted wherever it likes
ocrust find /mnt/share --terms profil.toml --shard 1/4 -f jsonl -o hits-1.jsonl
ocrust find /mnt/share --terms profil.toml --shard 2/4 -f jsonl -o hits-2.jsonl
# …
cat hits-*.jsonl > hits.jsonl
```

A JSON Lines report is written as it goes, one line per file, so a run that is
interrupted keeps what it did, and `--resume` carries on from there, skipping
every file the report already has. Half a line left by the interruption is cut
off and its file read again; a file that is not such a report is refused and
left as it is.

```bash
ocrust find /mnt/share --terms profil.toml -f jsonl -o hits.jsonl            # interrupted
ocrust find /mnt/share --terms profil.toml -f jsonl -o hits.jsonl --resume   # the rest
```

In Python, `Ocr.scan_each` is the same batch loop: parallel, in input order,
and a file that cannot be read arrives as its exception instead of ending the
batch.

```python
import ocrust

ocr = ocrust.Ocr(page_workers=8)
profile = ocrust.terms.load("profil.toml")
for path, result in ocr.scan_each(["/mnt/share"]):
    if isinstance(result, Exception):
        print(path, "unreadable:", result)
        continue
    for hit in result.find(profile):
        print(path, hit.page, hit.term, hit.how, hit.score)
```

## In Python

```python
profile = ocrust.terms.load("profil.toml")   # or a .json, a .txt, a list of phrases
report = ocrust.scan("akte.pdf").find(profile)

report.terms          # {'Projekt Adler': 3, 'Kundennummer': 1}
report.severity       # 'high'
report.at_least("medium")
for hit in report:
    hit.term, hit.page, hit.text, hit.how, hit.score, hit.box, hit.boxes, hit.zone
```

`hit.boxes` has one box per line a hit spans; `hit.box` surrounds them all.
`ocrust.terms.parse(dict)` builds a profile from a dict, and profiles add up:
`profile_a + profile_b`.

## Accuracy

Measured with `scripts/evaluate_terms.py` on a set written by someone who had
not seen the matcher (`scripts/make_terms_holdout.py`): 84 files, 96 pages —
letters, faxes, invoices, minutes, e-mails, contracts, forms, slides, data
sheets and four two-column staff newsletters, as PNG, JPEG, TIFF and PDF, some
blurred, faint, skewed, JPEG-compressed or stamped — with 411 expected hits of
34 terms and 62 decoys that must not be hits: `Hafenstraße 120` for
`Hafenstraße 12`, `Kranichsteiner Straße` for `Kranich`, `Nordlicht` alone for
`Projekt Nordlicht`.

| | precision | recall | files entirely right |
|---|---:|---:|---:|
| first run, blind | 98.7 % | 95.4 % | 64 of 84 |
| after the fixes it prompted | **99.8 %** | **99.8 %** | **82 of 84** |

The first run is the honest figure; the second is no longer blind. Its misses
were fixed at their cause, each with a test: a phrase broken over two columns
(0 of 6 found before, 6 of 6 after), a customer or part number in a form's
comb fields for a regex (`K D - 4 3 8 3 0 0`), a word glued to the next with only a
capital to show it (`HerrWeißmüller`), a misspelling whose cheapest reading
started inside the word, `NOX` after a word ending in `r` (the prefilter took
the `rn` for an `m`), and a stamp read twice reported twice.

What is still counted wrong: `über orka` read as `überorka` — the recognizer
lost the space and a small letter follows a small letter, so nothing marks a
word boundary; and a `NOX` the set calls a decoy because `Abgaswerte` stands
in the same line, 65 letters away, where the profile's `window` is 50.

Matching takes about 11 ms a page, next to about 1.6 s for reading it.

## Limits

- It reads what the recognizer read. A term the recognizer lost entirely — a
  word under a stamp, handwriting — is not found; `Document.quality` says how far
  a page can be trusted.
- Each page is read twice: in the layout's order, and column by column — the
  page cut where it is emptiest, between columns and between sections, again
  and again. A phrase that wraps inside a column, a table cell, or from the
  foot of one column to the head of the next is found either way; one whose
  halves are not next to each other in either order is not.
- Read column by column, a form's values follow one another down the page, so
  `Kessler` in one row and `Logistik` in the row below would read as
  `Kessler Logistik`.
- One- and two-letter words — an initial, a house number, the `X4` of
  `Sentinel X4` — must be read as written; `fuzzy` is spent on the longer
  words only. `M. Schöllhorn` is not found in `Ms Schöllhorn`.
- `near` and `not_near` look at letters to either side, not at sentences or
  pages. `not_near` also sees the word the hit is part of: with
  `whole_words = false`, `not_near = ["Adlerhorst"]` rules out the `Adler` in
  `Adlerhorst`. Both are judged in each reading order, so a context word right
  above the term in its column counts though the layout read the next column
  in between.
- A phrase across two pieces of a line far apart — two table cells, or two
  columns the layout read as one line — is a hit, marked `split`: a table
  row "Projekt | Adler" is the phrase, the end of one article and the start of
  the next beside it may not be.
- Letter-spaced text has no reliable word gaps, so inside a letter-spaced run
  a word boundary is assumed wherever one is needed: `ADLER` letter-spaced
  inside `R A D L E R` would be a hit.
