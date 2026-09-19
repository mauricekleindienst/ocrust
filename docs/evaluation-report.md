# Corpus evaluation

- corpus: **118 files**, 203 recognized pages, 416 MB
- models: `ppocrv6_rec.onnx`, 18709 characters, 26 languages
- runtime: 1.30.0 · 4 CPU cores
- total scan time: **158.0 s** (778 ms per page)
- accuracy over 108 files with ground truth: **mean CER 0.031**, median 0.006, mean WER 0.093, **mean word recall 0.918**
- held out of those figures: 4 file(s) in a script the bundled model cannot write (mean CER 0.180) — see below
- failures: 5 total, **0 unexpected**, 0 broken file(s) that did not error

## Scripts the bundled model cannot write

Scanned and scored, but excluded from every figure above: `ocrust languages` does not offer these, and asking for one fails with the characters it cannot emit. They are here so the cost of that gap is on the record rather than folded into an average.

| file | language | CER | WER | word recall | confidence |
|---|---|---:|---:|---:|---:|
| `clean/el_200dpi.pdf` | el | 0.192 | 0.464 | 0.536 | 0.916 |
| `clean/el_200dpi.png` | el | 0.178 | 0.464 | 0.536 | 0.940 |
| `clean/el_300dpi.pdf` | el | 0.183 | 0.464 | 0.536 | 0.916 |
| `clean/el_300dpi.png` | el | 0.168 | 0.464 | 0.536 | 0.937 |

## By category

| category | files | ok | pages | ms/page | mean CER | median CER | mean WER | word recall | confidence |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| aged | 8 | 8 | 8 | 679 | 0.006 | 0.006 | 0.063 | 0.937 | 0.986 |
| aged-pdf | 8 | 8 | 8 | 706 | 0.008 | 0.007 | 0.095 | 0.905 | 0.986 |
| blank | 1 | 1 | 1 | 278 | — | — | — | — | — |
| born-digital | 2 | 2 | 4 | 610 | 0.002 | 0.002 | 0.010 | 0.990 | 0.993 |
| broken | 6 | 1 | 1 | 658 | 0.000 | 0.000 | 0.000 | 1.000 | 0.995 |
| clean | 14 | 14 | 14 | 653 | 0.010 | 0.002 | 0.103 | 0.897 | 0.983 |
| clean-pdf | 14 | 14 | 14 | 657 | 0.017 | 0.006 | 0.165 | 0.835 | 0.977 |
| drawing | 4 | 4 | 4 | 577 | 0.170 | 0.168 | 0.276 | 0.855 | 0.986 |
| drawing-pdf | 4 | 4 | 4 | 627 | 0.166 | 0.166 | 0.250 | 0.882 | 0.984 |
| fax | 4 | 4 | 4 | 538 | 0.014 | 0.013 | 0.133 | 0.867 | 0.978 |
| fax-pdf | 4 | 4 | 4 | 634 | 0.014 | 0.015 | 0.109 | 0.897 | 0.975 |
| form | 4 | 4 | 4 | 625 | 0.063 | 0.063 | 0.256 | 0.744 | 0.990 |
| format | 8 | 8 | 8 | 664 | 0.001 | 0.000 | 0.023 | 0.977 | 0.994 |
| huge | 1 | 1 | 1 | 28337 | 0.147 | 0.147 | 0.211 | 0.895 | 0.989 |
| inverted | 1 | 1 | 1 | 630 | 0.003 | 0.003 | 0.020 | 0.980 | 0.994 |
| long | 1 | 1 | 1 | 3052 | 0.000 | 0.000 | 0.000 | 1.000 | 0.992 |
| multipage-pdf | 3 | 3 | 47 | 654 | 0.004 | 0.004 | 0.043 | 0.957 | 0.988 |
| multipage-tiff | 3 | 3 | 47 | 632 | 0.003 | 0.003 | 0.017 | 0.983 | 0.991 |
| newspaper | 8 | 8 | 8 | 550 | 0.000 | 0.000 | 0.000 | 1.000 | 0.996 |
| pricelist | 4 | 4 | 4 | 486 | 0.013 | 0.013 | 0.023 | 0.977 | 0.996 |
| pricelist-pdf | 4 | 4 | 4 | 508 | 0.013 | 0.013 | 0.023 | 0.977 | 0.989 |
| receipt | 4 | 4 | 4 | 348 | 0.183 | 0.183 | 0.059 | 0.941 | 0.995 |
| rotated-pdf | 3 | 3 | 3 | 866 | 0.054 | 0.079 | 0.116 | 0.884 | 0.972 |
| skewed | 4 | 4 | 4 | 746 | 0.000 | 0.000 | 0.000 | 1.000 | 0.992 |
| tiny | 1 | 1 | 1 | 409 | 0.006 | 0.006 | 0.111 | 0.889 | 0.992 |

## By language

| language | files | mean CER | mean WER | word recall | confidence |
|---|---:|---:|---:|---:|---:|
| cs | 4 | 0.008 | 0.058 | 0.942 | 0.987 |
| de | 70 | 0.041 | 0.078 | 0.939 | 0.990 |
| en | 14 | 0.008 | 0.064 | 0.936 | 0.986 |
| fr | 8 | 0.006 | 0.055 | 0.945 | 0.985 |
| ja | 4 | 0.065 | 0.672 | 0.328 | 0.986 |
| pl | 8 | 0.004 | 0.046 | 0.954 | 0.988 |

## Hardest files

| file | category | CER | WER | word recall | confidence | ms |
|---|---|---:|---:|---:|---:|---:|
| `drawings/drawing_01.png` | drawing | 0.206 | 0.342 | 0.763 | 0.988 | 545 |
| `drawings/drawing_01.pdf` | drawing-pdf | 0.197 | 0.316 | 0.789 | 0.982 | 619 |
| `drawings/drawing_03.pdf` | drawing-pdf | 0.197 | 0.316 | 0.789 | 0.985 | 603 |
| `drawings/drawing_03.png` | drawing | 0.197 | 0.289 | 0.816 | 0.984 | 548 |
| `receipts/receipt_00.png` | receipt | 0.183 | 0.059 | 0.941 | 0.995 | 342 |
| `receipts/receipt_01.png` | receipt | 0.183 | 0.059 | 0.941 | 0.995 | 345 |
| `receipts/receipt_02.png` | receipt | 0.183 | 0.059 | 0.941 | 0.995 | 350 |
| `receipts/receipt_03.png` | receipt | 0.183 | 0.059 | 0.941 | 0.995 | 353 |
| `extremes/a0_drawing_300dpi.png` | huge | 0.147 | 0.211 | 0.895 | 0.989 | 28337 |
| `drawings/drawing_00.png` | drawing | 0.139 | 0.237 | 0.921 | 0.986 | 609 |
| `drawings/drawing_02.png` | drawing | 0.139 | 0.237 | 0.921 | 0.986 | 607 |
| `drawings/drawing_00.pdf` | drawing-pdf | 0.134 | 0.184 | 0.974 | 0.985 | 644 |

## Slowest files

| file | bytes | pages | seconds | pages/s | MB/s |
|---|---:|---:|---:|---:|---:|
| `extremes/a0_drawing_300dpi.png` | 0.3 MB | 1 | 28.34 | 0.04 | 0.0 |
| `multipage/scan_30p.pdf` | 2.5 MB | 30 | 19.31 | 1.55 | 0.1 |
| `multipage/scan_30p.tiff` | 195.6 MB | 30 | 18.93 | 1.58 | 10.3 |
| `multipage/scan_12p.pdf` | 1.0 MB | 12 | 7.81 | 1.54 | 0.1 |
| `multipage/scan_12p.tiff` | 78.3 MB | 12 | 7.58 | 1.58 | 10.3 |
| `multipage/scan_5p.pdf` | 0.4 MB | 5 | 3.34 | 1.50 | 0.1 |
| `multipage/scan_5p.tiff` | 32.6 MB | 5 | 3.17 | 1.58 | 10.3 |
| `extremes/long_receipt.png` | 0.2 MB | 1 | 3.05 | 0.33 | 0.1 |
| `borndigital/report_text_3p.pdf` | 0.0 MB | 3 | 1.75 | 1.72 | 0.0 |
| `rotated/pdf_rotate_90.pdf` | 0.1 MB | 1 | 0.97 | 1.03 | 0.1 |

## Failures

| file | expected? | error |
|---|---|---|
| `broken/deflate_stream.bin` | yes | ValueError: unsupported input: /tmp/corpus3/broken/deflate_stream.bin: The image format could not be determined |
| `broken/empty.pdf` | yes | ValueError: unsupported input: /tmp/corpus3/broken/empty.pdf: The image format could not be determined |
| `broken/garbage.pdf` | yes | ValueError: unsupported input: /tmp/corpus3/broken/garbage.pdf: The image format could not be determined |
| `broken/header_only.pdf` | yes | RuntimeError: pdf error: could not parse PDF: Invalid |
| `broken/truncated.png` | yes | ValueError: unsupported input: /tmp/corpus3/broken/truncated.png: unexpected end of file |

## Export formats

Rendered from `clean/cs_200dpi.png`:

| format | characters | ms |
|---|---:|---:|
| text | 309 | 0.4 |
| markdown | 310 | 0.4 |
| json | 26280 | 0.4 |
| hocr | 6730 | 0.3 |
| alto | 6580 | 0.3 |
| csv | 736 | 0.3 |

## PDF text layer

- 43 PDFs processed, 42 succeeded
- 84 page(s) got a layer, 4 skipped because they already had text
- mean time 1.24 s per document
- mean growth 3.6 KB
- 0 character(s) outside WinAnsi in the text layer
- 40 document(s) without a detectable text layer

| file | error |
|---|---|
| `broken/png_named_pdf.pdf` | OcrustError: pdf error: could not parse PDF: couldn't parse input |

## Archive TIFF

| file | pages | colour | greyscale | seconds |
|---|---:|---:|---:|---:|
| `aged/aged_00_de.pdf` | 1 | 2.6 MB | 1.1 MB | 0.91 |
| `aged/aged_01_en.pdf` | 1 | 4.4 MB | 2.0 MB | 0.87 |
| `aged/aged_02_fr.pdf` | 1 | 5.8 MB | 2.5 MB | 1.01 |
| `aged/aged_03_pl.pdf` | 1 | 4.6 MB | 2.2 MB | 0.94 |
| `aged/aged_04_de.pdf` | 1 | 3.4 MB | 1.8 MB | 1.03 |
| `aged/aged_05_en.pdf` | 1 | 4.4 MB | 2.0 MB | 0.90 |

## Worker scaling

On `multipage/scan_12p.pdf`:

| workers | seconds | pages/s | speedup |
|---:|---:|---:|---:|
| 1 | 7.91 | 1.52 | 1.00x |
| 2 | 6.32 | 1.90 | 1.25x |
| 4 | 5.38 | 2.23 | 1.47x |
| 8 | 5.74 | 2.09 | 1.38x |

## DPI sweep

Over 6 PDFs (clean, aged and fax):

| dpi | mean CER | median CER | word recall | seconds |
|---:|---:|---:|---:|---:|
| 100 | 0.003 | 0.003 | 0.969 | 3.6 |
| 150 | 0.004 | 0.003 | 0.964 | 3.7 |
| 200 | 0.007 | 0.007 | 0.910 | 3.9 |
| 300 | 0.002 | 0.003 | 0.980 | 4.5 |

## Does preprocessing pay off?

Over 13 skewed, aged and inverted pages:

| preprocessing | mean CER | word recall | seconds |
|---|---:|---:|---:|
| on | 0.004 | 0.960 | 8.4 |
| off | 0.040 | 0.960 | 7.6 |

## Batch API

20 files: 13.2 s one by one versus 12.3 s with `scan_many` (**1.07x**).

## Sample output

**clean** (`clean/cs_200dpi.png`, CER 0.006):

> FAKTURA číslo 2026-04-1187 | Strojírny Plzeň a.s. | Škrétova 12, 301 00 Plzeň | Datum dodání: 17.03.2026 | Položka 1: vodicí lišta FS-220, počet 12 | Položka 2: kuličkové ložisko 6205-2RS, počet 48

**aged** (`aged/aged_00_de.jpg`, CER 0.003):

> RECHNUNG Nr. 2026-04-1187 | Kleindienst Maschinenbau GmbH | Industriestraße 14,85748 Garching | Lieferdatum: 17.03.2026 | Position 1: Führungsschiene FS-220, Stückzahl 12 | Position 2: Kugellager 6205-2RS, Stückzahl 48

**fax** (`fax/fax_00_de.tiff`, CER 0.011):

> RECHNUNG Nr. 2026-04-118 | Kleindienst Maschinenbau GmbH | Industriestraße 14, 85748 Garching | Lieferdatum:17.03.2026 | Position 1: Führungsschiene FS-220, Stückzahl 12 | Position 2: Kugellager 6205-2RS, Stückzahl 48

**drawing** (`drawings/drawing_00.png`, CER 0.139):

> 640 |  | ∅ 70 H7 |  | 400 | 

**receipt** (`receipts/receipt_00.png`, CER 0.183):

> SUPERMARKT AM PLATZ | Bahnhofstr. 9, 80335 |  | Milch 1L 1,19 | Brot 500g 2,49 | Kaffee 500g 6,99
