# Bundled OCR models

These files are the only thing `ocrust` needs besides the wheel itself. They live
in this repository on purpose: a corporate proxy that allows GitHub but blocks
Hugging Face, ModelScope or `bcebos.com` can still install and run the library,
and nothing has to be fetched at first use.

| File | What it does | Size |
|---|---|---|
| `ppocrv6/ppocrv6_det.onnx` | PP-OCRv6 text detection (DB) | 9.5 MB |
| `ppocrv6/ppocrv6_rec.onnx` | PP-OCRv6 text recognition, 18 708 classes | 21 MB |
| `ppocrv6/ppocr_cls.onnx` | 180° text-line orientation | 0.6 MB |

`ppocrv6.json` is the manifest `ocrust models install` uses: it carries a
SHA-256 for every file and a `raw.githubusercontent.com` URL pointing back here.

## Language coverage

The recognizer's character set is embedded in the ONNX file, so no dictionary
file is needed. `ocrust languages` prints what it covers; at the time of writing
that is complete coverage for English, German, French, Spanish, Italian,
Portuguese, Dutch, Swedish, Danish, Norwegian, Finnish, Polish, Czech, Slovak,
Hungarian, Romanian, Turkish, Croatian, Slovenian, Estonian, Latvian,
Lithuanian, Greek, Japanese, Korean and Chinese (Simplified and Traditional).

Cyrillic (Russian, Ukrainian, Bulgarian, Serbian), Arabic and Devanagari need a
script-specific recognition model; drop one into a directory and point
`OCRUST_MODELS_DIR` at it, or pass `Ocr(recognition_model=…, dictionary=…)`.

## Provenance and license

The models are PP-OCR releases by the PaddleOCR project, licensed
**Apache-2.0** — the same license as this repository. They were taken from the
PyPI distribution `rapidocr` 3.9.2 (`rapidocr/models/`), which redistributes the
PaddleOCR ONNX exports unmodified:

| File here | Original name |
|---|---|
| `ppocrv6_det.onnx` | `PP-OCRv6_det_small.onnx` |
| `ppocrv6_rec.onnx` | `PP-OCRv6_rec_small.onnx` |
| `ppocr_cls.onnx` | `ch_ppocr_mobile_v2.0_cls_mobile.onnx` |

Upstream: <https://github.com/PaddlePaddle/PaddleOCR> ·
<https://github.com/RapidAI/RapidOCR>

The files are byte-identical to those distributions; verify with the SHA-256
values in `ppocrv6.json`.
