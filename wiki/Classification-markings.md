# Classification markings

`ocrust vs` reads documents the way a person checks them for a
*Verschlusssache*: it looks at what is printed on every page and says which
grade each file is marked with, where, and why it thinks so.

```bash
ocrust vs akten/
```

```text
VS-NfD           akten/2026/beschaffung.pdf  pp. 1, 2, 4 · header, footer · unmarked: p. 3
GEHEIM           akten/2026/lage.tif  pp. 1-3 · header, footer
unreadable       akten/2026/scan_0815.pdf  unsupported input: could not parse PDF: Invalid
VS-NfD           akten/2026/vermerk.png  p. 1 · coloured stamp
TLP:AMBER        akten/cert/advisory.png  p. 1 · header
-                akten/merkblatt.png  5 mentions
done: 6 files, 4 marked (2 VS-NfD, 1 GEHEIM, 1 TLP:AMBER), 1 clean, 1 unreadable, 10 pages, 19.9 s
```

Each line is one file: the grade, the pages it is on, where on them, and what
else is worth knowing — here a page of a VS-NfD document that carries no grade,
a stamp found only by its colour, a file whose grade is unknown because it could
not be read, and a handout that talks about VS-NfD five times without being
marked with it. The exit status is 3: something is marked.

It is a scanner, not a classifier: it finds the grade a document **is marked
with**. Whether a document *ought* to be classified is a question for the
person who wrote it, and nothing here tries to answer it.

## What it finds

Every grade lands on one scale, the one the bilateral security agreements use,
so a gate can say "VS-NfD or above" and mean the NATO and EU equivalents too.

| Level | Germany (SÜG § 4, VSA 2023) | NATO | EU | Austria | Switzerland |
|---|---|---|---|---|---|
| 1 | VS-NUR FÜR DEN DIENSTGEBRAUCH, VS-NfD | NATO RESTRICTED | RESTREINT UE / EU RESTRICTED | EINGESCHRÄNKT | INTERN ≈ |
| 2 | VS-VERTRAULICH | NATO CONFIDENTIAL | CONFIDENTIEL UE / EU CONFIDENTIAL | VERTRAULICH | VERTRAULICH |
| 3 | GEHEIM | NATO SECRET | SECRET UE / EU SECRET | GEHEIM | GEHEIM |
| 4 | STRENG GEHEIM | COSMIC TOP SECRET | TRÈS SECRET UE / EU TOP SECRET | STRENG GEHEIM | — |

Also on the scale, several of them only approximately (≈):

- France: DIFFUSION RESTREINTE ≈ 1 (a handling mention, not a classification);
  the pre-2021 CONFIDENTIEL, SECRET and TRÈS SECRET DÉFENSE as 2, 3 and 4.
- United Kingdom: OFFICIAL-SENSITIVE ≈ 1 (formally not a tier), SECRET,
  TOP SECRET.
- United States: CUI and FOUO ≈ 1 (formally unclassified), CONFIDENTIAL,
  SECRET, TOP SECRET, with banner caveats such as `//NOFORN`.
- Switzerland in French and Italian (INTERNE, CONFIDENTIEL; AD USO INTERNO,
  CONFIDENZIALE, SEGRETO) and Italy (RISERVATO … SEGRETISSIMO).
- DDR: VD, VVS, GVS and their long forms; historic German: Geheime
  Kommandosache, Geheime Reichssache, "Geheim!".
- Lower bounds: "amtlich geheimgehalten", which the VSA prints beside
  VS-VERTRAULICH and above, counts as *at least* level 2; the classification
  term line ("Die VS-Einstufung endet mit Ablauf des Jahres 2055") and a US
  classification block (`Classified By:`) count as classified without naming
  the grade.

Reported **beside** the scale, never on it:

- **TLP** 2.0 and 1.0: `TLP:RED`, `TLP:AMBER+STRICT`, `TLP:AMBER`,
  `TLP:GREEN`, `TLP:CLEAR` (`TLP:WHITE` is read as CLEAR).
- **Company markings**: STRENG VERTRAULICH, VERTRAULICH, INTERN, NUR FÜR DEN
  INTERNEN GEBRAUCH, GESCHÄFTSGEHEIMNIS, STRICTLY CONFIDENTIAL, CONFIDENTIAL,
  INTERNAL, RESTRICTED.
- **Explicitly unclassified**: OFFEN (Bundeswehr), UNCLASSIFIED, NATO
  UNCLASSIFIED, NOT PROTECTIVELY MARKED — markings at level 0.

VERTRAULICH, INTERN and GEHEIM mean different things in different countries,
so the document decides: VERTRAULICH on a letter from Vienna or Bern is a
state grade (level 2), on a letter from a Munich company it is that company's
marking. The cues are the letterhead words a reader would use — *Republik
Österreich*, *Wien*, *Eidgenossenschaft*, *VBS* — and a document with none of
them is read as German.

## Marking or mention

Finding the words is the easy half. A directive explaining how VS-NfD papers are
handled, a product brochure "zugelassen für VS-NfD", a letter about the
*Betriebsgeheimnis* or a *geheime Wahl* all contain them, and none of them is
classified. Each hit is judged by where it stands:

| Where | Judged as | Why |
|---|---|---|
| Alone on its line (a stamp) | marking | nothing else was printed with it |
| In the header or footer band (top and bottom 12 % of the page) | marking | where the VSA puts it: top of every page, top and bottom from GEHEIM up |
| Beside a page number, copy number, field label, date or caveat (`Seite 2 von 5`, `Kopie Nr. 3`, `Geheimhaltungsgrad:`) | marking | those are what a marking line carries |
| After an e-mail's subject label (`Betreff: VS-NfD – …`) | marking | the VSA puts the grade before the subject |
| Beside a ticked box on a form (`☒ VS-NfD`) | marking | the form's choice |
| Beside an empty box (`☐ VS-VERTRAULICH`) | mention | an option not chosen |
| In a sentence, among lowercase words | mention | "Unterlagen des Grades VS-NfD sind …" |
| Negated (`nicht`, `kein`, `not`) | mention | "nicht als Verschlusssache eingestuft" |
| A word built on it (`VS-NfD-Zulassung`) | mention | a product approval, not a grade |
| On a line about grades (`Merkblatt`, `Leitfaden`, `zugelassen`, `approved for`) | mention | "VS-NUR FÜR DEN DIENSTGEBRAUCH (VS-NfD-Merkblatt)" is a handout's title |
| On a line that carries on the one before (`… NATO RESTRICTED und` / `RESTREINT UE/EU RESTRICTED`) | mention | it stands alone only because the sentence wrapped |
| Two grades joined by words (`NATO RESTRICTED entspricht VS-NfD`) | mention | a comparison — joined by only a separator they are one bilingual marking |
| In a table cell | mention | a register or a form listing grades |
| One of three or more grades standing alone on one page | mention | a directive or a training slide listing them — even if its own header says VS-NfD |
| On a page stamped MUSTER or SPECIMEN | mention | shown as an example |

Words that are ordinary language — GEHEIM, VERTRAULICH, INTERN, SECRET,
CONFIDENTIAL — count only in capitals: "die Wahl ist geheim" is not a grade at
all and is not even reported. The one exception is a company's own stamp, which
is often set in ordinary case: "Vertraulich" or "Geschäftsgeheimnis" alone on
its line is a company marking; "Geheim" alone is a heading. Abbreviations that mean other things in a
sentence (NfD, VVS, GVS, CUI) are reported only as markings. Mentions are kept
in the report — `--mentions` lists them — because "this handbook talks about
GEHEIM on page 12" is sometimes exactly what an auditor wants to know.

## How OCR reads a marking

Markings are printed badly more often than body text: rubber stamps at an angle,
red ink, letter-spaced capitals, faxes of faxes. The matcher takes the text as
the recognizer read it:

- **Rotated stamps** are read by the pipeline at any angle; a stamp at 0°, 15°,
  30°, 45° and 90° came out as `VS-NfD` or `VS-NFD` in every probe.
- **Letter-spaced** stamps (`G E H E I M`, `V S - V E R T R A U L I C H`) are
  read with the spaces removed.
- **OCR confusions** in capitals are undone — `V5-NFD`, `VS-NtD`, `GEHElM`,
  `VERTRAUL1CH`, a letter dropped from a long word — and the finding is
  flagged `fuzzy`, to be checked by eye. The repair never turns an inflected
  word into a grade: GEHEIME WAHL stays a heading.
- **A coloured stamp across the text** is lost among the black lines it
  crosses — and garbles them. `ocrust vs` reads the page's coloured ink a
  second time on its own, dark on white with the black text dropped, which is
  how a person reads it too; a red VS-NfD at 20° over a paragraph goes from
  missed to found. A page without coloured ink costs one look at its pixels,
  about 1 %; a page with a stamp about 13 % more. In Python it is
  `Ocr(read_stamps=True)`.
- **Tick boxes** are hardly ever read by a recognizer: "☐ offen ☒ VS-NfD ☐
  VS-VERTRAULICH" arrives as "offen VS-NfD VS-VERTRAULICH", and which grade the
  form carries is gone. `ocrust vs` finds the boxes in the pixels — a small
  square outline, empty or crossed — and the option beside a ticked box is the
  marking, the others mentions. A box filled solid is a bullet, not a tick. It
  costs about 40 ms a page; in Python it is `Ocr(tick_boxes=True)`.
- **A stamp that bridges two rows** of letterhead is read by the layout as one
  line with them; each of the detector's boxes is judged on its own, so the
  stamp is still a marking, boxed where it was printed.
- **Fax noise**: pages covered in lone specks are cleaned before detection
  (only pixels with no like-coloured neighbour change, so strokes are never
  thinned). On a 100 dpi fax with 4 % noise this took the header marking from
  missed to found; over the evaluation corpus it cut the fax error rate from
  1.4 % to 0.8 % and left every other page byte-identical.

## The file itself

With `ocrust vs` (or `inspect(doc, file=path)` in Python) the file is checked
too:

- A **sensitivity label** that Microsoft Information Protection, Acrobat or
  the MIP SDK stored in the metadata (`MSIP_Label_<guid>_Name`) counts as a
  marking. A label named "VS-NfD" is level 1; "Highly Confidential" is a
  company marking; a label that names no grade ("Public") is reported as it
  is. Office's own *Save as PDF* drops these properties, so their absence
  proves nothing.
- A grade in the **file name** — the VSA asks for `…_VS-NfD.pdf` — is reported
  as a hint and never counts: `Merkblatt_VS-NfD.pdf` names a grade it does not
  carry.

## `ocrust vs`

```bash
ocrust vs archive/                            # every readable file, recursively
ocrust vs scan.pdf --fail-on geheim           # exit 3 only from GEHEIM up
ocrust vs share/ -f csv -o audit.csv          # one row per finding, for a spreadsheet
ocrust vs share/ -f jsonl | jq 'select(.level >= 1) | .source'
ocrust vs inbox/ --mentions                   # also the sentences that talk about grades
```

| Option | Meaning |
|---|---|
| `--fail-on GRADE` | exit 3 at this grade or above: `vs-nfd` (default), `vs-v`, `geheim`, `streng-geheim`, or 1–4; `any` also counts TLP (except CLEAR) and company markings; `none` never |
| `-f text\|json\|jsonl\|csv` | report format; `json` and `csv` carry every finding with its page, box, reason and confidence |
| `-o FILE` | write the report to a file (atomically) instead of stdout |
| `--mentions` | list mentions under each file in the text report |
| `--workers N` | pages scanned in parallel (default: one per core, at most 16) |
| `--terms FILE`, `--term PHRASE` | also look for the terms of a search profile ([Search profiles](Search-profiles.md)); a hit then trips the default gate too |
| `--shard K/N`, `--resume` | split a large job between machines, and continue an interrupted one |
| `--pages`, `--dpi`, `--password`, `--max-pixels`, `--models`, `--device`, `--threads` | as for `ocrust scan` |

Files are scanned in parallel, a chunk at a time, and reported in the order they
were given as each chunk finishes — a share of ten thousand files starts
reporting within seconds and never holds more than one chunk in memory.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | every file was read and none is marked at or above `--fail-on` |
| 1 | none is marked, but some file could not be read — its grade is unknown, so this is **not** a clean result |
| 2 | bad arguments, or an input that does not exist |
| 3 | at least one file is marked at or above `--fail-on` |

3 wins over 1: a marked file is the finding that matters, and the unreadable
ones are still listed. A gate in CI or on an upload path is therefore:

```bash
ocrust vs upload/ -q || { echo "classified or unreadable content — not published"; exit 1; }
```

### Reports

The text report names the grade, the pages it was found on, where on the page,
anything else marked, and — for a marked document — **pages that carry no
grade**. The VSA wants the grade on every page, so a gap is either a page left
unmarked or a page the scan could not read; both are worth a look. A line that
says a grade was lifted or lowered ("VS-NfD aufgehoben", "Approved For Release")
is flagged, but the grade still counts: whether it applies to this copy is a
human's call.

`-f json` writes one object per file (the first of its findings shown):

```json
{
  "source": "akten/2026/beschaffung.pdf",
  "status": "ok",
  "level": 1,
  "label": "VS-NfD",
  "classified": true,
  "tlp": null,
  "company": null,
  "pages": [1, 1, 0, 1],
  "page_numbers": [1, 2, 3, 4],
  "unmarked_pages": [3],
  "cancelled": false,
  "caveats": [],
  "findings": [
    {
      "kind": "marking",
      "scheme": "de",
      "level": 1,
      "label": "VS-NfD",
      "text": "VS - NUR FÜR DEN DIENSTGEBRAUCH",
      "match": "VS - NUR FÜR DEN DIENSTGEBRAUCH",
      "page": 1,
      "box": [545.0, 66.6, 1109.1, 96.6],
      "confidence": 0.9908,
      "fuzzy": false,
      "reason": "header",
      "caveats": [],
      "cancelled": false
    }
  ]
}
```

`-f jsonl` writes the same, one line per file as each is done; `-f csv` one row
per finding, and one row for a file with none, so a spreadsheet lists every file
that was looked at.

## In Python

```python
import ocrust

doc = ocrust.scan("akte.pdf")
report = doc.markings()          # or ocrust.markings.inspect(doc, file="akte.pdf")

report.level                     # 1
report.label                     # 'VS-NfD'
report.at_least("vs-nfd")        # True
report.unmarked_pages            # (3,)
for f in report.markings:
    print(f.page, f.label, f.reason, f.box.as_tuple(), f.fuzzy)
```

| `MarkingReport` | |
|---|---|
| `level`, `label`, `classified` | the highest grade on the common scale, its canonical name, and whether it is 1 or above |
| `tlp`, `company` | the most restrictive TLP colour and company marking |
| `pages`, `page_numbers`, `unmarked_pages` | per page the grade found on it; page numbers as in the source |
| `markings`, `mentions`, `findings` | the evidence, each a `Finding` |
| `cancelled`, `caveats` | a lifted grade on some page; NOFORN, ATOMAL, REL TO … |
| `at_least(grade)`, `to_dict()` | the gate, and plain JSON |

`ocrust.markings.level_for("vs-v")` turns a grade name into its level.

## Accuracy

Measured with `scripts/evaluate_vs.py` on two generated sets: a development
corpus of 186 files (228 pages) the rules were written against, and an
independent set of 109 files (140 pages) written by someone who had not seen
the detector — new templates and wording: printed e-mails, forms with tick
boxes, slides, newsletters, a TV listing, CERT advisories — to see how it does
on documents it was not tuned for.

| set | files | precision | recall | right level | no false grade on a negative | documents entirely right |
|---|---:|---:|---:|---:|---:|---:|
| development corpus | 186 | 100 % | 100 % | 106 / 106 | 62 / 62 | 186 / 186 |
| independent set, first run | 109 | 95.8 % | 92.0 % | 46 / 50 | 46 / 48 | 101 / 109 |
| independent set, after the fixes it prompted | 109 | 100 % | 100 % | 50 / 50 | 48 / 48 | 108 / 109 |

The first run on the independent set is the number to quote: it is what a
document nobody tuned for gets. Its eight errors were a grade ticked on a form
(the recognizer drops tick boxes — now found in the pixels), two printed
e-mails with "Betreff:" in a column of its own, "VS-NUR FOR DEN DIENSTGEBRAUCK", the film "Top
Secret!", a slide comparing grades, a newsletter called "HAUS INTERN" and an
UNCLASSIFIED banner named rather than left unnamed — the last is a convention,
and the one difference left. The second run is no longer blind.

Both sets are generated, not collected: realistic, varied and degraded (fax,
JPEG, aged, skewed, faint, low resolution), but no generated set is the
archive on your share. Run `ocrust vs` over a sample you know and look at what
it says before trusting it with the rest — the report gives you the page, the
box and the reason for every finding, which is what makes that quick.

OCR takes about 1.4 s a page on four CPU cores (colour and tick-box passes
included); the detector itself under 10 ms.

## Limits

- It reads what the recognizer read. A marking the OCR cannot see — pale red
  on red paper, handwriting, a stamp over a photo — is not found; `quality` on
  the scan says how far the page can be trusted.
- A tick box is found when it is a printed square outline. A tick drawn by
  hand far outside the box, or a form that marks its choice by circling or
  underlining, is not seen; the options then read as a list of grades.
- A struck-through grade still reads as the grade. The VSA keeps the old grade
  readable on purpose, and telling a strike-through apart needs the pixels, not
  the text.
- Metadata is read from PDFs and images as they are stored; Office documents
  themselves (`.docx`, `.xlsx`) are not a format ocrust opens.
- Country cues are words on the page. A VERTRAULICH letter from a Swiss federal
  office without any of them is read as a company marking.

None of this replaces the person responsible for the documents. It is built to
make sure that person sees every file that needs looking at.
