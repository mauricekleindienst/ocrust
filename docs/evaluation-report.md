# Corpus evaluation

- corpus: **122 files**, 207 recognized pages, 417 MB
- models: `ppocrv6_rec.onnx`, 18709 characters, 26 languages
- runtime: 1.30.0 · 4 CPU cores
- total scan time: **152.2 s** (735 ms per page)
- accuracy over 108 files with ground truth: **mean CER 0.029**, median 0.003, mean WER 0.089, **mean word recall 0.922**
- held out of those figures: 8 file(s) in a script the bundled model cannot write (mean CER 0.137) — see below
- failures: 5 total, **0 unexpected**, 0 broken file(s) that did not error

## Scripts the bundled model cannot write

Scanned and scored, but excluded from every figure above: `ocrust languages` does not offer these, and asking for one fails with the characters it cannot emit. They are here so the cost of that gap is on the record rather than folded into an average.

| file | language | CER | WER | word recall | confidence |
|---|---|---:|---:|---:|---:|
| `clean/el_200dpi.pdf` | el | 0.192 | 0.464 | 0.536 | 0.916 |
| `clean/el_200dpi.png` | el | 0.178 | 0.464 | 0.536 | 0.940 |
| `clean/el_300dpi.pdf` | el | 0.183 | 0.464 | 0.536 | 0.916 |
| `clean/el_300dpi.png` | el | 0.168 | 0.464 | 0.536 | 0.937 |
| `clean/vi_200dpi.pdf` | vi | 0.106 | 0.452 | 0.548 | 0.961 |
| `clean/vi_200dpi.png` | vi | 0.083 | 0.405 | 0.595 | 0.977 |
| `clean/vi_300dpi.pdf` | vi | 0.092 | 0.405 | 0.595 | 0.976 |
| `clean/vi_300dpi.png` | vi | 0.092 | 0.405 | 0.595 | 0.982 |

## By category

| category | files | ok | pages | ms/page | mean CER | median CER | mean WER | word recall | confidence |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| aged | 8 | 8 | 8 | 642 | 0.006 | 0.006 | 0.063 | 0.937 | 0.986 |
| aged-pdf | 8 | 8 | 8 | 658 | 0.008 | 0.007 | 0.095 | 0.905 | 0.986 |
| blank | 1 | 1 | 1 | 275 | — | — | — | — | — |
| born-digital | 2 | 2 | 4 | 583 | 0.002 | 0.002 | 0.010 | 0.990 | 0.993 |
| broken | 6 | 1 | 1 | 657 | 0.000 | 0.000 | 0.000 | 1.000 | 0.995 |
| clean | 16 | 16 | 16 | 616 | 0.010 | 0.002 | 0.103 | 0.897 | 0.982 |
| clean-pdf | 16 | 16 | 16 | 629 | 0.017 | 0.006 | 0.165 | 0.835 | 0.976 |
| drawing | 4 | 4 | 4 | 502 | 0.170 | 0.168 | 0.276 | 0.855 | 0.986 |
| drawing-pdf | 4 | 4 | 4 | 563 | 0.166 | 0.166 | 0.250 | 0.882 | 0.984 |
| fax | 4 | 4 | 4 | 529 | 0.014 | 0.013 | 0.133 | 0.867 | 0.978 |
| fax-pdf | 4 | 4 | 4 | 608 | 0.014 | 0.015 | 0.109 | 0.897 | 0.975 |
| form | 4 | 4 | 4 | 585 | 0.063 | 0.063 | 0.256 | 0.744 | 0.990 |
| format | 8 | 8 | 8 | 613 | 0.001 | 0.000 | 0.023 | 0.977 | 0.994 |
| huge | 1 | 1 | 1 | 26176 | 0.147 | 0.147 | 0.211 | 0.895 | 0.989 |
| inverted | 1 | 1 | 1 | 617 | 0.003 | 0.003 | 0.020 | 0.980 | 0.994 |
| long | 1 | 1 | 1 | 3077 | 0.000 | 0.000 | 0.000 | 1.000 | 0.992 |
| multipage-pdf | 3 | 3 | 47 | 625 | 0.004 | 0.004 | 0.043 | 0.957 | 0.988 |
| multipage-tiff | 3 | 3 | 47 | 595 | 0.003 | 0.003 | 0.017 | 0.983 | 0.991 |
| newspaper | 8 | 8 | 8 | 510 | 0.000 | 0.000 | 0.000 | 1.000 | 0.996 |
| pricelist | 4 | 4 | 4 | 463 | 0.000 | 0.000 | 0.000 | 1.000 | 0.999 |
| pricelist-pdf | 4 | 4 | 4 | 488 | 0.000 | 0.000 | 0.000 | 1.000 | 0.998 |
| receipt | 4 | 4 | 4 | 314 | 0.183 | 0.183 | 0.059 | 0.941 | 0.995 |
| rotated-pdf | 3 | 3 | 3 | 826 | 0.002 | 0.000 | 0.029 | 0.971 | 0.992 |
| skewed | 4 | 4 | 4 | 737 | 0.000 | 0.000 | 0.000 | 1.000 | 0.992 |
| tiny | 1 | 1 | 1 | 426 | 0.006 | 0.006 | 0.111 | 0.889 | 0.992 |

## By language

| language | files | mean CER | mean WER | word recall | confidence |
|---|---:|---:|---:|---:|---:|
| cs | 4 | 0.008 | 0.058 | 0.942 | 0.987 |
| de | 70 | 0.037 | 0.072 | 0.945 | 0.991 |
| en | 14 | 0.008 | 0.064 | 0.936 | 0.986 |
| fr | 8 | 0.006 | 0.055 | 0.945 | 0.985 |
| ja | 4 | 0.065 | 0.672 | 0.328 | 0.986 |
| pl | 8 | 0.004 | 0.046 | 0.954 | 0.988 |

## Hardest files

| file | category | CER | WER | word recall | confidence | ms |
|---|---|---:|---:|---:|---:|---:|
| `drawings/drawing_01.png` | drawing | 0.206 | 0.342 | 0.763 | 0.988 | 493 |
| `drawings/drawing_01.pdf` | drawing-pdf | 0.197 | 0.316 | 0.789 | 0.982 | 529 |
| `drawings/drawing_03.pdf` | drawing-pdf | 0.197 | 0.316 | 0.789 | 0.985 | 520 |
| `drawings/drawing_03.png` | drawing | 0.197 | 0.289 | 0.816 | 0.984 | 483 |
| `receipts/receipt_00.png` | receipt | 0.183 | 0.059 | 0.941 | 0.995 | 321 |
| `receipts/receipt_01.png` | receipt | 0.183 | 0.059 | 0.941 | 0.995 | 309 |
| `receipts/receipt_02.png` | receipt | 0.183 | 0.059 | 0.941 | 0.995 | 333 |
| `receipts/receipt_03.png` | receipt | 0.183 | 0.059 | 0.941 | 0.995 | 292 |
| `extremes/a0_drawing_300dpi.png` | huge | 0.147 | 0.211 | 0.895 | 0.989 | 26176 |
| `drawings/drawing_00.png` | drawing | 0.139 | 0.237 | 0.921 | 0.986 | 518 |
| `drawings/drawing_02.png` | drawing | 0.139 | 0.237 | 0.921 | 0.986 | 515 |
| `drawings/drawing_00.pdf` | drawing-pdf | 0.134 | 0.184 | 0.974 | 0.985 | 602 |

## Slowest files

| file | bytes | pages | seconds | pages/s | MB/s |
|---|---:|---:|---:|---:|---:|
| `extremes/a0_drawing_300dpi.png` | 0.3 MB | 1 | 26.18 | 0.04 | 0.0 |
| `multipage/scan_30p.tiff` | 195.6 MB | 30 | 18.59 | 1.61 | 10.5 |
| `multipage/scan_30p.pdf` | 2.5 MB | 30 | 18.51 | 1.62 | 0.1 |
| `multipage/scan_12p.pdf` | 1.0 MB | 12 | 7.45 | 1.61 | 0.1 |
| `multipage/scan_12p.tiff` | 78.3 MB | 12 | 7.17 | 1.67 | 10.9 |
| `multipage/scan_5p.pdf` | 0.4 MB | 5 | 3.18 | 1.57 | 0.1 |
| `extremes/long_receipt.png` | 0.2 MB | 1 | 3.08 | 0.32 | 0.1 |
| `multipage/scan_5p.tiff` | 32.6 MB | 5 | 2.84 | 1.76 | 11.5 |
| `borndigital/report_text_3p.pdf` | 0.0 MB | 3 | 1.67 | 1.80 | 0.0 |
| `rotated/pdf_rotate_90.pdf` | 0.1 MB | 1 | 0.93 | 1.07 | 0.1 |

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
| markdown | 310 | 0.2 |
| json | 26287 | 0.3 |
| hocr | 6730 | 0.2 |
| alto | 6580 | 0.2 |
| csv | 736 | 0.2 |

## PDF text layer

- 45 PDFs processed, 44 succeeded
- 86 page(s) got a layer, 4 skipped because they already had text
- mean time 1.12 s per document
- mean growth 3.1 KB
- 0 character(s) outside WinAnsi in the text layer
- 42 document(s) without a detectable text layer

| file | error |
|---|---|
| `broken/png_named_pdf.pdf` | OcrustError: pdf error: could not parse PDF: couldn't parse input |

## Archive TIFF

| file | pages | colour | greyscale | seconds |
|---|---:|---:|---:|---:|
| `aged/aged_00_de.pdf` | 1 | 2.6 MB | 1.1 MB | 0.86 |
| `aged/aged_01_en.pdf` | 1 | 4.4 MB | 2.0 MB | 0.80 |
| `aged/aged_02_fr.pdf` | 1 | 5.8 MB | 2.5 MB | 0.90 |
| `aged/aged_03_pl.pdf` | 1 | 4.6 MB | 2.2 MB | 0.76 |
| `aged/aged_04_de.pdf` | 1 | 3.4 MB | 1.8 MB | 0.82 |
| `aged/aged_05_en.pdf` | 1 | 4.4 MB | 2.0 MB | 0.71 |

## Worker scaling

On `multipage/scan_12p.pdf`:

| workers | seconds | pages/s | speedup |
|---:|---:|---:|---:|
| 1 | 7.47 | 1.61 | 1.00x |
| 2 | 5.94 | 2.02 | 1.26x |
| 4 | 5.23 | 2.30 | 1.43x |
| 8 | 5.59 | 2.15 | 1.34x |

## DPI sweep

Over 6 PDFs (clean, aged and fax):

| dpi | mean CER | median CER | word recall | seconds |
|---:|---:|---:|---:|---:|
| 100 | 0.003 | 0.003 | 0.969 | 3.4 |
| 150 | 0.004 | 0.003 | 0.964 | 3.5 |
| 200 | 0.007 | 0.007 | 0.910 | 3.8 |
| 300 | 0.002 | 0.003 | 0.980 | 4.2 |

## Does preprocessing pay off?

Over 13 skewed, aged and inverted pages:

| preprocessing | mean CER | word recall | seconds |
|---|---:|---:|---:|
| on | 0.004 | 0.960 | 7.9 |
| off | 0.040 | 0.960 | 7.4 |

## Batch API

20 files: 12.1 s one by one versus 11.8 s with `scan_many` (**1.03x**).

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
