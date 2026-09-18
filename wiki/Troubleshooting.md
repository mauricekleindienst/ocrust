# Troubleshooting

Start here:

```bash
ocrust doctor
```

Anything other than `status: ready` names the problem. The cases below are what
that line can say, and what to do about each.

## `the ONNX Runtime library could not be loaded`

```text
RuntimeError: the ONNX Runtime library could not be loaded: …
hint: install it with `pip install onnxruntime`, or point OCRUST_ORT_DYLIB /
ORT_DYLIB_PATH at libonnxruntime
```

`ocrust` loads `libonnxruntime` at run time instead of linking it at build time —
that is why the wheel needs no compiler. The library has to exist somewhere:

```bash
pip install onnxruntime          # normally all it takes
```

If your platform has no `onnxruntime` wheel, or you must use a vendored build:

```bash
export ORT_DYLIB_PATH=/opt/onnxruntime/lib/libonnxruntime.so
```

ONNX Runtime 1.20 or newer is required — that is the floor the `onnxruntime`
dependency pins, and `ocrust` asks the library for API level 22.
`ocrust doctor` prints both the path it found and the version that actually
loaded — if the `library` line is filled in but `loaded` says `unavailable`, the
file exists and is the wrong architecture or too old.

## `no text detection model found`

```text
RuntimeError: model error: no text detection model found (searched: …).
hint: `pip install ocrust[models]`, or point OCRUST_MODELS_DIR at a directory
holding PP-OCR *_det.onnx / *_rec.onnx files
```

Pick one:

```bash
pip install "ocrust[models]"     # the models as a wheel — nothing downloaded later
ocrust install-models            # fetch from GitHub into the cache
export OCRUST_MODELS_DIR=/opt/models/ppocrv6
```

`ocrust models` prints which files would be used, so you can see what is found
and what is missing.

## `install-models` fails with 404

The manifest points at `raw.githubusercontent.com`. Two reasons it can 404:

1. **The repository is private.** Anonymous raw requests to a private repo return
   404, not 403. Set a token:

   ```bash
   export OCRUST_GITHUB_TOKEN=ghp_…      # or GITHUB_TOKEN
   ocrust install-models
   ```

2. **The ref does not exist.** `OCRUST_MODEL_REF` defaults to `main`; if you
   pinned a tag that was never pushed, every file 404s.

The `ocrust-models` wheel avoids both — it is the recommended path on locked-down
networks.

## `checksum mismatch`

```text
RuntimeError: download failed: checksum mismatch: expected 090f04ab…, got d41d8c…
```

Something rewrote the response — usually a proxy serving an HTML error page or a
login redirect instead of the file. The download is rejected rather than cached,
which is the point. Check the URL with `curl -I`, and remember that a proxy
returning 200 with an HTML body is the normal failure shape here.

## `unsupported input: … The image format could not be determined`

The file is not an image or PDF that `ocrust` can decode, or it is truncated.
`ValueError` is deliberate: broken input is not a configuration problem.

```python
for path in paths:
    try:
        text = ocrust.read(path)
    except (IOError, ValueError) as exc:
        print(f"{path}: {exc}")     # skip and carry on
```

Note that content sniffing beats the extension: a PNG named `.pdf` is read as a
PNG. The reverse — a real PDF named `.png` — also works. TGA is the one format
with no magic bytes, so it is resolved by file name.

## `recognition model … does not cover the requested language(s)`

```text
invalid configuration: recognition model ppocrv6_rec.onnx does not cover the
requested language(s): Russian (ru): cannot write А Б В Г Д Е Ж З И Й …
hint: `ocrust languages` lists what this model covers
```

Working as designed. The shipped model has no Cyrillic, and a model that cannot
spell a language returns text that *looks* right and is wrong. Either drop the
`lang` requirement (accepting partial results knowingly) or supply a recognizer
for that script ([Models](Models.md)). `ocrust languages` lists what is covered.

## Text comes out in the wrong order

Not a bug you can configure away: reading order is geometric. It handles columns,
headings and paragraphs, and it does **not** understand table structure. Receipts,
ruled forms and drawings are the known weak spots — see [Accuracy](Accuracy.md).

Two things help today:

```python
doc = ocrust.scan("form.png")

for line in sorted(doc.lines, key=lambda l: (l.box.y0, l.box.x0)):   # your own order
    print(line.text)
```

and evaluating with word recall rather than CER if order does not matter for your
use case.

## A batch off a network share dies partway through

Reads and writes already retry twice with backoff, which covers the ordinary SMB
and NFS hiccup. A share that drops more often than that needs more:

```bash
ocrust scan '\\fileserver\scans' --io-retries 5 -o '\\fileserver\ocr'
```

```python
ocrust.Ocr(io_retries=5)
```

"No such file" and "permission denied" are never retried — a second attempt
returns the same answer. If the message names a Windows error instead
(`[WinError 53] The network path was not found`), the share itself is
unreachable and no retry count will help. [Network shares](Network-shares.md) has the details.

## Words are run together, or a space appears where none belongs

The recognizer drops word spaces on lossy input, so `ocrust` restores them from
the column ink of each line crop (see [Architecture](Architecture.md)). Two knobs, in the rare
case it gets one wrong:

```python
ocrust.Ocr(rec_space_gap=0)      # never insert a space; live with 88EUR
ocrust.Ocr(rec_space_gap=3.0)    # be stricter about which gaps qualify
```

Words still run together where the paper between them is not actually blank —
touching glyphs on a heavy fax, or a very tight font. Raising `pdf_dpi` helps more
than tuning the threshold, because the blank columns then exist.

## A blank or nearly blank page comes out rotated

Fixed: the deskew estimator ignores pages whose ink fraction is below 0.05% and
prefers 0° when a rotation is not clearly better. If you see this on a page that
does have content, `preprocess=False` disables deskew entirely — it costs nothing
measurable in accuracy ([Performance](Performance.md)).

## Small print is missed

The detector works at `det_limit_side` (960 px). On a large sheet, small labels
vanish at that scale. Pages above 3840 px are tiled automatically; between those
two sizes, raise the working size:

```python
ocrust.Ocr(det_limit_side=1600, pdf_dpi=300)
```

## Characters are clipped, or neighbouring lines merge

`det_unclip_ratio` (1.5) controls how far each detected box is expanded:

```python
ocrust.Ocr(det_unclip_ratio=1.8)    # clipped ascenders/descenders
ocrust.Ocr(det_unclip_ratio=1.3)    # lines bleeding into each other
```

## Lines are missing from the output

`drop_score` (0.5) discards low-confidence lines. Faint thermal print and heavy
fax dithering can fall below it:

```python
ocrust.Ocr(drop_score=0.2)
```

```bash
ocrust scan faint.png --min-confidence 0.2
```

## The PDF text layer has `?` where the text had letters

That was the old behaviour, when the layer only had a WinAnsi font. Lines with
CJK, Cyrillic or Greek now use a Type0 font with a `ToUnicode` map and come out
intact; `report["unmappable_chars"]` says how many characters the layer really
lost, and should read 0.

If you still see `?`, check that the extractor reads `ToUnicode` — some very old
tools ignore it. The recognized text is complete in every other format either
way:

```bash
ocrust scan scan.pdf -f json -o scan.json
```

## Selection in the searchable PDF does not line up

If the offset is a whole page rotation, the source PDF's `/Rotate` is being
applied twice by a non-compliant viewer — check in two viewers before debugging.
Everything else is usually a mixed pipeline: run `ocrust ocr` on the *original*
PDF, not on a version you re-rasterized yourself at another DPI.

## More workers made it slower

Then `page_workers × threads` exceeds your core count. `ocrust` divides the cores
between page workers and inference threads, but an explicit `threads=` overrides
that. Leave `threads` alone, or keep the product at or below the core count
([Performance](Performance.md)).

## Memory grows to ~800 MB

Expected. Most of it is ONNX Runtime's CPU arena, which holds allocations for
reuse. It plateaus. Smaller `rec_batch_size` and `det_limit_side` lower the peak;
one engine instead of two (`keep_page_images=True` when you will also write PDFs)
avoids doubling it.

## Nothing is detected at all, on every file

Check `ocrust models`: a detection model of the wrong kind (a recognizer pointed
at `detection_model=`, or a v5 bundle with mismatched dictionary) loads fine and
finds nothing. The shipped bundle is verified by checksum; a hand-assembled one
is not.

## Still stuck

```bash
ocrust doctor --json
python -c "import ocrust, json; print(json.dumps(ocrust.runtime_info(), indent=2))"
```

That output plus the file that fails is everything needed to reproduce a problem.
See [Contributing](Contributing.md) for where to put it.
