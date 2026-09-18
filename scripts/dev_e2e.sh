#!/usr/bin/env bash
# End-to-end development run: build, install, test and exercise every surface.
#
#   ./scripts/dev_e2e.sh              # full run, creates a throwaway venv
#   VENV=/tmp/my-venv ./scripts/dev_e2e.sh
#   SKIP_INSTALL=1 ./scripts/dev_e2e.sh   # reuse the venv, skip pip
#
# Everything the run needs is either in this repository (models) or on PyPI
# (onnxruntime, pytest). No other hosts are contacted, no admin rights and no C
# toolchain are required.
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
VENV="${VENV:-/tmp/ocrust-e2e}"
WORK="${WORK:-/tmp/ocrust-e2e-work}"
MODELS="$ROOT/models/ppocrv6"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

step "Toolchain"
cargo --version
rustc --version
python3 --version

step "Rust: format, lint, unit tests"
cargo fmt --all --check
cargo clippy --workspace --all-targets -- -D warnings
cargo test -p ocrust-core --lib

step "Rust: end-to-end tests against the bundled models"
OCRUST_MODELS_DIR="$MODELS" cargo test -p ocrust-core --test end_to_end

step "Build the wheel and the model wheel"
if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --quiet --upgrade pip
fi
if [ "${SKIP_INSTALL:-0}" != "1" ]; then
  "$VENV/bin/pip" install --quiet "maturin>=1.7,<2.0" pytest ruff numpy pillow
fi
"$VENV/bin/maturin" build --release --quiet -o dist
python3 scripts/build_models_wheel.py --models-dir "$MODELS" -o dist-models >/dev/null
WHEEL="$(ls -t dist/ocrust-*.whl | head -1)"
MODEL_WHEEL="$(ls -t dist-models/ocrust_models-*.whl | head -1)"
echo "wheel:       $WHEEL"
echo "model wheel: $MODEL_WHEEL"

step "Install into a clean environment"
rm -rf "$WORK"
mkdir -p "$WORK"
python3 -m venv "$WORK/venv"
"$WORK/venv/bin/pip" install --quiet --upgrade pip
"$WORK/venv/bin/pip" install --quiet "$WHEEL" "$MODEL_WHEEL" pytest numpy pillow
PY="$WORK/venv/bin/python"
OCRUST="$WORK/venv/bin/ocrust"

step "Python: lint and tests"
"$VENV/bin/ruff" check python tests scripts
"$VENV/bin/ruff" format --check python tests scripts
"$PY" -m pytest tests -q

step "Diagnostics"
"$OCRUST" doctor
"$OCRUST" languages

step "Build a test document"
"$PY" - "$WORK" <<'PYCODE'
import sys
from pathlib import Path

sys.path.insert(0, "tests")
from conftest import _pdf_with_text  # noqa: E402

work = Path(sys.argv[1])
(work / "invoice.pdf").write_bytes(
    _pdf_with_text(
        [
            ("RECHNUNG 2026-0042", 34),
            ("Grüße aus München", 28),
            ("Betrag: 1.299,90 EUR", 26),
            ("Français: déjà payé", 24),
        ]
    )
)
(work / "scan.pdf").write_bytes(_pdf_with_text([("Seite eins", 30)], pages=2))
print("wrote invoice.pdf and scan.pdf")
PYCODE

step "CLI: every export format"
for fmt in text markdown json hocr alto csv; do
  "$OCRUST" scan "$WORK/invoice.pdf" -f "$fmt" -o "$WORK/out" -q
done
ls -1 "$WORK/out"
echo "--- text ---"
cat "$WORK/out/invoice.txt"

step "CLI: OCR layer over an existing PDF"
"$OCRUST" ocr "$WORK/scan.pdf" --dry-run
"$OCRUST" ocr "$WORK/scan.pdf" --force -o "$WORK/scan.ocr.pdf"

step "CLI: searchable PDF and multi-page TIFF"
"$OCRUST" pdf "$WORK/invoice.pdf" -o "$WORK/invoice.searchable.pdf"
"$OCRUST" tiff "$WORK/scan.pdf" --gray --sidecar text -o "$WORK/scan.tiff"

step "CLI: every input format round-trips"
"$PY" - "$WORK" <<'PYCODE'
import sys
from pathlib import Path

from PIL import Image

work = Path(sys.argv[1])
page = Image.open(work / "scan.tiff").convert("RGB")
for suffix in ("png", "jpg", "webp", "bmp", "ppm", "tga", "gif"):
    page.save(work / f"page.{suffix}")
print("wrote", ", ".join(p.name for p in sorted(work.glob("page.*"))))
PYCODE
for f in "$WORK"/page.* "$WORK/scan.tiff"; do
  printf '%-12s ' "$(basename "$f")"
  "$OCRUST" scan "$f" -q -f text | head -1
done

step "Python API"
"$PY" - "$WORK" <<'PYCODE'
import sys
from pathlib import Path

import ocrust

work = Path(sys.argv[1])
ocr = ocrust.Ocr(lang="de,fr", page_workers=4)
doc = ocr.scan(work / "invoice.pdf")
print(f"pages={len(doc.pages)} lines={len(doc.lines)} words={len(doc.words)}")
print(f"confidence={doc.confidence:.3f} elapsed={doc.elapsed_ms:.0f} ms")
print(f"languages={len(ocr.languages)} charset={ocr.charset_size}")
print("first line:", doc.lines[0].text, doc.lines[0].box.as_tuple())

pdf, report = ocr.ocr_pdf(work / "scan.pdf", skip_pages_with_text=False)
print("text layer:", report)
tiff, tiff_doc = ocr.to_tiff(work / "invoice.pdf", gray=True)
print(f"tiff bytes={len(tiff)} pages={len(tiff_doc.pages)}")
PYCODE

step "Benchmark"
if "$PY" -c "import rapidocr_onnxruntime" 2>/dev/null; then
  "$PY" scripts/benchmark.py "$WORK/invoice.pdf" --runs 3
else
  "$PY" -m pip install --quiet pypdfium2 >/dev/null 2>&1 || true
  "$PY" scripts/benchmark.py "$WORK/invoice.pdf" --runs 3 --only ocrust || \
    echo "(benchmark needs pypdfium2 for a neutral rasterizer)"
fi

step "Done"
echo "artifacts in $WORK"
