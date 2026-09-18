# CLI

The wheel installs an `ocrust` command.

```console
$ ocrust --help
usage: ocrust [-h] [--version]
              {scan,pdf,ocr,tiff,languages,install-models,doctor,models} ...

Fast document OCR: images, multi-page TIFF and PDF.
```

| Command | Purpose |
|---|---|
| `scan` | read documents, write text / Markdown / JSON / hOCR / ALTO / CSV |
| `ocr` | add an invisible text layer to an existing PDF, pages untouched |
| `pdf` | build a new searchable PDF from images |
| `tiff` | write a deskewed multi-page TIFF, optionally with a text sidecar |
| `languages` | what the installed model covers |
| `models` | which model files would be used |
| `install-models` | download the bundled models from GitHub |
| `doctor` | runtime and model diagnostics |

## `ocrust scan`

```bash
ocrust scan invoice.pdf                       # text on stdout
ocrust scan scan.tiff -f markdown             # headings, lists, paragraphs
ocrust scan page.png -f json -o page.json
ocrust scan *.jpg -f hocr -o out/             # one file per input
ocrust scan archive/ -f text -o sidecars/     # a directory, read recursively
ocrust scan 'scans/*.pdf' -o out/             # a pattern, even where the shell keeps it
ocrust scan book.pdf --pages 1,3-5            # 1-based on the command line
ocrust scan faint.png --min-confidence 0.3    # keep faint lines
ocrust scan book.pdf --progress               # page-by-page on stderr
ocrust scan '\\\\fileserver\\scans' --io-retries 5   # a share that drops connections
ocrust scan scan.pdf --dpi 300 --lang de,fr
ocrust scan photo.jpg -q                      # no summary line
```

Inputs may be files, directories or glob patterns — including UNC paths like
`\\\\fileserver\\scans`, where reads and writes are retried through a dropped
connection ([Network shares](Network-shares.md)). A directory is walked
recursively and filtered to readable extensions (images, TIFF, PDF); a pattern is
expanded by `ocrust` itself, which is what makes `ocrust scan '*.pdf'` work on
Windows too. Everything is sorted and de-duplicated, so a batch writes the same
output twice in a row. A directory that contains nothing readable is an error, not
a silent success.

```console
$ ocrust scan rechnung.png
RECHNUNG Nr. 2026-04-1187
Kleindienst Maschinenbau GmbH
Industriestraße 14, 85748 Garching
…
  1 page(s), 11 line(s), confidence 99.0%, 1146 ms
```

The summary goes to stderr, so `ocrust scan x.png > x.txt` gives you a clean
file and still tells you what happened. `-q` suppresses it.

Output rules worth knowing:

- `-o` with a suffix (`out.json`) is a **file**; without one (`out/`, `results`)
  it is a **directory**, and each input gets its own file named after it. That is
  why `-f json -o results` over ten inputs writes ten files instead of
  overwriting one.
- Several inputs default to `--workers 4`; a single input to 1. Pages and
  documents share the cores with the inference threads, so more is not better —
  see [Performance](Performance.md).
- `--pages` is 1-based and accepts ranges: `1,3-5,9`. The Python API is
  0-based, as Python should be.

## `ocrust ocr` — text layer over an existing PDF

```bash
ocrust ocr scan.pdf                      # -> scan.ocr.pdf
ocrust ocr scan.pdf -o searchable.pdf
ocrust ocr scan.pdf --dpi 300            # small print
ocrust ocr scan.pdf --dry-run            # decide per page, change nothing
ocrust ocr scan.pdf --force              # also OCR pages that already have text
ocrust ocr archive.pdf --lang de,fr --workers 4
```

```console
$ ocrust ocr archive.pdf --dry-run
page 1: 595x842 pt -> ocr
page 2: 595x842 pt, rotated 90deg -> ocr
page 3: 595x842 pt -> skip (has text)

2 of 3 page(s) would get a text layer
```

Full details in [PDF workflows](PDF-workflows.md).

## `ocrust pdf` — searchable PDF from images

```bash
ocrust pdf photo.jpg                     # -> photo.ocr.pdf
ocrust pdf scan.tiff -o scan.pdf --dpi 300 --quality 90
```

Use it for phone photos and loose scans. When a PDF already exists, use
`ocrust ocr` instead — it keeps the original bytes.

## `ocrust tiff` — archive copy

```bash
ocrust tiff scan.pdf                             # -> scan.ocr.tiff
ocrust tiff scan.pdf --gray --sidecar text       # plus scan.ocr.txt
ocrust tiff photo.jpg --sidecar alto -o out.tiff
```

The pages written are the preprocessed ones: deskewed, upright, inverted back if
they were light-on-dark. `--gray` halves the size for bitonal scans.

## `ocrust languages`

```console
$ ocrust languages
27 language(s) covered by the installed model (18709 characters):

  greek       el (Greek)
  han         zh (Chinese (Simplified)), zh-hant (Chinese (Traditional))
  kana        ja (Japanese)
  latin       cs (Czech), da (Danish), de (German), en (English), …

nearly covered (a few characters missing):
  vi (Vietnamese): 98%, missing ạ ả
```

`--all` lists every language `ocrust` can check, covered or not; `--json` gives
machine-readable output.

## `ocrust doctor`

```console
$ ocrust doctor
ocrust            0.1.0
python            3.11.15 on linux
onnxruntime       1.30.0
  library         .../onnxruntime/capi/libonnxruntime.so.1.30.0
  loaded          ONNX Runtime (API level 22)
models dir        .../ocrust_models/models
models cache      /root/.cache/ocrust/models
  detection      .../ppocrv6_det.onnx
  recognition    .../ppocrv6_rec.onnx
  orientation    .../ppocr_cls.onnx
  dictionary     -

status: ready
```

Exit code 0 means ready; anything else is a real problem, and the line above
`status:` says which. `--json` for scripts. [Troubleshooting](Troubleshooting.md) decodes each case.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | everything worked |
| 1 | a file failed to scan, or the engine could not be built |
| 2 | bad arguments: an input that does not exist, or `-o file.json` with several inputs |

A batch keeps going after a failure and reports it on stderr, so one corrupt
scan in a thousand does not abort the run — the exit code still says 1.

## Shell recipes

```bash
# every PDF in an archive becomes searchable, in place, keeping the original
find archive -name '*.pdf' -print0 | xargs -0 -n1 -P4 ocrust ocr

# text sidecars for a folder of scans
ocrust scan scans/*.tiff -f text -o sidecars/

# a CSV of every line with its box and score, for a spreadsheet
ocrust scan form.pdf -f csv -o form.csv

# JSON into jq: the lines the engine was unsure about
ocrust scan scan.pdf -f json | jq '.pages[].blocks[].lines[] | select(.confidence < 0.8) | .text'
```
