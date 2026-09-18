"""Locating the ONNX Runtime shared library and the model files.

``pip install ocrust`` pulls in the ``onnxruntime`` wheel, so the native
library is already on disk; this module just points the Rust extension at it
before the first call. No system packages, no PATH surgery.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__all__ = ["ensure_runtime", "find_onnxruntime", "default_models_dir", "runtime_report"]

_DYLIB_NAMES = {
    "win32": ("onnxruntime.dll",),
    "darwin": ("libonnxruntime.dylib",),
}
_DEFAULT_NAMES = ("libonnxruntime.so",)


def _candidate_names() -> tuple[str, ...]:
    return _DYLIB_NAMES.get(sys.platform, _DEFAULT_NAMES)


def find_onnxruntime() -> Path | None:
    """Returns the ONNX Runtime library to load, or ``None`` if none is found.

    Order: ``OCRUST_ORT_DYLIB``, ``ORT_DYLIB_PATH``, then the installed
    ``onnxruntime`` package.
    """
    for var in ("OCRUST_ORT_DYLIB", "ORT_DYLIB_PATH"):
        value = os.environ.get(var)
        if value and Path(value).exists():
            return Path(value)

    try:
        import onnxruntime  # noqa: PLC0415  (optional dependency, imported lazily)
    except Exception:
        return None

    roots = [Path(p) for p in getattr(onnxruntime, "__path__", [])]
    for root in roots:
        capi = root / "capi"
        for directory in (capi, root):
            if not directory.is_dir():
                continue
            for name in _candidate_names():
                exact = directory / name
                if exact.exists():
                    return exact
                # Wheels ship versioned names such as libonnxruntime.so.1.28.0.
                matches = sorted(directory.glob(name + "*"))
                if matches:
                    return matches[-1]
    return None


def ensure_runtime() -> Path | None:
    """Exports ``ORT_DYLIB_PATH`` so the Rust extension can dlopen the runtime."""
    found = find_onnxruntime()
    if found is not None:
        os.environ["ORT_DYLIB_PATH"] = str(found)
    return found


def default_models_dir() -> Path | None:
    """Model directory shipped with the optional ``ocrust-models`` package."""
    env = os.environ.get("OCRUST_MODELS_DIR")
    if env and Path(env).is_dir():
        return Path(env)
    try:
        import ocrust_models  # noqa: PLC0415  (optional dependency)
    except Exception:
        return None
    for root in getattr(ocrust_models, "__path__", []):
        models = Path(root) / "models"
        if models.is_dir():
            return models
        if Path(root).is_dir():
            return Path(root)
    return None


def runtime_report() -> dict[str, object]:
    """Diagnostics used by ``ocrust doctor``."""
    dylib = find_onnxruntime()
    models = default_models_dir()
    report: dict[str, object] = {
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "onnxruntime_dylib": str(dylib) if dylib else None,
        "models_dir": str(models) if models else None,
    }
    try:
        import onnxruntime  # noqa: PLC0415

        report["onnxruntime_version"] = onnxruntime.__version__
    except Exception:
        report["onnxruntime_version"] = None
    return report
