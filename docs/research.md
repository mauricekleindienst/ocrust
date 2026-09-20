# Model landscape (research notes, September 2026)

Notes behind the engine choice. Sources are linked; benchmark numbers are the
ones the publishers report, not measurements of this project.

## Where OCR stands

Two families are worth considering today.

**1. Specialist pipelines (detect → classify → recognize).** The PP-OCR line is
the reference: [PaddleOCR 3.0](https://arxiv.org/pdf/2507.05595) documents the
v5 pipeline (image preprocessing, text detection, text-line orientation, text
recognition), and [PP-OCRv6](https://arxiv.org/pdf/2606.13108) reports that
34.5M parameters beat billion-scale VLMs on OCR tasks. Models come in a *mobile*
variant for CPU and a *server* variant for accelerators.

**2. Document vision-language models.** These read a page image and emit
structured Markdown, including tables, formulas and reading order.
[PaddleOCR-VL-1.6](https://blog.roboflow.com/best-open-source-ocr-models/)
reports 96.34% on OmniDocBench v1.6 with a 0.9B-parameter model; MinerU2.5,
GLM-OCR, [LightOnOCR-1B](https://arxiv.org/pdf/2601.14251) and
[Qwen2.5-VL](https://arxiv.org/pdf/2502.13923) target the same task at different
sizes.

## What this project ships

**PP-OCRv6 mobile** (detection 9.5 MB, recognition 21 MB, orientation 0.6 MB).
The recognizer has 18 708 classes and covers 26 languages completely — all of
Western and Central Europe, Japanese and both Chinese scripts. Greek is *not*
among them: the charset holds the plain Greek letters but no accented vowel and
no final sigma, so the language cannot be written with it. Its
predecessor v5 (18 383 classes) is close but misses a handful of accents
(`î ï œ À` for French, `ś ź` for Polish, `ď ť ů` for Czech), which is why v6 is
the default.

The character set is embedded in the ONNX file, so no dictionary file has to be
shipped or matched.

## Why this project runs a specialist pipeline

The goal is a library that installs with one `pip install` and runs anywhere,
including CPU-only CI containers. Measured against that:

| | PP-OCR (mobile, ONNX) | Document VLM (~1B) |
|---|---|---|
| Model size | ~16 MB total | 1–2 GB |
| Runtime | ONNX Runtime, CPU-friendly | practically needs a GPU |
| Latency per page | tens of milliseconds | seconds |
| Output | lines, boxes, confidences | Markdown with structure |
| Determinism | deterministic | sampling, can hallucinate text |

A VLM is the better tool for *document understanding* (tables, formulas,
key-value extraction). It is the wrong default for a general-purpose OCR library
that has to be small, fast, offline-capable and faithful to the pixels — a
hallucinated invoice total is worse than a missing one.

The engine therefore runs PP-OCR-family ONNX models and keeps geometry and
per-line confidences, which is what downstream tooling (hOCR, ALTO, searchable
PDFs, spot-checking) needs. A VLM backend is a natural addition for
structure-heavy documents; the [architecture](architecture.md) keeps the model
stage isolated so it can be added without touching ingest, layout or export.

## Distribution: one source, no toolchain

A library is only as installable as its weakest dependency. Three decisions
follow from that:

- **Models live in this repository** and are installed from a wheel, or fetched
  from `raw.githubusercontent.com` with SHA-256 verification. Upstream model
  hosts (`huggingface.co`, `modelscope.cn`, `bcebos.com`) are commonly blocked by
  corporate proxies, and a first run that cannot download is a failed install.
- **No C toolchain for users.** The wheels are prebuilt, and ONNX Runtime is loaded
  dynamically from the `onnxruntime` wheel rather than linked. `pip install`
  therefore needs no compiler, no CMake and no admin rights.
- **Prebuilt abi3 wheels** for Linux (x86-64, aarch64), macOS (arm64, x86-64) and
  Windows, one per platform for every Python from 3.9 up.

## Runtime and PDF choices

- **ONNX Runtime via [`ort`](https://crates.io/crates/ort)** (dynamically
  loaded). The `onnxruntime` PyPI wheel already ships `libonnxruntime` for every
  platform, so depending on it means no system packages and no vendored binary
  in our own wheel. CUDA, CoreML and DirectML are available behind cargo
  features.
- **PDF with [`hayro`](https://crates.io/crates/hayro)**, a pure-Rust
  rasterizer. PDFium and Poppler would each mean a native dependency in the
  wheel — the exact problem this project exists to avoid.
- **Multi-page TIFF with the [`tiff`](https://crates.io/crates/tiff) crate**,
  because `image` only exposes the first page and dropping pages silently would
  be worse than not supporting the format.

## Implementation references

- DB post-processing (threshold → contours → minimum-area rectangles → unclip)
  follows PaddleOCR's `DBPostProcess`; the unclip distance is
  `area * ratio / perimeter`, applied along the box's own axes.
- Recognition normalizes to `(x/255 - 0.5) / 0.5` at 48 px height, batches crops
  by aspect ratio, and decodes CTC greedily with class 0 as the blank and an
  optional trailing space class (`use_space_char`).
- The character set is read from the ONNX metadata key `character` when no
  dictionary file is supplied, which is how recent PP-OCR exports ship.
- Per-language letter sets were cross-checked against PaddleOCR's own
  dictionaries (`ppocr/utils/dict/{german,french,it,latin,cyrillic,…}_dict.txt`).
  Those files are unions of training data and contain stray characters from other
  scripts, so the tables here are the languages' actual alphabets.

## Observed model behaviour

Worth knowing, and covered by the test suite:

- PP-OCRv6 transcribes the uppercase ligature `Æ` as `AE`. Lowercase `æ` is
  returned correctly.
- Vietnamese is at 55% coverage: the bundle has 58 of the 146 precomposed
  tone-marked forms of quốc ngữ, so the language cannot be written with it. It
  was reported as a 98% near miss until the entry listed the alphabet instead of
  a sample of it.
- `ẞ` is absent, and German is covered anyway: the language does not require it
  (`ß` uppercases to `SS`), and the recognizer returns `STRAßE` for `STRAẞE`.
  It is reported as a substitution rather than refused.
- Cyrillic, Arabic and Devanagari are not covered by this bundle at all and need
  a script-specific recognition model.
