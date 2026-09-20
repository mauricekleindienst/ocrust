# Performance

All numbers below were measured on the generated corpus — 122 files, 207 pages,
416 MB — on **four CPU cores**, with the shipped defaults. Reproduce them with
`scripts/evaluate_corpus.py` ([Evaluation](Evaluation.md)).

## The headline

| | |
|---|---|
| Throughput | **median 673 ms per page** (mean 805, dominated by one A0 sheet) |
| Whole corpus | 163.3 s for 203 pages, 416 MB of input |
| Typical A4 at 200 dpi | 0.6–1.0 s, one page worker |
| Cold start | ~130 MB and a moment to load the models — once per `Ocr` |
| Memory while scanning | ~700–800 MB resident on A4 pages at 200 dpi |

Two things follow from that:

1. **Reuse the engine.** `ocrust.read(path)` in a loop is fine (a shared default
   engine is built once), but a service should own an explicit `Ocr`.
2. **Page count, not file size, sets the cost.** A 40 MB single-page TIFF is
   cheaper than a 2 MB 30-page PDF.

## What a page costs

Measured over clean, newspaper, form, receipt and dark-mode pages, page time fits

```
time ≈ 320 ms + 30 ms × (number of text lines)
```

on four cores at 200 dpi. The fixed part is decoding, preprocessing and one
detection pass over the page; the per-line part is recognition, which runs once
per detected line. That is why an A4 page of prose costs about 680 ms while a
narrow receipt costs 339 ms, and why *line count*, not file size, predicts the
bill. Treat the fit as a rule of thumb — run-to-run noise on a shared machine is
easily ±30%.

## Workers

```python
ocr = ocrust.Ocr(page_workers=4)
```

```bash
ocrust scan big.pdf --workers 4
ocrust scan *.pdf                 # several inputs default to 4
```

On a 12-page scan, four cores:

| workers | seconds | pages/s | speed-up |
|---:|---:|---:|---:|
| 1 | 7.89 | 1.52 | 1.00× |
| 2 | 7.23 | 1.66 | 1.09× |
| 4 | 5.95 | 2.02 | **1.33×** |
| 8 | 6.58 | 1.82 | 1.20× |

Scaling is real but sublinear, and that is not a bug: ONNX Runtime already
parallelizes inside each inference. `ocrust` divides the cores between page
workers and intra-op threads (`intra_threads = cores / page_workers`), so adding
workers redistributes the same cores instead of oversubscribing them.

Before that split existed, **eight workers were 20% slower than one** and the
batch API ran at 0.73× — the classic thread-fight. If you tune this yourself,
keep `page_workers × threads ≈ cores`.

The default is one worker, because a one-page scan is the common case and a
single worker with all threads is fastest there. The CLI raises it to 4 when you
pass several files.

## Batching

```python
for doc in ocr.scan_many(paths):    # 1.01× versus one at a time
    ...
```

`scan_many` hands the whole list to Rust and scans documents with rayon. Over 20
files it is 13.6 s versus 13.5 s — effectively identical, because a single
document already saturates the cores. Use it for the ergonomics (one call, GIL
released once, results streamed), not for a speed-up.

## The knobs that actually move the clock

| Change | Effect on speed | Effect on accuracy |
|---|---|---|
| `preprocess=False` | +8% | **10× the CER** on skewed input (0.004 → 0.040) |
| `pdf_dpi=100` instead of 200 | +16% on PDFs | none on ordinary scans |
| `word_boxes=False` | small | no text change; hOCR/ALTO lose word geometry |
| `det_limit_side=736` (from 960) | detection scales with the working area | worse on small print |
| `rec_batch_size=16` (from 8) | small, more memory | none |
| `page_workers=4` | +30% on multi-page | none |

`preprocess=False` deserves a warning rather than the emphasis it used to get.
Deskewing was once a wash: every line is rectified individually before
recognition, so the characters came out the same either way. Since lines are
assembled from baselines, it also decides whether a *line* is assembled
correctly — and on a skewed page the baselines are only horizontal after
deskewing. Measured over the skewed, aged and inverted pages: CER 0.004 with
preprocessing, **0.040 without**, to save 8% of the time. Turn it off only for
input you know is upright.

## Resolution

| dpi | mean CER | word recall | seconds |
|---:|---:|---:|---:|
| 100 | 0.003 | 0.969 | 4.2 |
| 150 | 0.004 | 0.964 | 4.2 |
| 200 (default) | 0.007 | 0.910 | 4.4 |
| 300 | 0.002 | 0.980 | 5.2 |

Over six PDFs, differences are small and non-monotonic — the detector rescales
the page regardless. 200 dpi is the default because it is where scanner output
lives; raise it for genuinely small print, lower it when throughput rules.

## Large formats

An A0 drawing at 300 dpi (7522×5318 px) takes **30 s**. The detector works at
960 px, which turns an 8 pt label into four pixels, so pages above 4×
`det_limit_side` (3840 px) are **tiled**: overlapping tiles, each box owned by
exactly one tile, split at the overlap midpoint. That is what took the A0 sheet
from CER 0.96 to 0.042 — and it costs time proportional to the number of tiles.

If you have many such sheets, tile them yourself across machines; the work is
embarrassingly parallel.

## GPU

```bash
pip install "ocrust[gpu]"
```

```python
ocr = ocrust.Ocr(device="cuda")     # or "auto", "cuda:1", "coreml", "directml"
```

A provider needs (a) a matching ONNX Runtime build and (b) an `ocrust` wheel
built with that feature. `device="auto"` falls back to the CPU when either is
missing, so the same code still runs. On documents, the CPU path is rarely the
bottleneck people expect it to be — measure before you buy hardware.

## Memory

Measured with `/proc/<pid>/status` `VmHWM` over a synthetic A4 invoice at
200 dpi, one page worker unless stated:

| job | peak | before |
|---|---:|---:|
| one page | 233 MB | 413 MB |
| 40 pages | 249 MB | 999 MB |
| 120 pages | 289 MB | 1811 MB |
| 40 pages, 4 workers | 935 MB | — |
| 40 pages → searchable PDF | 260 MB | — |
| 40 pages → archive TIFF | 261 MB | — |

A long document does not cost more than a short one. Two things make that true.

**Pages are read one at a time.** A PDF is parsed once and its pages are
rasterized on demand, sharing one render cache, so a 120-page scan holds one page
of pixels rather than 120; a TIFF's directories are walked forward. Before that,
the same job grew by about 10 MB per page — a 500-page document needed more
memory than most laptops have. The searchable-PDF path compresses each page as it
is scanned and keeps the JPEG, roughly a fiftieth of the pixels; the TIFF path
encodes each page into the archive and lets it go.

**ONNX Runtime is told not to cache allocations.** Its arena and its
tensor-reuse plan are both speed optimizations that assume the shapes repeat.
Pages do not — every scan is a different size — so the arena accumulates blocks
it never reuses. Turning both off is the default here:

| 40 pages | peak | time |
|---|---:|---:|
| `memory="frugal"` (default) | 249 MB | 26.5 s |
| `memory="fast"` | 584 MB | 23.4 s |
| `memory="frugal"`, 4 workers | 935 MB | 17.0 s |
| `memory="fast"`, 4 workers | 2010 MB | 17.6 s |

So frugal costs about 10% of the clock at one worker and nothing at four (it came
out marginally faster there), for less than half the memory. Pass
`memory="fast"` to `Ocr(...)` or `--memory fast` on the command line when the time
matters more. Recognized text is identical either way.

Workers are the one thing that still multiplies memory: each needs its own
inference session, because `Session::run` wants `&mut self` and the sessions
would otherwise serialize on one lock. Four workers is roughly four times one.
If a container limit is tight, lower `--workers` before anything else; smaller
`rec_batch_size` and `det_limit_side` help after that.

Note also that `ocr_pdf` / `searchable_pdf` need page images, so they build a
sibling engine unless the first one was created with `keep_page_images=True`;
creating it that way keeps the memory to one engine.

## A realistic service shape

```python
import ocrust
from concurrent.futures import ThreadPoolExecutor

ocr = ocrust.Ocr(lang="de,en", page_workers=1)
pool = ThreadPoolExecutor(max_workers=4)      # the GIL is released while scanning

def handle(path: str) -> str:
    return ocr.read(path)

for text in pool.map(handle, paths):
    ...
```

One engine, one worker per page, parallelism in the thread pool: the cores stay
busy and nothing oversubscribes.
