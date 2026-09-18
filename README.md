# ocrust

**Document OCR for Python, with a Rust core.** Images, multi-page TIFF and PDF in
— text, Markdown, JSON, hOCR, ALTO, CSV, multi-page TIFF or a searchable PDF out.
27 languages. One `pip install`, no system packages.

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

## Why this exists

Installing OCR in Python is usually harder than the OCR itself. `ocrust` is built
around three rules:

1. **No native prerequisites.** No Tesseract binary, no PaddlePaddle, no PyTorch,
   no Poppler, no PDFium. The whole engine is one prebuilt wheel.
2. **No admin rights and no compiler.** Everything is a wheel; the Rust side has
   no C, C++ or CMake dependency at all.
3. **One source for everything.** The models live in this repository and are
   installed from a wheel or fetched from `raw.githubusercontent.com`. A
   corporate proxy that allows GitHub and PyPI — and nothing else — is enough.
   Hugging Face, ModelScope and `bcebos.com` are never contacted.

| | install | native prerequisites | PDF | first run |
|---|---|---|---|---|
| **ocrust** | `pip install "ocrust[models]"` | none | built in | offline, models ship with it |
| pytesseract | `pip install` **+** `apt install tesseract-ocr` | Tesseract binary, language packs | via extra tools | needs the system binary |
| PaddleOCR | `pip install paddleocr paddlepaddle` | PaddlePaddle (~1 GB with deps) | via extra tools | downloads models |
| EasyOCR | `pip install easyocr` | PyTorch (~2.5 GB with CUDA) | none | downloads models |

### Measured

Same page, same models, same ONNX Runtime — only the OCR stack differs
(`scripts/benchmark.py`, median of 5 runs, one page at a time, CPU only,
1700×2200 px rendered from a text PDF at 200 dpi):

| engine | time per page | recognized |
|---|---|---|
| **ocrust** | **322 ms** | 81 chars, all 4 lines |
| rapidocr-onnxruntime | 773 ms | 81 chars, all 4 lines |

Identical output, **2.4× the throughput**. The difference is the pipeline around
the models: no Python in the hot path, crops batched by aspect ratio, a session
pool instead of one lock, and page-level parallelism with the GIL released.

Numbers depend on the machine and the page; the script skips engines you do not
have installed.

```bash
pip install "ocrust[models]" rapidocr-onnxruntime pypdfium2 pillow
python scripts/benchmark.py your-page.png --runs 5
```

## Languages

The bundled PP-OCRv6 recognizer has 18 708 classes and covers **27 languages**
completely:

| script | languages |
|---|---|
| Latin | English, German, French, Spanish, Italian, Portuguese, Dutch, Swedish, Danish, Norwegian, Finnish, Icelandic, Polish, Czech, Slovak, Hungarian, Romanian, Turkish, Croatian, Slovenian, Estonian, Latvian, Lithuanian |
| Greek | Greek |
| Han / Kana | Chinese (Simplified and Traditional), Japanese |

```bash
ocrust languages          # what the installed model covers
ocrust languages --all    # every language ocrust knows about
```

Declaring the language is not a hint — it is a **check**:

```python
ocr = ocrust.Ocr(lang="de,fr")     # fine with the bundled model
ocr = ocrust.Ocr(lang="ru")        # OcrustError: cannot write а б в г д е ж …
```

That matters because a model without `ö` and `ß` does not fail on German text —
it quietly returns `Grusse` for `Grüße`. `ocrust` refuses instead, naming the
characters the model cannot produce. Cyrillic, Arabic and Devanagari need a
script-specific model; see [`models/README.md`](models/README.md).

## Adding an OCR layer to existing PDFs

The archival workflow: the PDF keeps its pages, its images and its compression,
and gains an invisible text layer so it becomes searchable and selectable.

```bash
ocrust ocr scan.pdf                      # -> scan.ocr.pdf
ocrust ocr scan.pdf --dry-run            # what would happen, page by page
ocrust ocr archive.pdf --force --dpi 300 # also re-OCR pages that have text
```

```python
pdf, report = ocrust.ocr_pdf("scan.pdf")
print(report)
# {'pages': 12, 'pages_with_layer': 9, 'pages_skipped': 3, 'lines': 214,
#  'unmappable_chars': 0}
```

- Pages that **already contain text** are skipped, so running it over a mixed
  archive is safe; `--force` overrides that.
- Page rotation (`/Rotate 90/180/270`) is handled: the text layer is transformed
  back into page space so selection lines up.
- Inherited page resources are preserved rather than shadowed, which is where
  naive implementations corrupt documents.
- Nothing is re-encoded. Only a content stream and a font object are added.

The text layer uses a base-14 WinAnsi font, which covers Western European text.
Characters outside it (CJK, Cyrillic) are counted in `unmappable_chars` and
written as `?` *in the invisible layer only* — the visible page never changes.

Need a searchable PDF from images instead? That builds a new document:

```bash
ocrust pdf photo.jpg -o photo.pdf
```

## Every input format

| | formats |
|---|---|
| Images | PNG, JPEG, WebP, BMP, GIF, PNM/PBM/PGM/PPM, TGA, DDS, HDR, OpenEXR, QOI, ICO |
| Multi-page | TIFF (every page), PDF (every page) |
| In memory | `bytes`, `numpy` arrays, PIL images |

EXIF orientation is applied, and formats without magic bytes (TGA) are resolved
from the file name. PDFs are rasterized by
[hayro](https://crates.io/crates/hayro), a pure-Rust renderer.

## Python API

```python
import ocrust

# One-liners use a lazily built default engine.
text = ocrust.read("scan.jpg")
doc = ocrust.scan("contract.pdf")

# Reuse an engine for more than one document: the models load once.
ocr = ocrust.Ocr(lang="de", device="auto", page_workers=8, pdf_dpi=240)

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

# numpy arrays and PIL images (neither is a dependency).
doc = ocr.scan(numpy_array, name="frame")

# Archive outputs.
pdf, report = ocr.ocr_pdf("scan.pdf")          # text layer over the original
open("out.pdf", "wb").write(ocrust.searchable_pdf("photo.jpg"))
tiff, doc = ocr.to_tiff("scan.pdf", gray=True)  # deskewed multi-page TIFF
```

## Command line

```bash
ocrust scan invoice.pdf                      # text on stdout
ocrust scan page.jpg -f markdown -o page.md
ocrust scan *.tiff -f json -o results/       # batch, one file per input
ocrust scan book.pdf --pages 1,4-8 --dpi 300 --lang de
ocrust ocr scan.pdf -o scan.ocr.pdf          # add a text layer
ocrust pdf photo.jpg -o photo.pdf            # searchable PDF from an image
ocrust tiff scan.pdf --gray --sidecar text   # archive TIFF plus text
ocrust languages                             # model coverage
ocrust models                                # which files are in use
ocrust install-models                        # fetch them from GitHub
ocrust doctor                                # what is installed, what is missing
```

## Models

`pip install "ocrust[models]"` installs them as a wheel — nothing is downloaded
at runtime. Alternatively:

```bash
ocrust install-models      # from raw.githubusercontent.com, checksum-verified
export OCRUST_MODELS_DIR=/opt/models/ppocrv6   # or bring your own
```

```python
ocr = ocrust.Ocr(models_dir="/opt/models/ppocrv6")
ocr = ocrust.Ocr(
    detection_model="det.onnx",
    recognition_model="rec.onnx",
    orientation_model="cls.onnx",   # optional
    dictionary="ppocr_keys.txt",    # optional when embedded in the model
)
```

Files are matched by name (`*det*.onnx`, `*rec*.onnx`, `*cls*.onnx`,
`*dict*.txt`/`*keys*.txt`), so bundles from PaddleOCR, RapidOCR and custom
exports all work. Resolution order: explicit paths → `models_dir` →
`OCRUST_MODELS_DIR` → the per-user cache. See
[`models/README.md`](models/README.md) for provenance and licensing.

Environment knobs: `OCRUST_MODELS_DIR`, `OCRUST_HOME` (cache root),
`OCRUST_MODEL_REF` (git ref for downloads), `OCRUST_ORT_DYLIB` /
`ORT_DYLIB_PATH` (a specific ONNX Runtime build).

## GPU

CPU is the default and needs nothing:

```bash
pip install "ocrust[gpu]"     # onnxruntime-gpu
```

```python
ocr = ocrust.Ocr(device="cuda")     # or "auto", "cuda:1", "coreml", "directml"
```

Accelerated builds of the extension ship as separate wheels; `device="auto"`
falls back to the CPU whenever a provider is unavailable, so code stays portable.

## Tuning

| Argument | Default | Effect |
|---|---|---|
| `lang` | none | Languages the model must be able to spell |
| `pdf_dpi` | 200 | PDF rasterization resolution; 300 helps on small print |
| `det_limit_side` | 960 | Longest side fed to detection; raise for dense pages |
| `det_box_threshold` | 0.6 | Lower finds fainter text; raise to cut noise |
| `det_unclip_ratio` | 1.5 | How far detected boxes grow before recognition |
| `rec_batch_size` | 8 | Line crops per recognition call |
| `drop_score` | 0.5 | Minimum mean confidence for a line to be kept |
| `preprocess` | `True` | Auto-invert, deskew, rescale |
| `word_boxes` | `True` | Per-word geometry for hOCR/ALTO |
| `page_workers` | 1 | Pages scanned in parallel; see below |

### Throughput versus latency

ONNX Runtime already spreads one inference across every core, so page workers
divide the cores rather than adding any. Measured on four cores with a 12-page
scan:

| `page_workers` | 12-page document | single page |
|---:|---:|---:|
| 1 (default) | 8.3 s | fastest |
| 4 | **6.0 s** | slower, the cores are split |

So: leave it at 1 for page-at-a-time work, and raise it for batches and long
PDFs (`ocrust scan --workers 4`). Getting this wrong is easy — before the cores
were shared, eight workers were 20% *slower* than one.

## Rust crate

The engine works without Python:

```toml
[dependencies]
ocrust-core = "0.1"
```

```rust
use ocrust_core::{Engine, EngineConfig, Source};

let engine = Engine::new(EngineConfig::new().with_languages(["de", "fr"])?)?;
let doc = engine.scan(&Source::path("rechnung.pdf"))?;
println!("{}", doc.text());
```

[`docs/architecture.md`](docs/architecture.md) walks the pipeline;
[`docs/research.md`](docs/research.md) records the model landscape this build is
based on.

## Development

```bash
./scripts/dev_e2e.sh          # the whole cycle: build, install, test, exercise
```

Individually:

```bash
cargo test -p ocrust-core --lib                       # 142 unit tests, no models needed
OCRUST_MODELS_DIR=models/ppocrv6 \
  cargo test -p ocrust-core --test end_to_end         # real models
maturin build --release -o dist                       # the wheel
python scripts/build_models_wheel.py \
  --models-dir models/ppocrv6 -o dist-models          # the model wheel
pytest                                                # 58 API, format and CLI tests
ruff check python tests scripts
```

Test fixtures are generated, not committed: a PDF with known text is written,
rendered and recognized, so the suite has no binary inputs and the PDF path is
covered on every run.

For accuracy work there is a generated corpus with ground truth — aged scans,
faxes, technical drawings, rotated pages, broken files — and an evaluation that
reports character error rates, word recall and throughput per category:

```bash
python scripts/make_corpus.py --out /tmp/corpus
python scripts/evaluate_corpus.py /tmp/corpus -o report
```

See [`docs/evaluation.md`](docs/evaluation.md) for what the numbers mean and
[`docs/evaluation-report.md`](docs/evaluation-report.md) for the full run.

## License

Apache-2.0. The bundled models are Apache-2.0 releases of the PaddleOCR project;
see [`models/README.md`](models/README.md).
