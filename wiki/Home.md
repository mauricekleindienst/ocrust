<p align="center"><img src="ocrust.svg" alt="ocrust" width="620"></p>

**Document OCR for Python, with a Rust core.** Images, multi-page TIFF and PDF in
— text, Markdown, JSON, hOCR, ALTO, CSV, multi-page TIFF or a searchable PDF out.
26 languages. One `pip install`, no system dependencies.

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
| **[Installation](Installation.md)** | `pip install`, offline installs, GPU builds, what needs no admin rights |
| **[Quickstart](Quickstart.md)** | The five calls that cover most work |
| **[Python API](Python-API.md)** | `Ocr`, `Document`, every keyword argument, result objects |
| **[CLI](CLI.md)** | `scan`, `ocr`, `pdf`, `tiff`, `vs`, `find`, `languages`, `models`, `doctor` |
| **[Search profiles](Search-profiles.md)** | `ocrust find`: any terms from a config, found however the scan broke them; sharding and resume for large jobs |
| **[Classification markings](Classification-markings.md)** | `ocrust vs`: which files are VS-NfD, GEHEIM, NATO, EU, TLP — marking told apart from mention |
| **[PDF workflows](PDF-workflows.md)** | Adding a text layer to existing PDFs, and converting anything readable into one |
| **[Network shares](Network-shares.md)** | UNC paths, mapped drives, retries on a share that drops |
| **[Languages](Languages.md)** | The 26 covered languages, and why declaring one is a check |
| **[Models](Models.md)** | Where models come from, how to bring your own, the one-GitHub-source rule |
| **[Performance](Performance.md)** | Measured numbers, what the knobs do, worker scaling |
| **[Accuracy](Accuracy.md)** | Error rates per document type, and where the engine is weak |
| **[Architecture](Architecture.md)** | The pipeline, module by module |
| **[Evaluation](Evaluation.md)** | The generated corpus and how to reproduce the numbers |
| **[Troubleshooting](Troubleshooting.md)** | Error messages and what they mean |
| **[Contributing](Contributing.md)** | Building, testing, the checks CI runs |
| **[Roadmap](Roadmap.md)** | What is missing, in the order it matters |

## Why it exists

Installing OCR in Python is usually harder than the OCR. `ocrust` is built around
three rules:

1. **No native prerequisites.** No Tesseract binary, no PaddlePaddle, no PyTorch,
   no Poppler, no PDFium. The engine is one prebuilt wheel.
2. **No admin rights and no compiler.** Installing is `pip install`: prebuilt
   abi3 wheels for Linux, macOS and Windows, nothing built on your machine.
3. **One source for everything.** Models live in the repository and install from a
   wheel or from `raw.githubusercontent.com`. A proxy that allows GitHub and PyPI
   — and nothing else — is enough. Hugging Face, ModelScope and `bcebos.com` are
   never contacted.

## At a glance

| | |
|---|---|
| Input | PNG, JPEG, WebP, BMP, GIF, PNM/PBM/PGM/PPM, TGA, HDR, QOI, multi-page TIFF (1-bit CCITT included), PDF, `bytes`, numpy arrays, PIL images |
| Output | text, Markdown, JSON, hOCR, ALTO XML, CSV, searchable PDF (Unicode text layer, one file from many inputs), PDF text layer, multi-page TIFF |
| Languages | 26 complete (Latin, Japanese, Chinese); 35 known and checkable, Greek among the ones refused |
| Models | PP-OCRv6, 18 708 characters, 31 MB, Apache-2.0 |
| Speed | ~670 ms per 200 dpi A4 page on four CPU cores |
| Accuracy | median CER 0.006 over a 118-file corpus of aged scans, faxes, drawings and forms |
| License | Apache-2.0 |
