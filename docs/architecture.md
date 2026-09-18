# Architecture

```
Source (path | bytes | RGB frame)
        │
        ▼
   ingest ──────────────  image::  PNG/JPEG/WebP/TIFF/BMP/GIF/PNM/…  (EXIF applied)
        │                 tiff::   every page of a multi-page TIFF
        │                 hayro::  PDF pages rasterized at a chosen DPI
        ▼
  preprocess ───────────  auto-invert · deskew · rescale · optional denoise/contrast
        │
        ▼
   detect (DB) ─────────  resize /32 · normalize · infer · threshold → contours
        │                 → min-area rect → score → unclip → map back
        ▼
   crop_quad ───────────  perspective crop per line, rotated upright if vertical
        │
        ▼
   classify (cls) ──────  180° text-line orientation, flips upside-down crops
        │
        ▼
  recognize (CTC) ──────  batch by aspect ratio · greedy decode · char positions
        │
        ▼
    layout ─────────────  XY-cut reading order · paragraph grouping · de-hyphenation
        │                 · heading/list classification · word boxes
        ▼
   Document ────────────  pages → blocks → lines → words, with boxes + confidences
        │
        ▼
    export ─────────────  text · Markdown · JSON · hOCR · ALTO · CSV · searchable PDF
```

## Crate layout

| Path | Role |
|---|---|
| `crates/ocrust-core` | The engine. Usable as a plain Rust library. |
| `crates/ocrust-py` | PyO3 bindings: owns the engine, releases the GIL, returns JSON. |
| `python/ocrust` | The public Python API, result dataclasses and the CLI. |

## Module map (`ocrust-core`)

| Module | Responsibility |
|---|---|
| `ingest` | Sniff the container, decode images, walk TIFF pages, rasterize PDFs. |
| `preprocess` | Skew estimation (sheared projection profile), inversion, rescaling. |
| `detect` | DB inference and post-processing into text-line quads. |
| `classify` | 180° line-orientation classification. |
| `recognize` | CTC recognition, batching, greedy decoding, per-character positions. |
| `geom` | Points, rects, quads, convex hull, minimum-area rect, perspective crop. |
| `layout` | Reading order, block grouping, de-hyphenation, word boxes. |
| `doc` | The result model (`Document` → `Page` → `Block` → `Line` → `Word`). |
| `export` | Text, Markdown, JSON, hOCR, ALTO, CSV and searchable PDF writers. |
| `models` | Model discovery, the cache directory, bundle manifests and downloads. |
| `runtime` | ONNX Runtime sessions, execution providers, the per-model session pool. |
| `pipeline` | `Engine`: wires the stages together and parallelizes pages. |

## Concurrency

- Each model holds a small pool of ONNX sessions (`SessionOptions::replicas`).
  `Session::run` needs `&mut`, so a pool avoids serializing page workers behind
  one lock while still bounding memory.
- Pages of one document, and documents of one batch, are processed with rayon.
- The Python bindings release the GIL for the whole scan, so `ocrust` also
  parallelizes from Python threads.

## Coordinates

All geometry refers to the **preprocessed** page image, whose size is reported as
`Page.width`/`Page.height`. Deskew and rescaling therefore stay consistent with
the boxes, and a searchable PDF built from `Page.image` lines up exactly.

## Error handling

`ocrust_core::Error` separates input problems (`Unsupported`, `Io`) from
configuration problems (`Config`, `Dict`, `Model`) and runtime problems
(`Runtime`, `RuntimeMissing`). The Python layer maps them onto `IOError`,
`ValueError` and `RuntimeError`, and `Error::RuntimeMissing` carries the
`pip install onnxruntime` hint because that is the one failure a new user is
most likely to hit.
