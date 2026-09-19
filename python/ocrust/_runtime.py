"""Locating the ONNX Runtime shared library and the model files.

``pip install ocrust`` pulls in the ``onnxruntime`` wheel, so the native
library is already on disk; this module just points the Rust extension at it
before the first call. No system packages, no PATH surgery.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

__all__ = [
    "ensure_runtime",
    "find_onnxruntime",
    "library_in",
    "default_models_dir",
    "runtime_report",
]

#: How each platform names the library, and how its wheels version that name.
#:
#: The versioned spellings differ in a way that matters: Linux appends the
#: version (`libonnxruntime.so.1.30.0`), macOS puts it *before* the extension
#: (`libonnxruntime.1.30.0.dylib`), and Windows normally ships an unversioned
#: `onnxruntime.dll`. A single `name + "*"` glob finds the first and misses the
#: second, which is why macOS could not load a runtime that was installed.
_DYLIB_PATTERNS = {
    "win32": ("onnxruntime.dll", "onnxruntime*.dll"),
    "darwin": ("libonnxruntime.dylib", "libonnxruntime*.dylib"),
}
_DEFAULT_PATTERNS = ("libonnxruntime.so", "libonnxruntime.so*")


def _candidate_patterns() -> tuple[str, ...]:
    """Exact name first, then the glob that matches versioned spellings."""
    return _DYLIB_PATTERNS.get(sys.platform, _DEFAULT_PATTERNS)


def _version_key(path: Path) -> tuple[int, ...]:
    """The version numbers in a library name, as numbers.

    Sorting the strings would put `libonnxruntime.so.1.9.0` above
    `libonnxruntime.so.1.30.0`, because `9` sorts after `3`.
    """
    return tuple(int(part) for part in re.findall(r"\d+", path.name))


def library_in(directory: Path) -> Path | None:
    """The ONNX Runtime library inside `directory`, if there is one.

    The newest version wins when a directory holds several.
    """
    if not directory.is_dir():
        return None
    exact, pattern = _candidate_patterns()
    if (directory / exact).exists():
        return directory / exact
    # `libonnxruntime_providers_*` sit next to the library on some platforms and
    # match the same glob; they are never the library itself.
    matches = [p for p in directory.glob(pattern) if "_provider" not in p.name]
    return max(matches, key=_version_key) if matches else None


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
        for directory in (root / "capi", root):
            found = library_in(directory)
            if found is not None:
                return found
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
