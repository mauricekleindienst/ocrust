# Contributing

## What you need

| Tool | Why | Version |
|---|---|---|
| Rust | the engine | 1.85 or newer |
| Python | bindings, CLI, tests | 3.9 or newer |
| maturin | builds the wheel | `>=1.7,<2.0` |

**That is nearly the whole list.** No CMake, no CUDA toolkit, no system OCR
library: ONNX Runtime is loaded at run time from the `onnxruntime` wheel, and the
engine is pure Rust.

The one exception is the optional model downloader, whose TLS stack (`ureq` →
`rustls` → `ring`) compiles C and assembly, so a default source build needs a C
compiler — gcc, clang or MSVC, whichever your platform already has for Rust.
Without it:

```bash
cargo build -p ocrust-core --no-default-features --features pdf   # entirely Rust
```

Users never hit this: the wheels are prebuilt. Before adding a dependency, check
what it drags in with `cargo tree -i cc`, and keep the engine itself free of
native build scripts.

## Build and install

```bash
git clone https://github.com/mauricekleindienst/ocrust
cd ocrust

python -m venv .venv && . .venv/bin/activate
pip install "maturin>=1.7,<2.0" onnxruntime pytest numpy pillow

maturin develop --release
export OCRUST_MODELS_DIR="$PWD/models/ppocrv6"

pytest -q
```

`maturin develop` without `--release` builds a debug extension. It is fine for
API work and far too slow to measure anything with — always re-measure in
release.

## The full loop

```bash
./scripts/dev_e2e.sh
```

Does everything CI does, in order: `cargo fmt --check`, `cargo clippy -D
warnings`, Rust unit tests, Rust end-to-end tests against the bundled models,
builds both wheels, installs them into a throwaway venv, runs the Python test
suite, then exercises every CLI surface on real files. `SKIP_INSTALL=1` reuses the
venv; `VENV=/path` puts it somewhere else.

## Individual checks

```bash
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test -p ocrust-core --lib                                   # no models needed
OCRUST_MODELS_DIR=$PWD/models/ppocrv6 cargo test -p ocrust-core --test end_to_end
pytest -q
ruff check python tests scripts examples
ruff format --check python tests scripts examples
```

Do not run `cargo clippy --all-features`: the accelerator features (`cuda`,
`coreml`, `directml`, …) need a matching ONNX Runtime build and do not compile
without one. CI runs default features, and so should you.

## Test layout

| Where | What it covers | Needs models? |
|---|---|---|
| `crates/ocrust-core/src/**` unit tests | geometry, CTC decode, layout, WinAnsi, manifests, language coverage | no |
| `crates/ocrust-core/tests/end_to_end.rs` | the real pipeline against the bundled models | yes |
| `tests/test_*.py` | Python API, CLI, formats, languages, PDF layer, TIFF export, the example app | yes |

Current state: 158 Rust unit tests, 8 Rust end-to-end tests, 77 Python tests.

The unit tests deliberately need neither models nor ONNX Runtime, which is what
keeps them fast enough to run on every save — and why CI can check three
platforms cheaply. Keep it that way: a new test that needs a model belongs in
`end_to_end.rs`.

## What CI runs

| Job | Matrix |
|---|---|
| `cargo test` | ubuntu, macOS, Windows — fmt, clippy, unit tests |
| `ruff` | lint and format check over `python tests scripts examples` |
| `wheel` | linux x86_64/aarch64, macOS arm64/x86_64, Windows x86_64 — builds both wheels, installs them, runs `pytest` |
| `publish` | on a `v*` tag only: PyPI via trusted publishing |

A wheel job that builds but fails `pytest` is the one to read first: it means the
wheel and the pure-Python layer disagree about something.

## Conventions

- `#![forbid(unsafe_code)]` in `ocrust-core`. There is no reason for unsafe in a
  document pipeline, and PyO3 handles the FFI.
- Comments explain **why**, not what. A comment restating the code is noise; a
  comment recording why the threshold is 0.55 is the reason the next person does
  not change it back.
- Public items get doc comments. `cargo doc --no-deps` should read like
  documentation, not a header dump.
- Errors are typed and specific: input problems, configuration problems and
  runtime problems are different variants, because the Python layer maps them onto
  different exception types.
- New knobs get a default that is right for documents. Nobody should have to tune
  anything to read an invoice.

## Changing accuracy

Any change to detection, recognition or layout needs numbers, not adjectives:

```bash
python scripts/make_corpus.py --out /tmp/corpus
python scripts/evaluate_corpus.py /tmp/corpus -o before
# … make the change …
python scripts/evaluate_corpus.py /tmp/corpus -o after
diff <(jq -S . before.json) <(jq -S . after.json)
```

State the before/after per category in the commit message, including the
categories that got *worse*. Every improvement in the CHANGELOG was measured this
way, and the five defects the corpus found were all invisible to unit tests.

Be suspicious of a change that improves one category by a lot and nothing else:
the corpus has 24 categories, and a real fix usually moves several.

## Adding a language

1. Add an entry to `LANGUAGES` in `crates/ocrust-core/src/lang.rs` with the
   non-ASCII letters of the alphabet (or probe characters for a non-alphabetic
   script). Cross-check against a real dictionary, not memory.
2. `cargo test -p ocrust-core --lib lang` — coverage against the shipped charset
   is computed, not asserted by hand.
3. If the shipped model cannot spell it, say so in [Languages](Languages.md) rather than
   pretending: a language that is listed but unreadable is worse than one that is
   absent.

## Adding an export format

`crates/ocrust-core/src/export/` holds one writer per format, and `Format`
enumerates them. The Python side calls `render_document(json, format)`, so a new
format appears in `Document.render`, the CLI's `-f` and the `FORMATS` tuple at
once — add it in Rust and nowhere else.

## Releasing

1. Update `CHANGELOG.md` with the measured numbers.
2. Bump the version in `Cargo.toml`, `crates/*/Cargo.toml` and `pyproject.toml`.
3. Tag `vX.Y.Z` and push. CI builds all five wheels and publishes to PyPI.
4. Model bundles are pinned by `OCRUST_MODEL_REF`; if the bundle changed, tag it
   too, so an old release keeps downloading the models it was tested with.
