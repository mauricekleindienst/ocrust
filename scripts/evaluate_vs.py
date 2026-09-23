#!/usr/bin/env python3
"""Measure the classification-marking detector against the VS corpus.

    python scripts/make_vs_corpus.py --out /tmp/vs/corpus
    python scripts/evaluate_vs.py /tmp/vs/corpus -o /tmp/vs/report.json

Every file is scanned, handed to ``ocrust.markings.inspect`` and compared with
``ground_truth.json``: is the document classified at all, at which level and
under which label, which TLP and company marking does it carry, which pages are
marked, and was a mention in running text taken for a marking. The report is
Markdown on stdout; the list of every error is the part to read first. With
``-o`` the numbers and every file's result are written as JSON as well.

A miss is reported together with the OCR lines that resemble the marking, so a
marking the OCR never read can be told apart from one the detector did not
recognise.
"""

from __future__ import annotations

import argparse
import difflib
import fnmatch
import importlib.util
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from inspect import signature
from pathlib import Path
from types import ModuleType
from typing import Any

REPO = Path(__file__).resolve().parents[1]
LEVELS = range(5)

# ---------------------------------------------------------------- the detector


def load_detector() -> tuple[ModuleType | None, str]:
    """Imports ``ocrust.markings``, lazily, so this file imports without it.

    The detector is pure Python on top of ``ocrust``; when the installed wheel
    predates it, the module is taken from this checkout instead, so it can be
    measured before a release.
    """
    try:
        from ocrust import markings
    except ImportError as exc:
        reason = f"{type(exc).__name__}: {exc}"
    else:
        return markings, f"ocrust.markings ({markings.__file__})"
    source = REPO / "python" / "ocrust" / "markings.py"
    if not source.exists():
        return None, reason
    spec = importlib.util.spec_from_file_location("ocrust.markings", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules["ocrust.markings"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # the checkout's module may not load yet
        del sys.modules["ocrust.markings"]
        return None, f"{reason}; {source}: {type(exc).__name__}: {exc}"
    return module, f"{source} (the installed ocrust has no markings module)"


def make_engine(ocrust: ModuleType, workers: int | None) -> Any:
    """Builds the engine as `ocrust vs` does, with `workers` under whichever
    name it takes."""
    kwargs: dict[str, Any] = {}
    parameters = signature(ocrust.Ocr).parameters
    if workers:
        for name in ("workers", "page_workers"):
            if name in parameters:
                kwargs[name] = workers
                break
    if "read_stamps" in parameters:
        # A stamp across the text is read from its colour; the command does it.
        kwargs["read_stamps"] = True
    return ocrust.Ocr(**kwargs)


def finding_dict(finding: Any) -> dict:
    """A Finding as plain data, tolerant of fields the detector leaves out."""
    box = getattr(finding, "box", None)
    if hasattr(box, "as_tuple"):
        box = list(box.as_tuple())
    elif box is not None:
        try:
            box = [float(v) for v in box]
        except (TypeError, ValueError):
            box = str(box)
    return {
        "kind": getattr(finding, "kind", None),
        "scheme": getattr(finding, "scheme", None),
        "level": getattr(finding, "level", None),
        "label": getattr(finding, "label", None),
        "text": getattr(finding, "text", None),
        "page": getattr(finding, "page", None),
        "confidence": getattr(finding, "confidence", None),
        "fuzzy": getattr(finding, "fuzzy", None),
        "reason": getattr(finding, "reason", None),
        "box": box,
    }


# ---------------------------------------------------------------- one file


@dataclass
class Result:
    """What the detector said about one file, next to what it should have said."""

    entry: dict
    pages: int = 0
    scan_ms: float = 0.0
    inspect_ms: float = 0.0
    error: str | None = None
    level: int = 0
    label: str | None = None
    tlp: str | None = None
    company: str | None = None
    page_levels: list[int] = field(default_factory=list)
    unmarked: list[int] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    ocr_hits: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)

    @property
    def expected_classified(self) -> bool:
        return self.entry["level"] >= 1

    @property
    def classified(self) -> bool:
        return self.level >= 1

    @property
    def carries_marking(self) -> bool:
        e = self.entry
        return bool(e["label"] or e["tlp"] or e["company"])

    @property
    def reported_marking(self) -> bool:
        """Anything at all reported as a marking, of any kind."""
        return bool(
            self.level
            or self.label
            or self.tlp
            or self.company
            or any(f["kind"] == "marking" for f in self.findings)
        )

    @property
    def document_ok(self) -> bool:
        e = self.entry
        return (
            self.error is None
            and self.level == e["level"]
            and self.label == e["label"]
            and self.tlp == e["tlp"]
            and self.company == e["company"]
        )

    def expected_unmarked(self) -> list[int]:
        if self.entry["level"] < 1:
            return []
        return [number for number, level in enumerate(self.entry["pages"], 1) if level < 1]


def judge(result: Result) -> None:
    """Names everything that is wrong with `result`, most serious first.

    What follows from an earlier issue is left out: a missed single page is
    not also a page error, and a false alarm's label is not also a label error.
    """
    e, issues = result.entry, result.issues
    if result.error:
        issues.append(f"error: {result.error}")
        return
    clean_negative = not result.carries_marking
    if clean_negative and e["mentions"] and result.reported_marking:
        issues.append("mention taken for a marking")
    elif result.expected_classified and not result.classified:
        issues.append("missed")
    elif not result.expected_classified and result.classified:
        issues.append("false alarm")
    elif result.level != e["level"]:
        issues.append(f"level {result.level} instead of {e['level']}")
    implied = bool(issues) and (result.label is None or e["label"] is None)
    if result.label != e["label"] and not implied:
        issues.append("wrong label")
    if result.tlp != e["tlp"]:
        issues.append("tlp")
    if result.company != e["company"]:
        issues.append("company")
    expected_pages = [level >= 1 for level in e["pages"]]
    got_pages = [level >= 1 for level in result.page_levels]
    if len(got_pages) != len(expected_pages):
        issues.append(f"{len(got_pages)} page levels for {len(expected_pages)} pages")
    elif got_pages != expected_pages and (len(expected_pages) > 1 or not issues):
        issues.append("pages")
    if sorted(result.unmarked) != result.expected_unmarked():
        issues.append("unmarked pages")


def normalize(text: str) -> str:
    return re.sub(r"[\s\-–—_:/.,;]", "", text.upper().replace("ß", "SS"))


def ocr_hits(doc: Any, markings: list[str], limit: int = 3) -> list[str]:
    """OCR lines that look like one of the drawn markings.

    Letter-spaced and dash variants collapse in `normalize`; beyond that, the
    best window of each line is compared with the marking, so a marking that
    sits inside a longer line (the letterhead, a page number) is still found.
    """
    targets = [normalize(m) for m in markings if normalize(m)]
    hits = []
    for page in doc.pages:
        for line in page.lines:
            text = normalize(line.text)
            best = 0.0
            for target in targets:
                if target in text:
                    best = 1.0
                    break
                width = len(target)
                for start in range(max(1, len(text) - width + 1)):
                    window = text[start : start + width]
                    best = max(best, difflib.SequenceMatcher(None, target, window).ratio())
            if best >= 0.7:
                y = int(getattr(line.box, "y0", 0))
                hits.append(f"p{page.index + 1} y={y} {line.text!r}")
                if len(hits) >= limit:
                    return hits
    return hits


# ---------------------------------------------------------------- scanning


def scan_all(engine: Any, root: Path, entries: list[dict], batch: int):
    """Yields (entry, document or None, error or None) in corpus order.

    Files go to `scan_many` a batch at a time, so documents are read in
    parallel and progress still shows. A batch that fails is rescanned file by
    file, so one unreadable file costs one result, not the batch.
    """
    for start in range(0, len(entries), batch):
        chunk = entries[start : start + batch]
        paths = [str(root / entry["file"]) for entry in chunk]
        try:
            documents = list(engine.scan_many(paths))
        except Exception:
            documents = None
        if documents is not None and len(documents) == len(chunk):
            for entry, document in zip(chunk, documents):
                yield entry, document, None
            continue
        for entry, path in zip(chunk, paths):
            try:
                yield entry, engine.scan(path), None
            except Exception as exc:
                yield entry, None, f"{type(exc).__name__}: {exc}"


def evaluate(markings: ModuleType, engine: Any, root: Path, entries: list[dict], batch: int):
    results: list[Result] = []
    inspect_failures = 0
    for index, (entry, document, error) in enumerate(scan_all(engine, root, entries, batch), 1):
        result = Result(entry, error=error)
        if document is not None:
            result.pages = len(document.pages)
            result.scan_ms = float(getattr(document, "elapsed_ms", 0.0))
            started = time.perf_counter()
            try:
                # Never `file=`: the detector reads grades out of file names,
                # and the corpus names its files after the answer.
                report = markings.inspect(document)
            except Exception as exc:
                result.error = f"inspect: {type(exc).__name__}: {exc}"
                inspect_failures += 1
                if inspect_failures == 1:
                    import traceback

                    traceback.print_exc(file=sys.stderr)
            else:
                result.level = int(getattr(report, "level", 0) or 0)
                result.label = getattr(report, "label", None)
                result.tlp = getattr(report, "tlp", None)
                result.company = getattr(report, "company", None)
                result.page_levels = [int(v or 0) for v in getattr(report, "pages", ()) or ()]
                result.unmarked = [int(v) for v in getattr(report, "unmarked_pages", ()) or ()]
                result.findings = [finding_dict(f) for f in getattr(report, "findings", ()) or ()]
            result.inspect_ms = (time.perf_counter() - started) * 1000
        judge(result)
        if result.issues and document is not None:
            result.ocr_hits = ocr_hits(document, entry.get("markings", []))
        status = "ok " if not result.issues else "ERR" if result.error else "-- "
        got = f"L{result.level} {result.label or '-'}"
        print(
            f"[{index:3d}/{len(entries)}] {status} {entry['file']:48} {result.pages}p "
            f"{result.scan_ms:6.0f} ms  {got:24} {', '.join(result.issues)}",
            file=sys.stderr,
        )
        results.append(result)
    return results


# ---------------------------------------------------------------- metrics


def share(part: int, whole: int) -> str:
    return f"{part}/{whole} ({part / whole:.0%})" if whole else "–"


def rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def binary(results: list[Result]) -> dict:
    tp = sum(r.expected_classified and r.classified for r in results)
    fp = sum(not r.expected_classified and r.classified for r in results)
    fn = sum(r.expected_classified and not r.classified for r in results)
    tn = len(results) - tp - fp - fn
    precision = rate(tp, tp + fp)
    recall = rate(tp, tp + fn)
    f1 = (
        round(2 * precision * recall / (precision + recall), 4)
        if precision and recall
        else (0.0 if tp + fp + fn else None)
    )
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": precision,
        "recall": recall,
        "f1": f1,
    }


def summarize(results: list[Result]) -> dict:
    positives = [r for r in results if r.expected_classified]
    labelled = [r for r in results if r.entry["label"]]
    with_tlp = [r for r in results if r.entry["tlp"]]
    with_company = [r for r in results if r.entry["company"]]
    clean = [r for r in results if not r.carries_marking]
    mentioning = [r for r in clean if r.entry["mentions"]]
    all_mentions = [r for r in results if r.entry["mentions"]]

    marked_pages = found_pages = unmarked_pages = false_pages = 0
    page_count_mismatch = 0
    for r in results:
        if r.error:
            continue
        got = r.page_levels + [0] * (len(r.entry["pages"]) - len(r.page_levels))
        if len(r.page_levels) != len(r.entry["pages"]):
            page_count_mismatch += 1
        for expected, detected in zip(r.entry["pages"], got):
            if expected >= 1:
                marked_pages += 1
                found_pages += detected >= 1
            else:
                unmarked_pages += 1
                false_pages += detected >= 1
    incomplete = [r for r in results if r.expected_unmarked()]
    complete = [r for r in positives if not r.expected_unmarked()]

    return {
        "files": len(results),
        "pages": sum(len(r.entry["pages"]) for r in results),
        "errors": sum(r.error is not None for r in results),
        "classified": binary(results),
        "level_exact": [sum(r.level == r.entry["level"] for r in positives), len(positives)],
        "label_exact": [sum(r.label == r.entry["label"] for r in labelled), len(labelled)],
        "label_false": [
            sum(r.label is not None for r in results if not r.entry["label"]),
            sum(not r.entry["label"] for r in results),
        ],
        "tlp_exact": [sum(r.tlp == r.entry["tlp"] for r in with_tlp), len(with_tlp)],
        "tlp_false": [
            sum(r.tlp is not None for r in results if not r.entry["tlp"]),
            sum(not r.entry["tlp"] for r in results),
        ],
        "company_exact": [
            sum(r.company == r.entry["company"] for r in with_company),
            len(with_company),
        ],
        "company_false": [
            sum(r.company is not None for r in results if not r.entry["company"]),
            sum(not r.entry["company"] for r in results),
        ],
        "pages_found": [found_pages, marked_pages],
        "pages_false": [false_pages, unmarked_pages],
        "page_count_mismatch": page_count_mismatch,
        "unmarked_exact": [
            sum(sorted(r.unmarked) == r.expected_unmarked() for r in incomplete),
            len(incomplete),
        ],
        "unmarked_false": [sum(bool(r.unmarked) for r in complete), len(complete)],
        "negatives_false": [sum(r.reported_marking for r in clean), len(clean)],
        "mention_as_marking": [sum(r.reported_marking for r in mentioning), len(mentioning)],
        "mention_found": [
            sum(any(f["kind"] == "mention" for f in r.findings) for r in all_mentions),
            len(all_mentions),
        ],
        "documents_ok": [sum(r.document_ok for r in results), len(results)],
    }


def confusion(results: list[Result]) -> list[list[int]]:
    matrix = [[0] * len(LEVELS) for _ in LEVELS]
    for r in results:
        matrix[r.entry["level"]][min(max(r.level, 0), 4)] += 1
    return matrix


def breakdown(results: list[Result], key) -> dict[str, dict]:
    groups: dict[str, list[Result]] = defaultdict(list)
    for r in results:
        for name in key(r):
            groups[name].append(r)
    out = {}
    for name, members in sorted(groups.items()):
        counts = binary(members)
        positives = [r for r in members if r.expected_classified]
        labelled = [r for r in members if r.entry["label"]]
        out[name] = {
            **counts,
            "files": len(members),
            "positives": len(positives),
            "level_exact": [sum(r.level == r.entry["level"] for r in positives), len(positives)],
            "label_exact": [sum(r.label == r.entry["label"] for r in labelled), len(labelled)],
            "documents_ok": [sum(r.document_ok for r in members), len(members)],
        }
    return out


# ---------------------------------------------------------------- report


def markdown(summary: dict, matrix, by_category, by_degradation, by_tag, results, timing) -> str:
    out: list[str] = []
    add = out.append
    c = summary["classified"]

    def pct(value: float | None) -> str:
        return "–" if value is None else f"{value:.1%}"

    add("## Classification markings\n")
    add(f"{summary['files']} files, {summary['pages']} pages, {summary['errors']} errors.\n")
    add("| measure | value |")
    add("|---|---|")
    counts = f"{c['tp']} / {c['fp']} / {c['fn']} / {c['tn']}"
    add(f"| classified (level ≥ 1): TP / FP / FN / TN | {counts} |")
    add(
        f"| precision / recall / F1 | {pct(c['precision'])} / {pct(c['recall'])} / {pct(c['f1'])} |"
    )
    rows = [
        ("exact level, classified files", "level_exact"),
        ("exact label, labelled files", "label_exact"),
        ("a label on a file without one", "label_false"),
        ("TLP right, files with TLP", "tlp_exact"),
        ("TLP on a file without one", "tlp_false"),
        ("company marking right", "company_exact"),
        ("company marking on a file without one", "company_false"),
        ("marked pages found", "pages_found"),
        ("unmarked pages reported as marked", "pages_false"),
        ("incomplete marking found (exact unmarked pages)", "unmarked_exact"),
        ("fully marked documents reported incomplete", "unmarked_false"),
        ("negatives with any marking reported", "negatives_false"),
        ("negatives: mention taken for a marking", "mention_as_marking"),
        ("files with mentions where a mention was reported", "mention_found"),
        ("documents entirely right", "documents_ok"),
    ]
    for title, key in rows:
        add(f"| {title} | {share(*summary[key])} |")
    if summary["page_count_mismatch"]:
        add(f"| page level lists of the wrong length | {summary['page_count_mismatch']} |")
    add(
        f"| time | {timing['total_s']:.1f} s total, {timing['ms_per_page']:.0f} ms/page "
        f"(detector {timing['inspect_ms_per_page']:.1f} ms/page) |"
    )

    add("\n### Level confusion (rows expected, columns detected)\n")
    add("| expected | " + " | ".join(str(level) for level in LEVELS) + " |")
    add("|---" * (len(LEVELS) + 1) + "|")
    for level, row in zip(LEVELS, matrix):
        add(f"| {level} | " + " | ".join(str(v) if v else "·" for v in row) + " |")

    for title, table in (
        ("By category", by_category),
        ("By degradation", by_degradation),
    ):
        add(f"\n### {title}\n")
        add("| group | files | classified | recall | FP | level | label | documents right |")
        add("|---|---|---|---|---|---|---|---|")
        for name, row in table.items():
            add(
                f"| {name} | {row['files']} | {row['positives']} | {pct(row['recall'])} | "
                f"{row['fp']} | {share(*row['level_exact'])} | {share(*row['label_exact'])} | "
                f"{share(*row['documents_ok'])} |"
            )

    add("\n### By how the marking was drawn (files carrying a marking)\n")
    add("| tag | files | documents right |")
    add("|---|---|---|")
    for name, row in by_tag.items():
        add(f"| {name} | {row['files']} | {share(*row['documents_ok'])} |")

    wrong = [r for r in results if r.issues]
    add(f"\n### Errors ({len(wrong)})\n")
    if not wrong:
        add("None.")
        return "\n".join(out) + "\n"
    order = ["error", "missed", "false alarm", "mention", "level", "wrong label"]

    def severity(r: Result) -> tuple:
        first = r.issues[0]
        rank = next((i for i, key in enumerate(order) if first.startswith(key)), len(order))
        return rank, r.entry["file"]

    add(
        "Per file: what the ground truth says, the markings as drawn, what the detector "
        "reported, each finding (kind, scheme, level, label, page, text, confidence, reason), "
        "and the OCR lines that resemble a drawn marking — none means the OCR never read it.\n"
    )
    add("```")
    for r in sorted(wrong, key=severity):
        add(error_entry(r))
    add("```")
    return "\n".join(out) + "\n"


def describe(level: int, label, tlp, company, pages) -> str:
    return f"L{level} {label or '-'} | tlp {tlp or '-'} | company {company or '-'} | pages {pages}"


def error_entry(r: Result) -> str:
    e = r.entry
    lines = [f"{e['file']}  [{e['category']}, {e['degradation']}]  {'; '.join(r.issues)}"]
    lines.append(
        "  expected " + describe(e["level"], e["label"], e["tlp"], e["company"], e["pages"])
    )
    if e.get("markings"):
        lines.append("  drawn    " + "  ".join(f"'{m}'" for m in e["markings"]))
    if r.error:
        lines.append(f"  error    {r.error}")
    else:
        lines.append(
            "  got      "
            + describe(r.level, r.label, r.tlp, r.company, r.page_levels)
            + (f" | unmarked {r.unmarked}" if r.unmarked or r.expected_unmarked() else "")
        )
    if r.findings:
        for f in r.findings:
            flags = " fuzzy" if f["fuzzy"] else ""
            confidence = f" {f['confidence']:.2f}" if isinstance(f["confidence"], float) else ""
            reason = f" ({f['reason']})" if f["reason"] else ""
            lines.append(
                f"  found    {f['kind']} {f['scheme']} L{f['level']} {f['label']} p{f['page']} "
                f"{f['text']!r}{confidence}{flags}{reason}"
            )
    elif not r.error:
        lines.append("  found    nothing")
    if e.get("markings"):
        lines.append(
            "  ocr      "
            + ("\n           ".join(r.ocr_hits) if r.ocr_hits else "no line resembles it")
        )
    if e.get("note"):
        lines.append(
            f"  note     {e['note']}" + ("  [declassified]" if e.get("declassified") else "")
        )
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path, help="directory written by make_vs_corpus.py")
    parser.add_argument("-o", "--output", type=Path, help="also write the results as JSON")
    parser.add_argument(
        "--only",
        action="append",
        metavar="CATEGORY",
        help="only these categories; globs and commas allowed (neg-*,de-header)",
    )
    parser.add_argument("--workers", type=int, help="pages scanned in parallel (default: cores)")
    parser.add_argument("--batch", type=int, default=16, help="files per scan_many call")
    args = parser.parse_args()

    manifest = args.corpus / "ground_truth.json"
    if not manifest.exists():
        print(f"no {manifest}; run scripts/make_vs_corpus.py --out {args.corpus}", file=sys.stderr)
        return 1
    entries = json.loads(manifest.read_text(encoding="utf-8"))["files"]
    if args.only:
        patterns = [p.strip() for arg in args.only for p in arg.split(",") if p.strip()]
        entries = [e for e in entries if any(fnmatch.fnmatch(e["category"], p) for p in patterns)]
        if not entries:
            print(f"no files in categories {patterns}", file=sys.stderr)
            return 1

    try:
        import ocrust
    except ImportError as exc:
        print(f"cannot import ocrust: {exc}", file=sys.stderr)
        return 2
    markings, source = load_detector()
    if markings is None or not hasattr(markings, "inspect"):
        print(
            "The marking detector is not available yet: ocrust.markings.inspect could not be "
            f"imported ({source}). It is expected in python/ocrust/markings.py.",
            file=sys.stderr,
        )
        return 2
    print(f"detector: {source}", file=sys.stderr)

    engine = make_engine(ocrust, args.workers)
    started = time.perf_counter()
    results = evaluate(markings, engine, args.corpus, entries, max(1, args.batch))
    total_s = time.perf_counter() - started
    pages = max(1, sum(r.pages for r in results))
    timing = {
        "total_s": round(total_s, 2),
        "pages": pages,
        "ms_per_page": round(total_s * 1000 / pages, 1),
        "inspect_ms_per_page": round(sum(r.inspect_ms for r in results) / pages, 2),
        "workers": args.workers,
    }

    summary = summarize(results)
    matrix = confusion(results)
    by_category = breakdown(results, lambda r: [r.entry["category"]])
    by_degradation = breakdown(results, lambda r: [r.entry["degradation"]])
    marked = [r for r in results if r.carries_marking]
    by_tag = breakdown(marked, lambda r: r.entry.get("tags", []))
    print(markdown(summary, matrix, by_category, by_degradation, by_tag, results, timing))

    if args.output:
        payload = {
            "corpus": str(args.corpus),
            "detector": source,
            "ocrust": getattr(ocrust, "__version__", None),
            "timing": timing,
            "summary": summary,
            "confusion": {"rows_expected_columns_detected": matrix},
            "by_category": by_category,
            "by_degradation": by_degradation,
            "by_tag": by_tag,
            "errors": [{"file": r.entry["file"], "issues": r.issues} for r in results if r.issues],
            "files": [
                {
                    "file": r.entry["file"],
                    "category": r.entry["category"],
                    "expected": {
                        k: r.entry[k] for k in ("level", "label", "tlp", "company", "pages")
                    },
                    "got": {
                        "level": r.level,
                        "label": r.label,
                        "tlp": r.tlp,
                        "company": r.company,
                        "pages": r.page_levels,
                        "unmarked_pages": r.unmarked,
                    },
                    "issues": r.issues,
                    "error": r.error,
                    "findings": r.findings,
                    "ocr_hits": r.ocr_hits,
                    "scan_ms": round(r.scan_ms, 1),
                    "inspect_ms": round(r.inspect_ms, 2),
                }
                for r in results
            ],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")
        print(f"report -> {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
