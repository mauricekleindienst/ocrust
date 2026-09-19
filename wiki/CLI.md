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
curl -s https://host/invoice.pdf | ocrust scan -    # from stdin
ocrust scan in/ -o out/ --skip-existing        # resume where a run stopped
ocrust scan inbox/ -o out/ --watch             # scan files as they arrive
ocrust scan book.pdf --memory fast             # more RAM, ~10% less time
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
  0-based, as Python should be. Asking for a page the document does not have is
  an error, not an empty file; a page spec that is not a number or a range is a
  bad argument and exits 2.
- `-q` works on `scan`, `ocr`, `pdf` and `tiff`: it silences the summary on
  stderr, which is what a `find | xargs -P4` pipeline wants.

### Resuming a batch

```console
$ ocrust scan archive/ -o out/ --skip-existing
  archive/0001.pdf: already written
  archive/0002.pdf: already written
archive/0003.pdf -> out/0003.txt
  4 pages, 88 lines, quality 97%, 6.2 s
done: 1 file, 4 pages, 88 lines, 6.2 s, 2 already written
```

`--skip-existing` compares against the file the run *would* write, format
included, so a text pass does not make a later `-f markdown` pass think it is
finished. It needs `-o`: without an output path there is nothing to compare.

### Reading from stdin

`-` reads one document from standard input — `curl … | ocrust scan -`, a scanner
writing to a pipe, `pdftk … output - | ocrust scan -`. The bytes are read whole,
because a PDF cannot be decoded in pieces. With `-o` pointing at a directory the
result is written as `stdin.<ext>`. A file genuinely called `-` is reachable as
`./-`.

### Watching a directory

```console
$ ocrust scan inbox/ -o out/ --watch
watching inbox — press Ctrl-C to stop
inbox/scan_0001.pdf -> out/scan_0001.txt
  2 pages, 47 lines, quality 98%, 3.1 s
^C
stopped after 1 file
```

For a folder a scanner or a colleague drops files into. Every file is scanned
once — even if it is still there next round — and a file is left alone until its
size stops changing between checks, because half a PDF is not a PDF.
`--watch-interval` sets how often it looks (default 2 s). Ctrl-C is the normal
way out and exits 0 unless a scan failed. It needs a directory: watching a single
file has nothing to wait for.

### Shell completions

```bash
ocrust completions bash       > /etc/bash_completion.d/ocrust
ocrust completions zsh        > "${fpath[1]}/_ocrust"
ocrust completions fish       > ~/.config/fish/completions/ocrust.fish
ocrust completions powershell >> $PROFILE
```

The scripts are generated from the argument parser itself, so they list the flags
this version actually has rather than the ones it had when someone last
remembered to update them. zsh and fish also get each flag's help text and the
value sets for `--format`, `--compression` and the rest.

### Memory

`--memory frugal` is the default and keeps peak memory down by telling ONNX
Runtime not to hold an allocation arena or plan tensor reuse — both assume the
tensor shapes repeat, and pages are all different sizes. Over a 40-page PDF that
is 253 MB against 584 MB, for about 10% more time at one worker and no extra time
at four. `--memory fast` buys the time back. Page count does not affect either:
pages are rasterized one at a time. See [Performance](Performance.md).

## `ocrust ocr` — text layer over an existing PDF

```bash
ocrust ocr scan.pdf                      # -> scan.ocr.pdf
ocrust ocr scan.pdf -o searchable.pdf
ocrust ocr scan.pdf --dpi 300            # small print
ocrust ocr scan.pdf --dry-run            # decide per page, change nothing
ocrust ocr scan.pdf -q                   # no summary line, for xargs pipelines
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
26 language(s) covered by the installed model (18709 characters):

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

## Output design

The result goes to **stdout**, everything about the run to **stderr**, so
`ocrust scan x.pdf > text.txt` gives you the text and nothing else, and
`ocrust scan x.pdf -f json | jq` works.

```console
$ ocrust scan archive/ -o out/
archive/one.pdf -> out/one.txt
  1 page, 2 lines, 99.9% confident, 991 ms
archive/sub/two.pdf -> out/two.txt
  1 page, 2 lines, 99.9% confident, 810 ms
done: 2 files, 2 pages, 4 lines, 1.8 s
```

Colour is used sparingly — a red `ocrust:` on an error, the confidence green,
amber or red, paths in bold — and switches itself off when stderr is not a
terminal. `NO_COLOR` disables it; `FORCE_COLOR=1` keeps it for a CI log that
renders ANSI. Long messages fold to the terminal width instead of running off
the edge, and `--progress` rewrites one line on a terminal but prints a line per
page into a log, where a carriage return would run the whole run together.

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
