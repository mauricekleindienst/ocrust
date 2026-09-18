# Architecture

```
Source (path | bytes | RGB frame)
        │
        ▼
   ingest ──────────────  image::  PNG/JPEG/WebP/BMP/GIF/PNM/TGA/DDS/HDR/EXR/QOI/ICO
        │                 tiff::   every page of a multi-page TIFF
        │                 hayro::  PDF pages rasterized at a chosen DPI
        │                 EXIF orientation applied; TGA resolved by file name
        ▼
  preprocess ───────────  auto-invert · deskew · rescale
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
        │                 charset checked against the requested languages
        ▼
    layout ─────────────  baseline merging · XY-cut reading order · paragraphs
        │                 · de-hyphenation · heading/list classification
        ▼
   Document ────────────  pages → blocks → lines → words, boxes + confidences
        │
        ├── export ──────  text · Markdown · JSON · hOCR · ALTO · CSV
        ├── export::pdf ─  searchable PDF (image + invisible text layer)
        ├── export::overlay  text layer added to an existing PDF, pages untouched
        └── export::tiff ─  multi-page TIFF
```

## Crates and packages

| Path | Role |
|---|---|
| `crates/ocrust-core` | the engine, usable as a plain Rust library (`#![forbid(unsafe_code)]`) |
| `crates/ocrust-py` | PyO3 bindings: own the engine, release the GIL, return JSON |
| `python/ocrust` | the public Python API, result dataclasses, CLI |
| `models/` | the model bundle and its manifest — the single source for models |
| `scripts/` | corpus generator, evaluator, wheel builder, E2E run |

## Modules

| Module | Responsibility |
|---|---|
| `ingest` | sniff the container, decode images, walk TIFF pages, rasterize PDFs |
| `preprocess` | skew estimation, inversion, rescaling |
| `detect` | DB inference and post-processing into text-line quads, tiling |
| `classify` | 180° line-orientation classification |
| `recognize` | CTC recognition, batching, greedy decode, per-character positions |
| `dict` | the recognizer's class list, from a file or the ONNX metadata |
| `lang` | language table, script grouping, charset-coverage checks |
| `geom` | points, rects, quads, convex hull, min-area rect, perspective crop |
| `layout` | reading order, block grouping, de-hyphenation, word boxes |
| `doc` | the result model |
| `export` | text, Markdown, JSON, hOCR, ALTO, CSV |
| `export::pdf` | searchable PDF writer, and the WinAnsi encoder both PDF paths use |
| `export::overlay` | text layer added to an existing PDF via `lopdf` |
| `export::tiff` | multi-page TIFF writer |
| `models` | model discovery, cache directory, manifests, downloads |
| `runtime` | ONNX Runtime sessions, execution providers, per-model session pool |
| `pipeline` | `Engine`: wires the stages, parallelizes pages |

## Detection, in detail

DB (Differentiable Binarization) returns a probability map, not boxes. Turning it
into text lines is where most of the engineering sits:

1. Resize so both sides are multiples of 32 and the long side is at most
   `det_limit_side` (960).
2. Normalize with the ImageNet mean/std the model was trained with.
3. Threshold the map at `thresh` (0.3) and trace contours.
4. Convex hull, then a **rotating-calipers minimum-area rectangle** — so a line
   at 3° stays a tight quad instead of a fat axis-aligned box.
5. Score each candidate by the mean probability inside it (`box_thresh`, 0.6).
6. **Unclip**: DB shrinks regions during training, so each quad is expanded by
   `area × unclip_ratio / perimeter`.
7. Map back to the original page coordinates.

### Tiling large formats

A0 at 300 dpi is ~9900×7000 px. Scaled to 960 px, an 8 pt label is four pixels
tall and detection simply fails (this cost CER 0.96 before it was fixed). Above
4× `det_limit_side` the page is split into overlapping tiles, each tile detects
independently, and **every box is owned by exactly one tile**: the overlap is
split at its midpoint, so a box is never counted twice and never dropped at a
seam.

## Recognition, in detail

Crops are perspective-warped to the quad, rotated upright when they are taller
than wide, and batched by aspect ratio so padding is minimal. The CTC output is
decoded greedily (blank = class 0), and each emitted character keeps the timestep
it came from — which is where word boxes come from: timesteps map back to x
positions, and spaces split the sequence into words.

That is also why `word_boxes=False` is cheaper: no per-character bookkeeping.

## Layout, in detail

The detector returns boxes, and a box is not a line. A receipt's item and its
right-aligned price are two boxes; so are the cells of a table row.

1. **Baseline merging.** Boxes whose vertical extents overlap by ≥ 55% are
   candidates for one line. Whether a wide horizontal gap belongs to the same line
   is decided by **repeating columns**: a table or price list puts cells at the
   same x positions row after row; a drawing's scattered labels merely share a
   height. The decision is taken per region, so a drawing's title block can be
   tabular while the sheet around it is not.
2. **XY-cut** for reading order: find the widest horizontal gutter, split,
   recurse. A column split additionally requires at least two lines on each side
   and a side wide relative to the gutter — that is what separates a newspaper's
   columns from a table's cell gaps.
3. **Paragraph grouping** by line spacing, then heading and list-item
   classification by relative height and leading punctuation.
4. **De-hyphenation** across line breaks.

## Language checking

A CTC recognizer can only emit characters from its own class list. Asking a
model without `ü` for German does not fail — it returns `Grusse`. So each
language in the table carries the non-ASCII letters of its alphabet
(cross-checked against PaddleOCR's dictionaries) or probe characters for
non-alphabetic scripts, and `Engine::new` refuses when the loaded charset cannot
produce them, naming what is missing. See [[Languages]].

## PDF text layers

Two jobs, two writers:

- **`export::pdf`** builds a *new* document: each page becomes a JPEG plus an
  invisible text layer. No PDF library involved.
- **`export::overlay`** modifies an *existing* document with `lopdf`: page
  objects, images and compression stay byte-identical; one content stream and one
  font are added.

The overlay path has to get three things right — display-space rotation,
inherited `/Resources`, and pages that already contain text. [[PDF workflows]]
covers each.

## Concurrency

- Each model holds a small pool of ONNX sessions. `Session::run` needs `&mut`,
  so a pool avoids serializing page workers behind one lock while still bounding
  memory.
- Pages of a document, and documents of a batch, run through rayon.
- `page_workers` and ONNX Runtime intra-op threads **divide** the cores
  (`intra_threads = cores / page_workers`). Before that, eight workers were 20%
  slower than one. See [[Performance]].
- The Python bindings release the GIL for the whole scan, so a Python thread pool
  parallelizes too.

## Coordinates

All geometry refers to the **preprocessed** page image, whose size is reported as
`Page.width`/`Page.height`. Deskew and rescaling therefore stay consistent with
the boxes, and TIFF output built from the same image lines up exactly. The PDF
overlay path is the exception: it disables deskew and rescaling for the pages it
OCRs, because the text layer must match the *original* page.

## No C, no C++, no CMake

The whole workspace is pure Rust — `cargo tree --edges build` lists no build
scripts that compile native code. ONNX Runtime is loaded **dynamically at run
time** (`ort` with `load-dynamic`, pointed at `libonnxruntime` by
`ORT_DYLIB_PATH`), supplied by the `onnxruntime` PyPI wheel. Nothing is compiled
or vendored at build time, which is what lets `pip install ocrust` work on a
locked-down machine without admin rights.

## Error handling

`ocrust_core::Error` separates input problems (`Unsupported`, `Io`, `Pdf`,
`Image`) from configuration problems (`Config`, `Dict`, `Model`) and runtime
problems (`Runtime`, `RuntimeMissing`, `Download`). The Python layer maps them
onto `IOError`, `ValueError` and `RuntimeError`, so a batch loop can distinguish
"this file is broken" from "the engine is misconfigured". `RuntimeMissing`
carries the `pip install onnxruntime` hint, because that is the one failure a new
user is most likely to hit.
