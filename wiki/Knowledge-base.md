# Knowledge base: any document as Markdown

`ocrust markdown` turns a folder of documents — scans, PDFs, Word, PowerPoint,
Excel, OpenDocument, e-books, web pages, mails, RTF, CSV, text, source code —
into a folder of Markdown notes, and keeps it in step with the documents. The
result opens in Obsidian as a vault, reads on GitHub, and is what a retrieval
pipeline (LangChain, LlamaIndex, Haystack, Open WebUI, AnythingLLM, …) loads
best: plain text with its structure intact and its metadata beside it.

```bash
ocrust markdown archiv/ -o wissen/
```

```text
archiv/Angebote/kunde.docx -> wissen/Angebote/kunde.docx.md
archiv/Berichte/q1.pdf -> wissen/Berichte/q1.pdf.md
archiv/Mails/rueckfrage.eml -> wissen/Mails/rueckfrage.eml.md
done: 3 notes written, 1.4 s
```

Run it again and only what changed is converted; run it every night and the
knowledge base follows the share.

## How the notes are stored, and why

**One note per document.** Not one per page — a page break is an accident of
paper, and a chunk that stops at one loses the sentence it cut — and not one
file for everything, which no editor opens and no sync can update in part.
Retrieval pipelines split a note into chunks themselves; they do it better
along headings than along pages.

**Folders mirrored, the extension kept.** `Berichte/q1.pdf` becomes
`Berichte/q1.pdf.md`. The note sits where its document sits, so the folder
structure people already navigate by stays the structure of the knowledge
base, and `q1.pdf` and `q1.docx` are two notes instead of one overwriting the
other.

**CommonMark with pipe tables and footnotes, nothing more.** Every Markdown
tool reads that the same way. Tables stay tables — a price list as a grid of
cells is what makes a question about a price answerable — and footnotes are
footnotes, not stray numbers in the text. Wiki links, callouts and other
dialects are left out: they would tie the notes to one tool.

**Flat YAML front matter.** One key, one value or one list of values:

```markdown
---
title: "Angebot Zutrittskontrolle – Rückfrage"
from: "Jürgen Weißmüller <j.weissmueller@helvetor.de>"
to: ["Maurice Kleindienst <m.kleindienst@example.org>"]
date: 2026-03-24T09:15:00Z
language: "de"
source: "Mails/rueckfrage.eml"
source_type: "eml"
source_modified: 2026-09-24T13:33:52Z
sha256: "4dd32558…"
attachments: ["angebot.docx"]
---
```

That is what Obsidian shows and filters as properties, and what a Markdown
loader turns into the metadata of every chunk — nested YAML is what both of
them handle worst. Strings are quoted, so no title with a colon or a leading
dash can change what the file means; dates are unquoted, which is how Obsidian
recognizes a date.

| property | from |
|---|---|
| `title` | the document's own title, else its first heading, else the file name |
| `author`, `created`, `modified`, `subject`, `description`, `keywords` | the document's metadata (Office, OpenDocument, EPUB, HTML, RTF) |
| `from`, `to`, `cc`, `date`, `attachments`, `message_id` | a mail's headers |
| `language` | the document's, or recognized from the text when it clearly is one language |
| `source`, `source_type`, `source_modified`, `sha256` | the file: path relative to the folder given, kind, time, content hash |
| `pages`, `slides`, `sheets`, `chapters`, `rows` | how long it is |
| `extraction`, `ocr_quality` | how the text was obtained — `text` (a PDF's own), `ocr` or `mixed` — and the estimated share of correct characters where OCR was involved |
| `truncated` | a spreadsheet or CSV longer than `--max-rows` |

**A marker where each page begins.** `<!-- page 3 -->` is invisible when the
note is rendered, and it is what lets an answer cite the page it came from.
Slides, sheets, chapters, attachments and archive members get one each too.

**The same bytes for the same input.** No conversion time in the note, NFC
Unicode, LF line ends, properties in a fixed order. Converting again changes
nothing, so nothing is re-embedded or shows up as a change in a synced vault
that did not change.

**The index beside the notes, out of sight.** `wissen/.ocrust/index.json`
records which document each note came from, its size, time and hash, and the
hash of the note as it was written. Obsidian and most tools ignore folders
starting with a dot.

## Keeping it in sync

A second run reads nothing that did not change: a document with the same size
and time is skipped without being opened, and one that was only touched —
copied back, restored — is recognized by its hash.

- **A note edited by hand is never overwritten.** Someone added a remark in
  Obsidian: the note's hash no longer matches the index, and the export leaves
  it alone and says so, every run, until `--force`.
- **Nothing the export did not write is touched.** A file already sitting where
  a note would go is kept.
- **`--prune` removes what is gone** — the notes of documents deleted from the
  folders given in this run, and their pictures. Notes from other folders
  exported into the same knowledge base, and notes edited by hand, stay.
- **`-n` / `--dry-run`** says what would be written and removed, and does it
  not.
- **One export at a time** writes to a folder; a second one started meanwhile
  stops and says so. A lock left behind by a crashed run is taken over.

## What is read from each kind of file

| | |
|---|---|
| **PDF** | The page's own text where it can be trusted (`--pdf-text auto`, the default): exact, and without any model time. Scans, pictures of text, and pages whose text layer is someone else's OCR are recognized. Running heads and page numbers repeated along the pages are dropped; headings get their level from their size; lines are joined where the column was full and kept apart where a line was broken on purpose (an address, a signature); a paragraph cut by a page break is one paragraph again; tables are pipe tables. |
| **Images** | Recognized — PNG, JPEG, TIFF (every page), WebP, BMP, GIF and the rest. |
| **Word** (`.docx`, `.docm`, `.dotx`) | Headings with their numbering, bold, italic and struck text, lists nested by level and numbered as printed — also when a table interrupts them —, tables with merged cells, links (fields included), footnotes and endnotes, text boxes, charts as the table of their numbers, SmartArt, pictures. Tracked deletions, hidden text and field codes are left out. |
| **PowerPoint** (`.pptx`) | Slide by slide in order, each under its title; placeholders in reading order, bullets nested, tables, charts as tables, SmartArt, pictures, and the speaker notes. |
| **Excel** (`.xlsx`, `.xlsm`) | Sheet by sheet; a table per block of rows, a title row as text; dates as dates, percentages as percentages, formulas as their values. |
| **OpenDocument** (`.odt`, `.ods`, `.odp`) | The same, from LibreOffice's formats. |
| **Web pages** (`.html`, `.mht`) | The page's `<main>` or `<article>` when it has one; navigation, scripts and hidden elements dropped; tables that only lay out the page read as the text they hold. |
| **Mail** (`.eml`) | Sender, recipients, date and subject as properties; the HTML body where there is one; every attachment converted into a section of its own — a forwarded mail included. |
| **E-books** (`.epub`) | Chapters in reading order. |
| **RTF** | Paragraphs, headings, bold and italic, lists, tables, links and footnotes, in the document's code page. |
| **CSV, TSV** | A table; the delimiter is recognized. |
| **Text** | Paragraphs with wrapped lines joined, lists, quotes, underlined headings and tab-separated tables; UTF-8, UTF-16 and Windows-1252 recognized. |
| **Markdown** | Kept as written; its own front matter is kept and only completed. |
| **Source code, JSON, YAML, XML, …** | One fenced block with the language named. |
| **Subtitles** (`.srt`, `.vtt`) | The spoken text, without numbers and timings. |
| **Zip archives** | A folder of notes, `paket.zip/…`; members outside the archive's own folder are never written. |
| **`.doc`, `.xls`, `.ppt`** | Through LibreOffice, when `soffice` is installed; otherwise reported. |

Everything except PDFs, images and the pictures inside documents is read
without the OCR models — a folder of Word files converts on a machine where
they are not installed — and without any package besides ocrust itself.

## Pictures

The text in a picture inside a document — a scanned letter pasted into Word, a
screenshot on a slide, a diagram — is recognized and follows the picture as a
quote. `--no-pictures` leaves pictures unread, `--no-ocr` leaves images and
pictures alone altogether.

`--assets` keeps the pictures too, under `_assets/` mirrored like the notes,
and links them: `![Lageplan](../_assets/Berichte/q1.docx/image1.png)`. A
picture that appears on every slide is kept once.

## Options

| option | |
|---|---|
| `-o FOLDER` | the knowledge base; with one document, `-o note.md` writes just that note, and without `-o` it goes to standard output |
| `--prune` | remove the notes of documents that are gone |
| `--force` | convert everything again, and replace notes edited by hand |
| `-n`, `--dry-run` | show what would change |
| `--pdf-text auto\|always\|never` | a PDF page's own text: where it can be trusted (default), always, or never |
| `--no-pictures`, `--no-ocr` | see above |
| `--assets` | keep pictures beside the notes |
| `--max-rows N` | rows of a sheet or CSV written out, 5000 by default, 0 for all |
| `--name NAME` | the file name of a document read from standard input (`-`) |

Engine options — `--models`, `--device`, `--workers`, `--threads`, `--dpi`,
`--password`, `--max-pixels` — are the same as for `ocrust scan`.

## From Python

```python
from ocrust import markdown

note = markdown.convert("Angebot.docx")
note.markdown        # the whole note, front matter and all
note.meta            # the properties, as a dict
note.body            # the text without the front matter

note = markdown.convert(data, name="scan.pdf", pdf_text="always", assets=True)
note.assets          # {"image1.png": b"…"} for the pictures kept

result = markdown.export(["archiv/"], "wissen/", prune=True)
result.written       # [(document, note), …]
result.kept          # notes left alone, and why
result.failed        # documents that could not be converted, and why
```

`convert` raises `markdown.ConversionError` for a broken file; `export`
reports it and goes on. Pass an `ocrust.Ocr` as `engine=` to recognize scans
with your own settings — languages, device, workers.

## Feeding it to a knowledge system

- **Obsidian**: open the output folder as a vault. Properties, search and graph
  work as they are; `.ocrust/` stays hidden.
- **Retrieval (RAG)**: load the folder with a Markdown loader that keeps front
  matter as metadata, and split along headings (LangChain's
  `MarkdownHeaderTextSplitter`, LlamaIndex's `MarkdownNodeParser`). Keep
  `source` and the page markers in the chunks, and every answer can say which
  document and which page it is from. Filter on `language`, `source_type` or
  `date`; skip or down-weight chunks from notes with a low `ocr_quality`.
- **A nightly sync**: `ocrust markdown //fileserver/akten -o /srv/wissen
  --prune -q` — exit status 1 when a document could not be converted, the
  others are written regardless.
