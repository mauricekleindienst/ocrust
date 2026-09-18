# Quickstart

Five calls cover most work.

## 1. Text out of anything

```python
import ocrust

text = ocrust.read("invoice.pdf")      # PDF, PNG, JPEG, TIFF, WebP, BMP, …
```

## 2. The full result

```python
doc = ocrust.scan("scan.jpg")

doc.text                # reading order applied
doc.confidence          # mean line confidence, 0..1
len(doc.pages)

for line in doc.lines:
    print(line.text, round(line.confidence, 3), line.box.as_tuple())
    for word in line.words:
        print("   ", word.text, word.box.as_tuple())
```

## 3. Reuse the engine

Building one loads the models; keep it and call it repeatedly.

```python
ocr = ocrust.Ocr(lang="de,fr", pdf_dpi=240)

for path in paths:
    print(ocr.read(path))
```

## 4. Other formats

```python
doc.markdown()     # headings, lists, paragraphs
doc.json()         # every box, every score
doc.hocr()         # hOCR for downstream tooling
doc.alto()         # ALTO XML for archives
doc.csv()          # one row per line
```

## 5. Make a scanned PDF searchable

```python
pdf, report = ocrust.ocr_pdf("scan.pdf")
open("scan.ocr.pdf", "wb").write(pdf)
print(report)
# {'pages': 12, 'pages_with_layer': 9, 'pages_skipped': 3, 'lines': 214,
#  'unmappable_chars': 0}
```

The pages keep their images and compression; only an invisible text layer is
added. Pages that already contain text are skipped. See [[PDF workflows]].

## The same from the shell

```bash
ocrust scan invoice.pdf                      # text on stdout
ocrust scan *.tiff -f json -o results/       # batch, one file per input
ocrust ocr scan.pdf -o scan.ocr.pdf          # text layer over the original
ocrust tiff scan.pdf --gray --sidecar text   # archive TIFF plus its text
ocrust languages                             # what the model covers
```

## Try it in a browser first

```bash
python examples/app.py        # http://127.0.0.1:8765
```

The repository ships a dependency-free web app: drop a file in, see the text, the
boxes over the image, every export format, search with highlighted hits and a
one-click searchable PDF. It is also a small HTTP API (`POST /scan`, `POST /pdf`,
`GET /languages`), which makes it a quick way to check an installation.

## Declaring a language is a check

```python
ocr = ocrust.Ocr(lang="de")     # fine
ocr = ocrust.Ocr(lang="ru")     # OcrustError: cannot write А Б В Г Д Е Ж З И Й …
```

A model that cannot spell `ö` and `ß` does not fail on German text — it quietly
returns `Grusse` for `Grüße`. `ocrust` refuses instead. See [[Languages]].
