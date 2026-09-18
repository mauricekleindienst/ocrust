# ocrust

**Document OCR for Python, with a Rust core.** Images, multi-page TIFF and PDF in
— text, Markdown, JSON, hOCR, ALTO, CSV, multi-page TIFF or a searchable PDF out.
27 languages. One `pip install`, no system dependencies.

```bash
pip install "ocrust[models]"
```

```python
import ocrust

print(ocrust.read("rechnung.pdf"))
```

```text
RECHNUNG 2026-0042
Grüße aus München
Betrag: 1.299,90 EUR
Français: déjà payé
```

## Start here

| Page | What it covers |
|---|---|
| **[[Installation]]** | `pip install`, offline installs, GPU builds, what needs no admin rights |
| **[[Quickstart]]** | The five calls that cover most work |
| **[[Python API]]** | `Ocr`, `Document`, every keyword argument, result objects |
| **[[CLI]]** | `scan`, `ocr`, `pdf`, `tiff`, `languages`, `models`, `doctor` |
| **[[PDF workflows]]** | Adding a text layer to existing PDFs versus building one from images |
| **[[Languages]]** | The 27 covered languages, and why declaring one is a check |
| **[[Models]]** | Where models come from, how to bring your own, the one-GitHub-source rule |
| **[[Performance]]** | Measured numbers, what the knobs do, worker scaling |
| **[[Accuracy]]** | Error rates per document type, and where the engine is weak |
| **[[Architecture]]** | The pipeline, module by module |
| **[[Evaluation]]** | The generated corpus and how to reproduce the numbers |
| **[[Troubleshooting]]** | Error messages and what they mean |
| **[[Contributing]]** | Building, testing, the checks CI runs |
| **[[Roadmap]]** | What is missing, in the order it matters |

## Why it exists

Installing OCR in Python is usually harder than the OCR. `ocrust` is built around
three rules:

1. **No native prerequisites.** No Tesseract binary, no PaddlePaddle, no PyTorch,
   no Poppler, no PDFium. The engine is one prebuilt wheel.
2. **No admin rights and no compiler.** Everything is a wheel; the Rust side has
   no C, C++ or CMake dependency at all.
3. **One source for everything.** Models live in the repository and install from a
   wheel or from `raw.githubusercontent.com`. A proxy that allows GitHub and PyPI
   — and nothing else — is enough. Hugging Face, ModelScope and `bcebos.com` are
   never contacted.

## At a glance

| | |
|---|---|
| Input | PNG, JPEG, WebP, BMP, GIF, PNM, TGA, DDS, HDR, OpenEXR, QOI, ICO, multi-page TIFF, PDF, `bytes`, numpy arrays, PIL images |
| Output | text, Markdown, JSON, hOCR, ALTO XML, CSV, searchable PDF, PDF text layer, multi-page TIFF |
| Languages | 27 complete (Latin, Greek, Japanese, Chinese); 35 known and checkable |
| Models | PP-OCRv6, 18 708 characters, 31 MB, Apache-2.0 |
| Speed | ~670 ms per 200 dpi A4 page on four CPU cores |
| Accuracy | median CER 0.006 over a 106-file corpus of aged scans, faxes, drawings and forms |
| License | Apache-2.0 |
