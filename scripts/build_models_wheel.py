#!/usr/bin/env python3
"""Package PP-OCR ONNX models as the ``ocrust-models`` wheel.

``pip install ocrust`` should work offline, which means the models travel as a
wheel instead of being downloaded on first use. This script turns a directory of
model files into that wheel.

    python scripts/build_models_wheel.py --models-dir /path/to/ppocr -o dist/

The wheel is platform independent and contains:

    ocrust_models/__init__.py
    ocrust_models/models/*.onnx
    ocrust_models/models/*.txt     (character dictionary, if present)

`ocrust` finds it automatically: :func:`ocrust._runtime.default_models_dir`
imports ``ocrust_models`` and uses its ``models/`` directory.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

WHEEL_NAME = "ocrust_models"
DIST_NAME = "ocrust-models"

INIT_PY = '''"""PP-OCR model files for :mod:`ocrust`."""

from pathlib import Path

__version__ = "{version}"

#: Directory holding the ONNX models and the character dictionary.
MODELS_DIR = Path(__file__).parent / "models"


def path(name: str) -> Path:
    """Absolute path of one bundled model file."""
    return MODELS_DIR / name
'''

METADATA = """Metadata-Version: 2.1
Name: {dist}
Version: {version}
Summary: PP-OCR model files for ocrust
License: Apache-2.0
Requires-Python: >=3.9
Description-Content-Type: text/markdown

Model files used by [ocrust](https://pypi.org/project/ocrust/).

Installed automatically by `pip install "ocrust[models]"`.
"""

WHEEL = """Wheel-Version: 1.0
Generator: ocrust build_models_wheel
Root-Is-Purelib: true
Tag: py3-none-any
"""


def _wanted(path: Path) -> bool:
    """True for files that belong in the bundle."""
    if path.suffix.lower() == ".onnx":
        return True
    name = path.name.lower()
    return path.suffix.lower() == ".txt" and any(key in name for key in ("dict", "keys", "charset"))


def _record_line(arcname: str, data: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{arcname},sha256={digest},{len(data)}"


def build(models_dir: Path, out_dir: Path, version: str) -> Path:
    files = sorted(p for p in models_dir.iterdir() if p.is_file() and _wanted(p))
    if not files:
        raise SystemExit(f"no model files found in {models_dir}")
    if not any("det" in p.name.lower() for p in files):
        raise SystemExit("no detection model (*det*.onnx) in the bundle")
    if not any("rec" in p.name.lower() for p in files):
        raise SystemExit("no recognition model (*rec*.onnx) in the bundle")

    out_dir.mkdir(parents=True, exist_ok=True)
    wheel_path = out_dir / f"{WHEEL_NAME}-{version}-py3-none-any.whl"
    dist_info = f"{WHEEL_NAME}-{version}.dist-info"
    records: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:

            def write(arcname: str, data: bytes) -> None:
                zf.writestr(arcname, data)
                records.append(_record_line(arcname, data))

            write(f"{WHEEL_NAME}/__init__.py", INIT_PY.format(version=version).encode())
            write(f"{WHEEL_NAME}/py.typed", b"")
            total = 0
            for src in files:
                data = src.read_bytes()
                total += len(data)
                write(f"{WHEEL_NAME}/models/{src.name}", data)
                print(f"  + {src.name} ({len(data) / 1e6:.1f} MB)")

            write(
                f"{dist_info}/METADATA",
                METADATA.format(dist=DIST_NAME, version=version).encode(),
            )
            write(f"{dist_info}/WHEEL", WHEEL.encode())
            write(f"{dist_info}/top_level.txt", f"{WHEEL_NAME}\n".encode())
            record = "\n".join(records + [f"{dist_info}/RECORD,,"]) + "\n"
            zf.writestr(f"{dist_info}/RECORD", record)
        shutil.rmtree(staging, ignore_errors=True)

    print(
        f"\n{wheel_path} ({wheel_path.stat().st_size / 1e6:.1f} MB, {total / 1e6:.1f} MB of models)"
    )
    return wheel_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--models-dir",
        type=Path,
        required=True,
        help="directory holding *det*.onnx, *rec*.onnx and optionally *cls*.onnx",
    )
    parser.add_argument("-o", "--out", type=Path, default=Path("dist"))
    parser.add_argument("--version", default="0.1.0")
    args = parser.parse_args(argv)
    if not args.models_dir.is_dir():
        raise SystemExit(f"not a directory: {args.models_dir}")
    build(args.models_dir, args.out, args.version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
