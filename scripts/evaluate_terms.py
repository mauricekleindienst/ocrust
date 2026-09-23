#!/usr/bin/env python3
"""Measure the configurable term search (``ocrust.terms``) on the held-out set.

    python scripts/make_terms_holdout.py --out /tmp/terms/holdout
    python scripts/evaluate_terms.py /tmp/terms/holdout -o /tmp/terms/holdout_report.json

Every file is scanned once with ``ocrust.Ocr(page_workers=N)`` (and
``read_stamps=True`` when the engine takes it), handed to ``terms.find`` with the
set's profile, and the hits are counted per file, page and term against
``ground_truth.json``: TP = min(expected, found), FN = expected - TP and
FP = found - TP. The report has precision, recall and F1 overall, recall per
breakage kind and per printing condition (the ``how`` labels of the ground
truth), the numbers per term, every false positive with the text and ``how`` of
the hit and the planted look-alike it fell for, and every miss with the OCR
lines of its page that come closest to the term, so that a term the OCR never
read can be told apart from one the matcher did not find. Markdown goes to
stdout; ``-o`` writes the same as JSON.

Which printed occurrence a hit belongs to is decided by its box. That only
matters for the recall per kind and condition: when a page has two
occurrences of a term and one is found, the box says which one.

``--cache DIR`` keeps the OCR result of every file, so the matcher can be
measured again without scanning. ``--checkout`` loads
``python/ocrust/terms.py`` from this checkout when the installed ocrust has no
``terms`` module.
"""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from inspect import signature
from pathlib import Path
from types import ModuleType
from typing import Any

try:  # Python 3.11+; without it the closest-lines search uses the printed text only
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None

REPO = Path(__file__).resolve().parents[1]
FOLD = str.maketrans(
    {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ẞ": "SS"}
)

# ---------------------------------------------------------------- the matcher and the engine


def load_terms(checkout: bool) -> tuple[ModuleType | None, ModuleType | None, str]:
    """Imports ``ocrust`` and ``ocrust.terms``, lazily, so this file imports
    without them. Returns (ocrust, terms, where terms came from or why not)."""
    try:
        import ocrust
    except ImportError as exc:
        return None, None, f"ocrust is not importable: {type(exc).__name__}: {exc}"
    try:
        from ocrust import terms
    except ImportError as exc:
        reason = f"{type(exc).__name__}: {exc}"
    else:
        return ocrust, terms, f"ocrust.terms ({getattr(terms, '__file__', '?')})"
    source = REPO / "python" / "ocrust" / "terms.py"
    if not checkout:
        return ocrust, None, reason
    if not source.exists():
        return ocrust, None, f"{reason}; {source} does not exist"
    spec = importlib.util.spec_from_file_location("ocrust.terms", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ocrust.terms"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # the checkout's module may not load yet
        del sys.modules["ocrust.terms"]
        return ocrust, None, f"{reason}; {source}: {type(exc).__name__}: {exc}"
    return ocrust, module, f"{source} (the installed ocrust has no terms module)"


def make_engine(ocrust: ModuleType, workers: int | None) -> tuple[Any, dict]:
    kwargs: dict[str, Any] = {}
    parameters = signature(ocrust.Ocr).parameters
    if workers:
        for name in ("page_workers", "workers"):
            if name in parameters:
                kwargs[name] = workers
                break
    if "read_stamps" in parameters:  # a term in a stamp across the text is read on its own
        kwargs["read_stamps"] = True
    return ocrust.Ocr(**kwargs), kwargs


def scan_all(ocrust: ModuleType, engine: Any, paths: list[Path], cache: Path | None) -> Any:
    """Yields (path, Document or Exception, wall ms) in order, from the cache
    where it holds a result newer than the file."""
    todo = []
    for path in paths:
        cached = cache / f"{path.name}.json" if cache else None
        if cached and cached.exists() and cached.stat().st_mtime >= path.stat().st_mtime:
            try:
                data = json.loads(cached.read_text(encoding="utf-8"))
                yield path, ocrust.Document._from_json(data), 0.0
                continue
            except Exception:  # a cache from another version: scan again
                pass
        todo.append(path)
    if not todo:
        return
    start = time.perf_counter()
    if hasattr(engine, "scan_each"):
        results = engine.scan_each([str(p) for p in todo])
    else:
        results = ((str(p), _try_scan(engine, p)) for p in todo)
    by_name = {str(p): p for p in todo}
    done: set[Path] = set()
    try:
        for source, result in results:
            now = time.perf_counter()
            path = by_name[str(source)]
            done.add(path)
            _keep(cache, path, result)
            yield path, result, (now - start) * 1000
            start = now
    except (KeyboardInterrupt, SystemExit, GeneratorExit):
        raise
    except BaseException as exc:  # a panic in the engine ends the whole batch
        print(
            f"  batch scan failed ({type(exc).__name__}: {exc}); scanning the rest one by one",
            file=sys.stderr,
        )
        for path in todo:
            if path in done:
                continue
            start = time.perf_counter()
            result = _try_scan(engine, path)
            _keep(cache, path, result)
            yield path, result, (time.perf_counter() - start) * 1000


def _keep(cache: Path | None, path: Path, result: Any) -> None:
    if cache and not isinstance(result, BaseException):
        cache.mkdir(parents=True, exist_ok=True)
        (cache / f"{path.name}.json").write_text(
            json.dumps(result.to_dict(), ensure_ascii=False), encoding="utf-8"
        )


def _try_scan(engine: Any, path: Path) -> Any:
    try:
        return engine.scan(str(path))
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # pyo3 panics are BaseExceptions
        return exc


# ---------------------------------------------------------------- hits and boxes


def fold(text: str) -> str:
    return text.translate(FOLD).lower()


def box_tuple(box: Any) -> tuple[float, float, float, float] | None:
    if box is None:
        return None
    if hasattr(box, "as_tuple"):
        return tuple(float(v) for v in box.as_tuple())
    if all(hasattr(box, a) for a in ("x0", "y0", "x1", "y1")):
        return (float(box.x0), float(box.y0), float(box.x1), float(box.y1))
    try:
        values = [float(v) for v in box]
    except (TypeError, ValueError):
        return None
    return tuple(values[:4]) if len(values) >= 4 else None


def hit_dict(hit: Any, doc: Any) -> dict:
    """A hit as plain data, tolerant of fields the matcher leaves out; the box
    also as fractions of its page."""
    how = getattr(hit, "how", ())
    if isinstance(how, str):
        how = (how,)
    page = getattr(hit, "page", None)
    box = box_tuple(getattr(hit, "box", None))
    frac = None
    if box and isinstance(page, int) and 1 <= page <= len(doc.pages):
        p = doc.pages[page - 1]
        if p.width and p.height:
            frac = [box[0] / p.width, box[1] / p.height, box[2] / p.width, box[3] / p.height]
    score = getattr(hit, "score", None)
    return {
        "term": getattr(hit, "term", None),
        "page": page,
        "text": getattr(hit, "text", None),
        "how": "+".join(str(h) for h in how),
        "score": round(float(score), 3) if isinstance(score, (int, float)) else score,
        "box": [round(v, 4) for v in frac] if frac else None,
    }


def overlap(a: list[float] | None, b: list[float] | None) -> float:
    """How well a hit box and a printed box agree: the overlap as a share of
    the smaller box, or 0.3 when the hit's centre lies just outside."""
    if not a or not b:
        return 0.0
    ix = min(a[2], b[2]) - max(a[0], b[0])
    iy = min(a[3], b[3]) - max(a[1], b[1])
    if ix > 0 and iy > 0:
        smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return ix * iy / smaller if smaller > 0 else 1.0
    cx, cy = (a[0] + a[2]) / 2, (a[1] + a[3]) / 2
    margin = 0.012
    inside = b[0] - margin <= cx <= b[2] + margin and b[1] - margin <= cy <= b[3] + margin
    return 0.3 if inside else 0.0


def assign(occurrences: list[list[float]], hits: list[dict]) -> dict[int, int]:
    """Occurrence index -> hit index, greedily by box agreement."""
    pairs = sorted(
        (
            (overlap(hit["box"], box), o, h)
            for o, box in enumerate(occurrences)
            for h, hit in enumerate(hits)
        ),
        reverse=True,
    )
    taken_o, taken_h, out = set(), set(), {}
    for score, o, h in pairs:
        if score < 0.2 or o in taken_o or h in taken_h:
            continue
        out[o] = h
        taken_o.add(o)
        taken_h.add(h)
    return out


# ---------------------------------------------------------------- closest OCR lines


def partial_ratio(needle: str, hay: str) -> float:
    """The best similarity of `needle` with a window of `hay` as long as it."""
    if not needle or not hay:
        return 0.0
    if len(hay) <= len(needle):
        return difflib.SequenceMatcher(None, needle, hay, autojunk=False).ratio()
    matcher = difflib.SequenceMatcher(None, needle, hay, autojunk=False)
    best = 0.0
    for a, b, _ in matcher.get_matching_blocks():
        start = max(0, min(b - a, len(hay) - len(needle)))
        window = hay[start : start + len(needle)]
        best = max(best, difflib.SequenceMatcher(None, needle, window, autojunk=False).ratio())
    return best


def closeness(needle: str, line: str) -> float:
    n, h = fold(needle), fold(line)
    squeezed = partial_ratio("".join(n.split()), "".join(h.split()))
    return max(partial_ratio(n, h), squeezed)


def closest_lines(page: Any, needles: list[str], top: int = 3) -> list[dict]:
    """The OCR lines of `page`, alone and joined with the next one, that come
    closest to any of `needles`."""
    lines = [line.text for line in page.lines if line.text.strip()]
    candidates = lines + [f"{a} ⏎ {b}" for a, b in zip(lines, lines[1:])]
    scored = []
    for candidate in candidates:
        score = max((closeness(n, candidate) for n in needles if n.strip()), default=0.0)
        scored.append((score, candidate))
    scored.sort(key=lambda item: -item[0])
    out, seen = [], set()
    for score, candidate in scored:
        if candidate in seen:
            continue
        seen.add(candidate)
        out.append({"score": round(score, 2), "line": candidate})
        if len(out) == top:
            break
    return out


def profile_needles(path: Path) -> dict[str, list[str]]:
    """Term name -> its literal patterns, read from the profile."""
    try:
        if path.suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        elif tomllib is not None:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        else:
            return {}
    except (OSError, ValueError):
        return {}
    out = {}
    for term in data.get("term", data.get("terms", [])):
        patterns = term.get("match", [])
        patterns = [patterns] if isinstance(patterns, str) else list(patterns)
        name = term.get("name") or (patterns[0] if patterns else term.get("regex", ""))
        out[name] = patterns or [name]
    return out


# ---------------------------------------------------------------- one file


@dataclass
class Result:
    entry: dict
    pages: int = 0
    scan_ms: float = 0.0
    engine_ms: float = 0.0
    find_ms: float = 0.0
    error: str | None = None
    hits: list[dict] = field(default_factory=list)
    cells: list[dict] = field(default_factory=list)
    false_positives: list[dict] = field(default_factory=list)
    misses: list[dict] = field(default_factory=list)


def judge(result: Result, doc: Any, needles: dict[str, list[str]]) -> None:
    entry = result.entry
    expected = {(e["page"], e["term"]): e for e in entry["expect"]}
    found: dict[tuple[Any, Any], list[dict]] = defaultdict(list)
    for hit in result.hits:
        found[(hit["page"], hit["term"])].append(hit)
    decoys = entry.get("decoys", [])
    for key in sorted(set(expected) | set(found), key=lambda k: (str(k[0]), str(k[1]))):
        page, term = key
        e = expected.get(key)
        hits = found.get(key, [])
        count = e["count"] if e else 0
        tp = min(count, len(hits))
        boxes = e.get("boxes", []) if e else []
        hows = e.get("how", []) if e else []
        texts = e.get("text", []) if e else []
        matched = assign(boxes, hits) if boxes and len(boxes) == count else {}
        credit = [1.0 if o in matched else 0.0 for o in range(count)]
        spare = tp - len(matched)
        if spare > 0:  # found, but not where it was printed: share the credit
            loose = [o for o in range(count) if o not in matched]
            for o in loose:
                credit[o] = min(1.0, spare / len(loose))
        elif spare < 0:  # more box matches than countable hits cannot happen; be safe
            credit = [min(1.0, c) for c in credit]
        occurrences = [
            {
                "how": hows[o] if o < len(hows) else "?",
                "text": texts[o] if o < len(texts) else "",
                "credit": round(credit[o], 3),
            }
            for o in range(count)
        ]
        result.cells.append(
            {
                "page": page,
                "term": term,
                "expected": count,
                "found": len(hits),
                "tp": tp,
                "fn": count - tp,
                "fp": len(hits) - tp,
                "occurrences": occurrences,
            }
        )
        if len(hits) > tp:
            used = set(matched.values())
            unmatched = []
            for i, hit in enumerate(hits):
                if i in used:
                    continue
                fell_for = [
                    d
                    for d in decoys
                    if d["term"] == term
                    and d["page"] == page
                    and (hit["box"] is None or overlap(hit["box"], d.get("box")) >= 0.2)
                ]
                unmatched.append((hit, fell_for[0] if fell_for else None))
            unmatched.sort(key=lambda item: item[1] is None)  # decoys first
            out_of_range = not isinstance(page, int) or not 1 <= page <= len(doc.pages)
            for hit, decoy in unmatched[: len(hits) - tp]:
                result.false_positives.append(
                    {
                        "file": entry["file"],
                        **hit,
                        "decoy": decoy["text"] if decoy else None,
                        "why": decoy["why"] if decoy else None,
                        "absent": term in entry.get("absent", []),
                        "page_out_of_range": out_of_range,
                    }
                )
        if count > tp:
            missed = [o for o in occurrences if o["credit"] < 1.0]
            wanted = list(needles.get(term, [])) + [t.replace("\n", " ") for t in texts]
            lines = (
                closest_lines(doc.pages[page - 1], wanted)
                if isinstance(page, int) and 1 <= page <= len(doc.pages)
                else []
            )
            result.misses.append(
                {
                    "file": entry["file"],
                    "page": page,
                    "term": term,
                    "expected": count,
                    "found": len(hits),
                    "missed": missed,
                    "hits": [h["text"] for h in hits],
                    "closest": lines,
                }
            )


def run_file(
    terms: ModuleType,
    profile: Any,
    entry: dict,
    doc: Any,
    wall_ms: float,
    needles: dict[str, list[str]],
) -> Result:
    result = Result(entry)
    if isinstance(doc, BaseException):
        result.error = f"scan: {type(doc).__name__}: {doc}"
        _all_missed(result)
        return result
    result.pages = len(doc.pages)
    result.scan_ms = wall_ms
    result.engine_ms = float(getattr(doc, "elapsed_ms", 0.0) or 0.0)
    start = time.perf_counter()
    try:
        report = terms.find(doc, profile)
        hits = list(getattr(report, "hits", report))
    except Exception as exc:
        result.find_ms = (time.perf_counter() - start) * 1000
        result.error = f"find: {type(exc).__name__}: {exc}"
        _all_missed(result)
        return result
    result.find_ms = (time.perf_counter() - start) * 1000
    result.hits = [hit_dict(hit, doc) for hit in hits]
    judge(result, doc, needles)
    return result


def _all_missed(result: Result) -> None:
    for e in result.entry["expect"]:
        result.cells.append(
            {
                "page": e["page"],
                "term": e["term"],
                "expected": e["count"],
                "found": 0,
                "tp": 0,
                "fn": e["count"],
                "fp": 0,
                "occurrences": [
                    {"how": how, "text": text, "credit": 0.0}
                    for how, text in zip(e["how"], e["text"])
                ],
            }
        )


# ---------------------------------------------------------------- summary


def prf(tp: float, fn: float, fp: float) -> dict:
    p = tp / (tp + fp) if tp + fp else 1.0
    r = tp / (tp + fn) if tp + fn else 1.0
    f = 2 * p * r / (p + r) if p + r else 0.0
    return {"precision": round(p, 4), "recall": round(r, 4), "f1": round(f, 4)}


def summarize(results: list[Result], wall_s: float) -> dict:
    tp = sum(c["tp"] for r in results for c in r.cells)
    fn = sum(c["fn"] for r in results for c in r.cells)
    fp = sum(c["fp"] for r in results for c in r.cells)
    kinds: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    conditions: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    per_term: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        for c in r.cells:
            t = per_term[c["term"]]
            t["expected"] += c["expected"]
            t["tp"] += c["tp"]
            t["fn"] += c["fn"]
            t["fp"] += c["fp"]
            for occ in c["occurrences"]:
                kind, *rest = occ["how"].split("+")
                kinds[kind][0] += 1
                kinds[kind][1] += occ["credit"]
                for cond in rest or ["(none)"]:
                    conditions[cond][0] += 1
                    conditions[cond][1] += occ["credit"]
    fps = [fp for r in results for fp in r.false_positives]
    pages = sum(r.pages for r in results) or 1
    return {
        "files": len(results),
        "pages": sum(r.pages for r in results),
        "errors": [f"{r.entry['file']}: {r.error}" for r in results if r.error],
        "expected": tp + fn,
        "found": tp + fp,
        "tp": tp,
        "fn": fn,
        "fp": fp,
        **prf(tp, fn, fp),
        "fp_on_decoys": sum(1 for f in fps if f["decoy"]),
        "fp_absent_terms": sum(1 for f in fps if f["absent"]),
        "files_without_error": sum(
            1
            for r in results
            if not r.error and all(c["fn"] == 0 and c["fp"] == 0 for c in r.cells)
        ),
        "kinds": {
            k: {"expected": n, "found": round(v, 2), "recall": round(v / n, 4)}
            for k, (n, v) in sorted(kinds.items(), key=lambda kv: -kv[1][0])
        },
        "conditions": {
            k: {"expected": n, "found": round(v, 2), "recall": round(v / n, 4)}
            for k, (n, v) in sorted(conditions.items(), key=lambda kv: -kv[1][0])
        },
        "terms": {
            name: {**dict(c), **prf(c["tp"], c["fn"], c["fp"])}
            for name, c in sorted(per_term.items())
        },
        "timing": {
            "wall_s": round(wall_s, 1),
            "scan_ms_per_page": round(sum(r.scan_ms for r in results) / pages, 1),
            "engine_ms_per_page": round(sum(r.engine_ms for r in results) / pages, 1),
            "find_ms_per_page": round(sum(r.find_ms for r in results) / pages, 2),
        },
    }


def pct(v: float) -> str:
    return f"{100 * v:.1f} %"


def markdown(summary: dict, results: list[Result], source: str, root: Path, engine: dict) -> str:
    s = summary
    out = [
        f"# Term search on `{root}`",
        "",
        f"Matcher: {source}  ",
        f"Engine: `Ocr({', '.join(f'{k}={v!r}' for k, v in engine.items())})`  ",
        f"{s['files']} files, {s['pages']} pages; {s['files_without_error']} files entirely right.",
        "",
        "| expected | found | TP | FN | FP | precision | recall | F1 | FP on decoys |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| {s['expected']} | {s['found']} | {s['tp']} | {s['fn']} | {s['fp']} | "
        f"{pct(s['precision'])} | {pct(s['recall'])} | {pct(s['f1'])} | {s['fp_on_decoys']} |",
        "",
    ]
    if s["errors"]:
        out += ["**Errors**", ""] + [f"- {e}" for e in s["errors"]] + [""]
    for title, key in (("Recall by breakage", "kinds"), ("Recall by condition", "conditions")):
        out += [f"## {title}", "", "| | expected | found | recall |", "|---|---:|---:|---:|"]
        out += [
            f"| {k} | {v['expected']} | {v['found']:g} | {pct(v['recall'])} |"
            for k, v in s[key].items()
        ]
        out.append("")
    out += [
        "## Per term",
        "",
        "| term | expected | TP | FN | FP | precision | recall |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    out += [
        f"| {name} | {t['expected']} | {t['tp']} | {t['fn']} | {t['fp']} | "
        f"{pct(t['precision'])} | {pct(t['recall'])} |"
        for name, t in s["terms"].items()
    ]
    out.append("")
    fps = [f for r in results for f in r.false_positives]
    out += [f"## False positives ({len(fps)})", ""]
    for f in fps:
        tag = f" — **decoy** “{f['decoy']}” ({f['why']})" if f["decoy"] else ""
        if f["absent"] and not f["decoy"]:
            tag = " — term listed as absent in this file"
        if f["page_out_of_range"]:
            tag += " — page out of range"
        out.append(
            f"- `{f['file']}` p{f['page']} **{f['term']}**: “{f['text']}” "
            f"how={f['how'] or '-'} score={f['score']}{tag}"
        )
    out.append("")
    misses = [m for r in results for m in r.misses]
    out += [
        f"## Misses ({sum(m['expected'] - min(m['expected'], m['found']) for m in misses)})",
        "",
    ]
    for m in misses:
        printed = "; ".join(
            f"{o['how']} “{o['text'].replace(chr(10), ' ⏎ ')}”" for o in m["missed"]
        )
        found = f", found {m['found']}: {m['hits']}" if m["found"] else ""
        out.append(
            f"- `{m['file']}` p{m['page']} **{m['term']}** "
            f"({m['found']}/{m['expected']}{found}) printed: {printed}"
        )
        for c in m["closest"]:
            out.append(f"    - {c['score']:.2f} `{c['line']}`")
    out.append("")
    t = s["timing"]
    out += [
        "## Timing",
        "",
        f"- wall clock {t['wall_s']} s",
        f"- scan {t['scan_ms_per_page']} ms/page (wall), engine {t['engine_ms_per_page']} ms/page",
        f"- terms.find {t['find_ms_per_page']} ms/page",
        "",
    ]
    return "\n".join(out)


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("root", type=Path, help="directory with ground_truth.json")
    parser.add_argument("-o", "--output", type=Path, help="write the report as JSON here")
    parser.add_argument("--workers", type=int, default=None, help="page workers (default: cores)")
    parser.add_argument("--profile", type=Path, help="profile instead of the one the set names")
    parser.add_argument("--cache", type=Path, help="keep OCR results here and reuse them")
    parser.add_argument("--only", nargs="*", help="files whose name contains one of these")
    parser.add_argument(
        "--checkout", action="store_true", help="load python/ocrust/terms.py from this checkout"
    )
    args = parser.parse_args()

    truth_path = args.root / "ground_truth.json"
    if not truth_path.exists():
        print(f"no ground truth at {truth_path}; run scripts/make_terms_holdout.py first")
        return 2
    truth = json.loads(truth_path.read_text(encoding="utf-8"))
    ocrust, terms, source = load_terms(args.checkout)
    if ocrust is None or terms is None:
        print(
            "ocrust.terms is not available, so there is nothing to measure yet.\n"
            f"  {source}\n"
            "Install an ocrust that has the term search, or pass --checkout to load "
            "python/ocrust/terms.py from this checkout."
        )
        return 2
    profile_path = args.profile or args.root / truth.get("profile", "profile.toml")
    try:
        profile = terms.load(str(profile_path))
    except Exception as exc:
        print(f"terms.load({str(profile_path)!r}) failed: {type(exc).__name__}: {exc}")
        return 2
    needles = profile_needles(profile_path)

    entries = [e for e in truth["files"] if not args.only or any(o in e["file"] for o in args.only)]
    engine, engine_kwargs = make_engine(ocrust, args.workers)
    by_name = {e["file"]: e for e in entries}
    paths = [args.root / e["file"] for e in entries]
    results: list[Result] = []
    start = time.perf_counter()
    for path, doc, wall_ms in scan_all(ocrust, engine, paths, args.cache):
        result = run_file(terms, profile, by_name[path.name], doc, wall_ms, needles)
        results.append(result)
        state = "error" if result.error else f"{sum(c['tp'] for c in result.cells)} TP"
        print(f"  {path.name}: {result.pages} p, {state}", file=sys.stderr)
    summary = summarize(results, time.perf_counter() - start)
    print(markdown(summary, results, source, args.root, engine_kwargs))
    if args.output:
        report = {
            "root": str(args.root),
            "matcher": source,
            "engine": engine_kwargs,
            "summary": summary,
            "false_positives": [f for r in results for f in r.false_positives],
            "misses": [m for r in results for m in r.misses],
            "files": [
                {
                    "file": r.entry["file"],
                    "pages": r.pages,
                    "error": r.error,
                    "scan_ms": round(r.scan_ms, 1),
                    "engine_ms": round(r.engine_ms, 1),
                    "find_ms": round(r.find_ms, 2),
                    "cells": r.cells,
                    "hits": r.hits,
                }
                for r in results
            ],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
