# ocrust

**Document OCR for Python, with a Rust core.** Images, multi-page TIFF and PDF in,
text — or Markdown, JSON, hOCR, ALTO, CSV, searchable PDF — out.

```bash
pip install "ocrust[models]"
```

```python
import ocrust

print(ocrust.read("invoice.pdf"))
```

```text
INVOICE 2026-0042
Total: 199.90 EUR
Thank you for your business
```

## Why another OCR package

Installing OCR in Python is usually the hard part, not the OCR:

| | install | native prerequisites | PDF support | first run |
|---|---|---|---|---|
| **ocrust** | `pip install "ocrust[models]"` | none — the wheel ships the engine, `onnxruntime` ships the runtime | built in (pure-Rust renderer) | offline, models come from the wheel |
| pytesseract | `pip install` **+** `apt install tesseract-ocr` | Tesseract binary, language packs | via extra tools | needs the system binary |
| PaddleOCR | `pip install paddleocr paddlepaddle` | PaddlePaddle (~1 GB with deps) | via extra tools | downloads models |
| EasyOCR | `pip install easyocr` | PyTorch (~2.5 GB with CUDA) | none | downloads models |

Everything else follows from that: one wheel, no `apt`, no CUDA toolkit, no model
download on first use, and the same behaviour in a container, on a laptop and in CI.

> **Note on `[models]`.** The `ocrust-models` wheel is built from this repo with
> `python scripts/build_models_wheel.py --models-dir <dir>`; until it is published
> to PyPI, install it from a local build or point `OCRUST_MODELS_DIR` at a model
> directory (see [Models](#models)).

### Measured

Same page, same models, same ONNX Runtime — only the OCR stack differs
(`scripts/benchmark.py`, median of 5 runs, one page at a time, CPU only,
1700×2200 px rendered from a text PDF at 200 dpi):

| engine | time per page | recognized |
|---|---|---|
| **ocrust** | **322 ms** | 81 chars, all 4 lines |
| rapidocr-onnxruntime | 773 ms | 81 chars, all 4 lines |

Identical output, **2.4× the throughput** — the difference is the pipeline around
the models: no Python in the hot path, crops batched by aspect ratio, and a
session pool instead of one lock. Run it yourself:

```bash
pip install "ocrust[models]" rapidocr-onnxruntime pypdfium2 pillow
python scripts/benchmark.py your-page.png --runs 5
```

Numbers depend on the machine, the models and the page; the script skips engines
you do not have installed.

## What it does

- **Every common input.** PNG, JPEG, WebP, TIFF (including multi-page), BMP, GIF,
  PNM, TGA, DDS, HDR, OpenEXR, QOI, ICO — plus PDF, rasterized by
  [hayro](https://crates.io/crates/hayro), a pure-Rust renderer. EXIF rotation is
  applied automatically.
- **Modern models.** PP-OCR family: DB text detection, 180° line-orientation
  classification and CTC recognition, run through ONNX Runtime. The recognizer's
  character set is read from the ONNX metadata, so a matching dictionary file is
  optional.
- **Pages that read correctly.** Skew is estimated and corrected, dark-mode pages
  are inverted, columns are detected with an XY-cut, paragraphs are grouped and
  words hyphenated across line breaks are joined.
- **Output for real pipelines.** Plain text, Markdown, JSON with every box and
  score, hOCR, ALTO XML, CSV, and searchable PDFs (the scan with an invisible
  text layer).
- **Fast by construction.** Rust, no Python in the hot path, batched recognition
  grouped by aspect ratio, page-level parallelism, and the GIL released during
  every scan.

## Python API

```python
import ocrust

# One-liners use a lazily built default engine.
text = ocrust.read("scan.jpg")
doc = ocrust.scan("contract.pdf")

# Reuse an engine when you have more than one document: models load once.
ocr = ocrust.Ocr(device="auto", page_workers=8, pdf_dpi=240)

doc = ocr.scan("invoice.pdf", pages=[0, 1])
doc.text                      # reading order applied
doc.markdown()                # headings, lists, paragraphs
doc.hocr(); doc.alto(); doc.csv(); doc.json()
doc.confidence                # mean line confidence, 0..1

for line in doc.lines:
    print(line.text, round(line.confidence, 3), line.box.as_tuple())
    for word in line.words:
        print("   ", word.text, word.box.as_tuple())

# Batch, in parallel.
for result in ocr.scan_many(["a.pdf", "b.png", "c.tiff"]):
    print(result.source, len(result.pages))

# numpy arrays and PIL images work too (neither is a dependency).
import numpy as np
doc = ocr.scan(np.asarray(pil_image), name="frame")

# Archive-ready: the original scan, now searchable.
open("scan.ocr.pdf", "wb").write(ocrust.searchable_pdf("scan.jpg"))
```

## Command line

```bash
ocrust scan invoice.pdf                      # text on stdout
ocrust scan page.jpg -f markdown -o page.md
ocrust scan *.tiff -f json -o results/       # batch; one file per input
ocrust scan book.pdf --pages 1,4-8 --dpi 300
ocrust pdf scan.jpg -o scan.ocr.pdf          # searchable PDF
ocrust doctor                                # runtime + model diagnostics
ocrust models                                # which model files are in use
```

## Models

`pip install "ocrust[models]"` brings the PP-OCR bundle along as a wheel, so a
fresh install works offline. To use your own models, point at a directory
holding PP-OCR-style ONNX files:

```bash
export OCRUST_MODELS_DIR=/opt/models/ppocrv5
ocrust models
```

```python
ocr = ocrust.Ocr(models_dir="/opt/models/ppocrv5")
# or name the files explicitly
ocr = ocrust.Ocr(
    detection_model="det.onnx",
    recognition_model="rec.onnx",
    orientation_model="cls.onnx",   # optional
    dictionary="ppocr_keys.txt",    # optional when embedded in the model
)
```

Files are matched by name (`*det*.onnx`, `*rec*.onnx`, `*cls*.onnx`,
`*dict*.txt`/`*keys*.txt`), which fits bundles from PaddleOCR, RapidOCR and
custom exports alike. Resolution order: explicit paths → `models_dir` →
`OCRUST_MODELS_DIR` → per-user cache (`ocrust models` prints it).

## GPU

CPU is the default and needs nothing. For an accelerator, install a matching
ONNX Runtime build and select the device:

```bash
pip install "ocrust[gpu]"     # onnxruntime-gpu
```

```python
ocr = ocrust.Ocr(device="cuda")     # or "auto", "cuda:1", "coreml", "directml"
```

Accelerated builds of the extension are published as separate wheels; `device="auto"`
falls back to the CPU whenever a provider is unavailable, so code stays portable.

## Tuning

| Argument | Default | Effect |
|---|---|---|
| `pdf_dpi` | 200 | PDF rasterization resolution; 300 helps on small print |
| `det_limit_side` | 960 | Longest side fed to detection; raise for dense pages |
| `det_box_threshold` | 0.6 | Lower finds more, faint text; raise to cut noise |
| `det_unclip_ratio` | 1.5 | How far detected boxes are grown before recognition |
| `rec_batch_size` | 8 | Line crops per recognition call |
| `drop_score` | 0.5 | Minimum mean confidence for a line to be kept |
| `preprocess` | `True` | Auto-invert, deskew, rescale |
| `word_boxes` | `True` | Per-word geometry for hOCR/ALTO |
| `page_workers` | one per core | Pages scanned in parallel |

## Rust crate

The engine is usable on its own, without Python:

```toml
[dependencies]
ocrust-core = "0.1"
```

```rust
use ocrust_core::{Engine, EngineConfig, Source};

let engine = Engine::new(EngineConfig::new())?;
let doc = engine.scan(&Source::path("invoice.pdf"))?;
println!("{}", doc.text());
```

See [`docs/architecture.md`](docs/architecture.md) for the pipeline, and
[`docs/research.md`](docs/research.md) for the model landscape this build is
based on.

## Development

```bash
cargo test -p ocrust-core                 # 80+ unit tests, no models needed
OCRUST_MODELS_DIR=/path/to/models \
  cargo test -p ocrust-core --test end_to_end
maturin develop --release                 # build the extension into a venv
pytest                                    # Python API and CLI tests
```

The end-to-end tests generate their own fixtures (a PDF with known text is
written, rendered and recognized), so there are no binary files in the repo.

## License

Apache-2.0.
