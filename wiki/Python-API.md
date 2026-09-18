# Python API

Everything is reachable from the top-level module. There is one class, `Ocr`, a
set of result dataclasses, and module-level helpers that use a shared default
engine.

```python
import ocrust

ocrust.read(source)          -> str
ocrust.scan(source)          -> Document
ocrust.scan_many(sources)    -> Iterator[Document]
ocrust.ocr_pdf(source)       -> tuple[bytes, dict[str, int]]
ocrust.searchable_pdf(src)   -> bytes
ocrust.to_tiff(source)       -> tuple[bytes, Document]
ocrust.languages()           -> tuple[dict, ...]
ocrust.known_languages()     -> tuple[dict, ...]
ocrust.install_models()      -> dict[str, str | None]
ocrust.models_cache_dir()    -> str
ocrust.runtime_info()        -> dict
```

The module-level helpers build one engine on first use and reuse it. That is
convenient for a script and wrong for a service: an explicit `Ocr` lets you pick
the device, the language check and the worker count.

## `Ocr`

```python
ocr = ocrust.Ocr(
    models_dir=None,
    device=None,          # "cpu" (default), "auto", "cuda", "cuda:1", "coreml", "directml"
    threads=None,         # threads per inference operator
    page_workers=None,    # pages in parallel; None -> 1
    pdf_dpi=None,         # PDF rasterization DPI, default 200
    preprocess=True,      # auto-invert, deskew, rescale
    deskew=None,          # deskew alone, when preprocess is on
    word_boxes=True,      # per-word geometry (hOCR/ALTO word output needs it)
    drop_score=None,      # minimum mean line confidence, default 0.5
    lang=None,            # "de", "de,fr" or ["de", "fr"] -> checked, not a hint
    keep_page_images=False,
    # model overrides
    detection_model=None, recognition_model=None, orientation_model=None, dictionary=None,
    # tuning
    fix_orientation=True, det_limit_side=None, det_box_threshold=None,
    det_unclip_ratio=None, rec_batch_size=None, rec_image_height=None,
    rec_space_gap=None,   # 0 turns off restoring swallowed word spaces
)
```

Building one loads the models, so keep it. Instances are thread-safe and release
the GIL for the whole scan, so a thread pool in Python parallelizes properly.

| Argument | When to change it |
|---|---|
| `pdf_dpi` | 200 is the default. Raise to 300 for small print; 100 is faster and, on ordinary scans, just as accurate ([[Performance]]). |
| `page_workers` | Multi-page documents and batches. Defaults to 1 because workers and inference threads share the same cores. |
| `preprocess` | `False` buys ~9% and costs nothing measurable in CER; it can hurt multi-column layout. |
| `word_boxes` | `False` when you only want text — it skips the per-character bookkeeping. |
| `drop_score` | Raise it to suppress noise lines, lower it to keep faint print. |
| `det_limit_side` | The detector's working size (960). Large formats are tiled automatically above 3840 px. |
| `det_unclip_ratio` | 1.5. Raise it when characters are clipped, lower it when neighbouring lines merge. |
| `lang` | Always, in production. See [[Languages]]. |
| `rec_space_gap` | Almost never. `0` disables space restoration, which you want only if a swallowed space is preferable to a wrongly inserted one. |

### Methods

```python
ocr.scan(source, *, pages=None, name=None) -> Document
ocr.read(source, **kw) -> str
ocr.scan_many(sources) -> Iterator[Document]

ocr.ocr_pdf(source, *, dpi=None, skip_pages_with_text=True, compress=True) -> (bytes, report)
ocr.plan_pdf(source, *, skip_pages_with_text=True) -> tuple[dict, ...]
ocr.searchable_pdf(source, *, dpi=None, jpeg_quality=80) -> bytes
ocr.to_tiff(source, *, gray=False) -> (bytes, Document)

ocr.models            # the four resolved file paths
ocr.languages         # languages the loaded model covers completely
ocr.charset_size      # characters the recognizer can emit
ocr.partial_languages(min_ratio=0.8)
```

`source` can be a path, `bytes`, a numpy array (`(H, W)`, `(H, W, 3)` or
`(H, W, 4)`, any dtype) or a PIL image. Neither numpy nor Pillow is a
dependency — they are used only if you already have them.

```python
import numpy as np
from PIL import Image

ocr.read(np.asarray(Image.open("page.png")))
ocr.read(Image.open("page.png"))
ocr.read(open("page.png", "rb").read())
ocr.read("scan.pdf", pages=[0, 2, 4])          # zero-based
```

## Results

```python
Document(source, pages, elapsed_ms)
  .text                      # every page, reading order applied
  .lines  .words             # flattened across pages
  .confidence                # mean line confidence, or None
  .render(format) / .markdown() / .json() / .hocr() / .alto() / .csv()
  .to_dict()

Page(index, width, height, rotation, origin, blocks, elapsed_ms)
  .text  .lines  .confidence

Block(kind, box, lines)       # kind: "paragraph", "heading", "list"
  .text

Line(text, box, confidence, angle, words, polygon)
Word(text, box, confidence)
Box(x0, y0, x1, y1)  .width  .height  .as_tuple()
```

All coordinates are pixels in the **preprocessed** page image, whose size is
`Page.width` × `Page.height`. `Line.polygon` keeps the detector's four corners,
so rotated lines are not squashed into their bounding box, and `Line.angle` is
the baseline angle in degrees.

The dataclasses are frozen, so they hash, compare and pickle without surprises.

```python
doc = ocrust.scan("form.pdf")

for page in doc.pages:
    for block in page.blocks:
        if block.kind == "heading":
            print(page.index, block.text)

low = [l for l in doc.lines if l.confidence < 0.8]
print(f"{len(low)} line(s) worth a human look")
```

## Errors

Everything the engine refuses raises `ocrust.OcrustError` (a `RuntimeError`),
with the underlying reason in the message:

```python
try:
    ocr = ocrust.Ocr(lang="ru")
except ocrust.OcrustError as exc:
    print(exc)   # invalid configuration: the recognition model cannot write Russian …
```

Unreadable files raise `IOError` or `ValueError`, so a batch loop can tell "this
file is broken" from "the engine is misconfigured":

```python
for path in paths:
    try:
        print(ocrust.read(path))
    except (IOError, ValueError) as exc:
        print(f"{path}: skipped ({exc})")
```

`scan_many` raises `OcrustError` for a file that failed, after yielding the ones
that succeeded before it.

## Diagnostics

```python
ocrust.runtime_info()
```

```python
{'python': '3.12.3',
 'platform': 'linux',
 'onnxruntime_dylib': '.../onnxruntime/capi/libonnxruntime.so.1.30.0',
 'onnxruntime_version': '1.30.0',
 'onnxruntime_loaded': 'ONNX Runtime (API level 22)',
 'models_dir': '.../ocrust_models/models',
 'models': {'detection': '.../ppocrv6_det.onnx',
            'recognition': '.../ppocrv6_rec.onnx',
            'orientation': '.../ppocr_cls.onnx',
            'dictionary': None},
 'models_cache_dir': '/root/.cache/ocrust/models',
 'ocrust': '0.1.0'}
```

See [[Troubleshooting]] when a field says `unavailable`.
