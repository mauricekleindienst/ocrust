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
        │                 swallowed word spaces restored from the column ink
        │                 charset checked against the requested languages
        ▼
    layout ─────────────  XY-cut reading order · paragraph grouping · de-hyphenation
        │                 · heading/list classification · word boxes
        ▼
   Document ────────────  pages → blocks → lines → words, with boxes + confidences
        │
        ├── export ──────  text · Markdown · JSON · hOCR · ALTO · CSV
        ├── export::pdf ─  searchable PDF (image + invisible text layer)
        ├── export::overlay  text layer added to an existing PDF, pages untouched
        └── export::tiff ─  multi-page TIFF
```

## Crate layout

| Path | Role |
|---|---|
| `crates/ocrust-core` | The engine. Usable as a plain Rust library. |
| `crates/ocrust-py` | PyO3 bindings: owns the engine, releases the GIL, returns JSON. |
| `python/ocrust` | The public Python API, result dataclasses and the CLI. |
| `models/` | The model bundle and its manifest — the single source for models. |

## Module map (`ocrust-core`)

| Module | Responsibility |
|---|---|
| `ingest` | Sniff the container, decode images, walk TIFF pages, rasterize PDFs. |
| `preprocess` | Skew estimation (sheared projection profile), inversion, rescaling. |
| `detect` | DB inference and post-processing into text-line quads. |
| `classify` | 180° line-orientation classification. |
| `recognize` | CTC recognition, batching, greedy decoding, per-character positions, space restoration. |
| `dict` | The recognizer's class list, from a file or the ONNX metadata. |
| `lang` | Language table, script grouping and charset-coverage checks. |
| `geom` | Points, rects, quads, convex hull, minimum-area rect, perspective crop. |
| `layout` | Reading order, block grouping, de-hyphenation, word boxes. |
| `doc` | The result model (`Document` → `Page` → `Block` → `Line` → `Word`). |
| `export` | Text, Markdown, JSON, hOCR, ALTO, CSV writers. |
| `export::pdf` | Searchable PDF writer, plus the WinAnsi encoder both PDF paths use. |
| `export::overlay` | Text layer added to an existing PDF via `lopdf`. |
| `export::tiff` | Multi-page TIFF writer. |
| `models` | Model discovery, the cache directory, bundle manifests and downloads. |
| `runtime` | ONNX Runtime sessions, execution providers, the per-model session pool. |
| `pipeline` | `Engine`: wires the stages together and parallelizes pages. |

## Restoring swallowed spaces

A CTC recognizer emits a space only when it is confident about the space class,
and on JPEG-compressed scans or faxes it drops them: `88 EUR` comes back as
`88EUR`, which costs a word in every downstream metric and breaks search.

The character positions alone cannot fix that, because the timeline leaves a wide
gap after a capital `M` and between a doubled `mm` as well. The pixels can. Each
crop that goes into the recognizer also yields a **column ink profile** — one pass
over the image that is being copied into the input tensor anyway — and a gap is
turned into a space only when three things hold:

1. it is wide on the CTC timeline (at least `space_gap_factor` median glyph
   widths, default 2),
2. the paper inside it is blank for at least `space_ink_fraction` of the crop
   height (default 0.3), and
3. typography allows it — nothing is spaced off a following full stop or comma.

Measured on 200 dpi invoices, a swallowed space leaves 0.38 to 0.40 of the crop
height in blank columns, the gap after an `M` leaves 0.04 to 0.06, and the widest
innocent case (the paper around a narrow `1` in `1187`) reaches 0.27. Setting
`space_gap_factor` to 0 turns the pass off.

Glyph widths matter for this, so the decoder also keeps the *run* of timesteps a
class occupies instead of only its first step: `m` is wider than `i`, which makes
both the gaps and the per-word boxes mean something.

## Language checking

A CTC recognizer can only emit characters from its own class list. Asking a
Chinese-only model for German therefore does not fail — it returns `Grusse` for
`Grüße`. `lang` turns that into an up-front check: each language carries the
non-ASCII letters of its alphabet (cross-checked against PaddleOCR's own
dictionaries), and `Engine::new` refuses when the loaded charset cannot produce
them, naming the missing characters. Punctuation is reported separately, because
a missing typographic quote does not corrupt a word.

## PDF text layers

Two different jobs, two writers:

- **`export::pdf`** builds a *new* document: each page becomes a JPEG plus an
  invisible text layer. Use it for images.
- **`export::overlay`** modifies an *existing* document with `lopdf`: the page
  objects, images and compression stay exactly as they were, and only a content
  stream and a font object are added. Use it for scanned PDFs.

The overlay path has to get three things right:

1. **Rotation.** Renderers apply `/Rotate`, so OCR coordinates are in display
   space. A `cm` matrix maps them back to page space; the four quarter turns are
   unit-tested by mapping display corners onto page corners.
2. **Inherited resources.** `/Resources` may live on an ancestor node. Setting a
   fresh dictionary on the page would shadow it and break existing content, so
   the inherited dictionary is copied and extended.
3. **Existing text.** Pages whose content streams already show text (`Tj`, `TJ`,
   `'`, `"`) are skipped unless forced, which makes the tool safe to run across a
   mixed archive.

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
the boxes, and PDF/TIFF output built from `Page.image` lines up exactly.

## Dependencies

The whole workspace is pure Rust: no C, C++ or CMake dependency, which is what
lets the wheel install without a toolchain. ONNX Runtime is loaded dynamically at
run time (`ORT_DYLIB_PATH`), supplied by the `onnxruntime` PyPI wheel, so nothing
is compiled or vendored at build time either.

## Error handling

`ocrust_core::Error` separates input problems (`Unsupported`, `Io`, `Pdf`) from
configuration problems (`Config`, `Dict`, `Model`) and runtime problems
(`Runtime`, `RuntimeMissing`). The Python layer maps them onto `IOError`,
`ValueError` and `RuntimeError`, and `Error::RuntimeMissing` carries the
`pip install onnxruntime` hint because that is the one failure a new user is most
likely to hit.
