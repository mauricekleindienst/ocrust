# Installation

```bash
pip install "ocrust[models]"
```

That is the whole story on Linux, macOS and Windows for every Python from 3.9 up.
Two things arrive with it:

| Package | Why |
|---|---|
| `onnxruntime` | supplies `libonnxruntime`, which the extension loads at run time |
| `ocrust-models` | the PP-OCRv6 model files, so nothing is downloaded on first use |

No `apt`, no Homebrew, no CUDA toolkit, no compiler, no admin rights: the wheels
are prebuilt (abi3, one per platform, every Python from 3.9 up) and nothing is
compiled during `pip install`.

## Without the models extra

```bash
pip install ocrust
ocrust install-models     # fetches them from raw.githubusercontent.com, checksum-verified
```

While the repository is private, anonymous raw requests return 404 — set a token
first (`export OCRUST_GITHUB_TOKEN=…`, or `GITHUB_TOKEN`, which CI already sets).
The token is only ever sent to GitHub hosts. The `[models]` extra needs no token
at all, which is why it is the recommended path.

Or point at a directory you already have:

```bash
export OCRUST_MODELS_DIR=/opt/models/ppocrv6
```

## Offline and air-gapped

Everything can be staged from one machine:

```bash
# on a machine with network access
pip download "ocrust[models]" onnxruntime -d wheels/

# on the target
pip install --no-index --find-links wheels/ "ocrust[models]"
```

Nothing is fetched at run time when the models extra is installed.

## GPU

```bash
pip install "ocrust[gpu]"     # onnxruntime-gpu
```

```python
ocr = ocrust.Ocr(device="cuda")     # or "auto", "cuda:1", "coreml", "directml"
```

`device="auto"` falls back to the CPU whenever a provider is unavailable, so the
same code runs everywhere. Accelerated builds of the extension ship as separate
wheels; see [Performance](Performance.md).

## Checking an install

```bash
ocrust doctor
```

```text
ocrust            0.1.0
python            3.11.15 on linux
onnxruntime       1.30.0
  library         .../onnxruntime/capi/libonnxruntime.so.1.30.0
  loaded          ONNX Runtime (API level 22)
models dir        .../ocrust_models/models
models cache      /root/.cache/ocrust/models
  detection      .../ppocrv6_det.onnx
  recognition    .../ppocrv6_rec.onnx
  orientation    .../ppocr_cls.onnx
  dictionary     -

status: ready
```

Anything other than `status: ready` is explained in [Troubleshooting](Troubleshooting.md).

## From source

Needs Rust 1.85 or newer, and — with one exception below — nothing else:

```bash
git clone https://github.com/mauricekleindienst/ocrust
cd ocrust
pip install "maturin>=1.7,<2.0"
maturin develop --release
python scripts/build_models_wheel.py --models-dir models/ppocrv6 -o dist
pip install --find-links dist ocrust-models
```

## Environment variables

| Variable | Effect |
|---|---|
| `OCRUST_MODELS_DIR` | directory to load models from |
| `OCRUST_HOME` | root of the model cache (default: the OS cache directory) |
| `OCRUST_MODEL_REF` | git ref that `install-models` downloads from |
| `OCRUST_ORT_DYLIB`, `ORT_DYLIB_PATH` | a specific ONNX Runtime build to load |
| `OCRUST_GITHUB_TOKEN`, `GITHUB_TOKEN` | authenticates model downloads from GitHub |
