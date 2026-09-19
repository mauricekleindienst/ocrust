#!/usr/bin/env python3
"""Run ocrust across a generated corpus and measure accuracy and speed.

    python scripts/make_corpus.py --out /tmp/corpus
    python scripts/evaluate_corpus.py /tmp/corpus -o report

Every file in the corpus ships with the exact text it contains, so this reports
character and word error rates instead of "it ran". It also exercises every
public surface — all export formats, the PDF text layer, TIFF output, worker
scaling, a DPI sweep and the broken-input cases — and writes a Markdown report
plus the raw JSON next to it.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import ocrust

# ---------------------------------------------------------------- text metrics


def normalize(text: str) -> str:
    """Folds away differences that do not change what a human reads."""
    text = unicodedata.normalize("NFC", text)
    # Quotes, dashes and non-breaking spaces vary by renderer and font.
    table = {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "−": "-",
        " ": " ",
        "­": "",
        "​": "",
    }
    for source, target in table.items():
        text = text.replace(source, target)
    return " ".join(text.split())


def levenshtein(a: str, b: str) -> int:
    """Edit distance, iterative with a single row."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(
                min(
                    previous[j] + 1,  # deletion
                    current[j - 1] + 1,  # insertion
                    previous[j - 1] + (ca != cb),  # substitution
                )
            )
        previous = current
    return previous[-1]


def word_recall(expected: str, actual: str) -> float:
    """Share of ground-truth words that appear anywhere in the output.

    Reading order can legitimately differ — a drawing's title block, a
    three-column newspaper — and CER punishes that as if the text were wrong.
    Recall separates "read the characters correctly" from "ordered them
    differently".
    """
    from collections import Counter

    expected_words = Counter(normalize(expected).lower().split())
    if not expected_words:
        return 1.0
    actual_words = Counter(normalize(actual).lower().split())
    found = sum(min(count, actual_words[word]) for word, count in expected_words.items())
    return found / sum(expected_words.values())


def error_rates(expected: str, actual: str) -> tuple[float, float]:
    """Character and word error rate, both clamped to 0..1 for readability."""
    exp, act = normalize(expected), normalize(actual)
    if not exp:
        return (0.0 if not act else 1.0), (0.0 if not act else 1.0)
    cer = levenshtein(exp, act) / len(exp)
    exp_words, act_words = exp.split(), act.split()
    # Word distance over a compact alphabet: map each distinct word to one char.
    vocabulary: dict[str, str] = {}

    def encode(words: list[str]) -> str:
        out = []
        for word in words:
            if word not in vocabulary:
                vocabulary[word] = chr(0x4E00 + len(vocabulary))
            out.append(vocabulary[word])
        return "".join(out)

    wer = levenshtein(encode(exp_words), encode(act_words)) / max(1, len(exp_words))
    return min(cer, 1.0), min(wer, 1.0)


# ---------------------------------------------------------------- results


@dataclass
class FileResult:
    path: str
    category: str
    language: str
    bytes: int
    expected_pages: int
    ok: bool = False
    error: str = ""
    pages: int = 0
    lines: int = 0
    words: int = 0
    chars: int = 0
    confidence: float | None = None
    seconds: float = 0.0
    cer: float | None = None
    wer: float | None = None
    recall: float | None = None
    #: True for a script the bundled model cannot write. Still scanned and
    #: still scored, but held out of every accuracy figure below: averaging in
    #: a language ocrust does not claim would understate the ones it does.
    unsupported_script: bool = False
    text: str = ""

    @property
    def pages_per_second(self) -> float:
        return self.pages / self.seconds if self.seconds > 0 else 0.0

    @property
    def mb_per_second(self) -> float:
        return (self.bytes / 1e6) / self.seconds if self.seconds > 0 else 0.0


@dataclass
class Report:
    files: list[FileResult] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)


# ---------------------------------------------------------------- passes


def scan_pass(engine: ocrust.Ocr, root: Path, truth: dict) -> list[FileResult]:
    """Scans every file once and scores it against its ground truth."""
    results: list[FileResult] = []
    for index, (relative, entry) in enumerate(sorted(truth.items()), start=1):
        path = root / relative
        result = FileResult(
            path=relative,
            category=entry["category"],
            language=entry.get("language", "de"),
            bytes=entry["bytes"],
            expected_pages=entry.get("pages", 1),
            unsupported_script=entry.get("unsupported_script", False),
        )
        started = time.perf_counter()
        try:
            doc = engine.scan(path)
            result.seconds = time.perf_counter() - started
            result.ok = True
            result.pages = len(doc.pages)
            result.lines = len(doc.lines)
            result.words = len(doc.words)
            result.text = doc.text
            result.chars = len(normalize(doc.text))
            result.confidence = doc.confidence
            expected_text = " ".join(entry["lines"])
            if entry["lines"]:
                result.cer, result.wer = error_rates(expected_text, doc.text)
                result.recall = word_recall(expected_text, doc.text)
        except Exception as exc:  # noqa: BLE001 - the point is to record failures
            result.seconds = time.perf_counter() - started
            result.error = f"{type(exc).__name__}: {exc}"
        results.append(result)
        flag = "ok " if result.ok else "ERR"
        scores = (
            f"cer {result.cer:.3f} recall {result.recall:.3f}"
            if result.cer is not None
            else "cer   -   recall   -  "
        )
        print(
            f"[{index:3d}/{len(truth)}] {flag} {relative:<38} "
            f"{result.pages:2d}p {result.seconds * 1000:7.0f} ms {scores}",
            flush=True,
        )
    return results


def format_pass(engine: ocrust.Ocr, root: Path, truth: dict) -> dict[str, object]:
    """Renders one document into every export format."""
    sample = next(
        relative
        for relative, entry in sorted(truth.items())
        if entry["category"] == "clean" and relative.endswith(".png")
    )
    doc = engine.scan(root / sample)
    out: dict[str, object] = {"sample": sample, "formats": {}}
    for fmt in ("text", "markdown", "json", "hocr", "alto", "csv"):
        started = time.perf_counter()
        rendered = doc.render(fmt)
        out["formats"][fmt] = {
            "chars": len(rendered),
            "ms": (time.perf_counter() - started) * 1000,
            "ok": bool(rendered.strip()),
        }
    return out


def pdf_layer_pass(engine: ocrust.Ocr, root: Path, truth: dict) -> dict[str, object]:
    """Adds a text layer to every PDF in the corpus."""
    rows = []
    for relative, entry in sorted(truth.items()):
        if not relative.endswith(".pdf") or entry.get("expect_error"):
            continue
        path = root / relative
        started = time.perf_counter()
        try:
            pdf, report = engine.ocr_pdf(path)
            seconds = time.perf_counter() - started
            grew = len(pdf) - entry["bytes"]
            rows.append(
                {
                    "path": relative,
                    "category": entry["category"],
                    "ok": True,
                    "seconds": seconds,
                    "pages": report["pages"],
                    "with_layer": report["pages_with_layer"],
                    "skipped": report["pages_skipped"],
                    "lines": report["lines"],
                    "unmappable": report["unmappable_chars"],
                    "growth_bytes": grew,
                    "searchable": b"3 Tr" in pdf or report["pages_skipped"] == report["pages"],
                }
            )
        except Exception as exc:  # noqa: BLE001
            rows.append(
                {
                    "path": relative,
                    "category": entry["category"],
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "seconds": time.perf_counter() - started,
                }
            )
        print(
            f"  layer {relative:<38} {'ok' if rows[-1]['ok'] else rows[-1].get('error')}",
            flush=True,
        )
    return {"files": rows}


def tiff_pass(engine: ocrust.Ocr, root: Path, truth: dict) -> dict[str, object]:
    """Converts a selection of documents into archive TIFFs."""
    rows = []
    wanted = [
        relative
        for relative, entry in sorted(truth.items())
        if entry["category"] in {"aged-pdf", "multipage-pdf", "drawing", "fax"}
        and not entry.get("expect_error")
    ][:6]
    for relative in wanted:
        started = time.perf_counter()
        data, doc = engine.to_tiff(root / relative)
        rows.append(
            {
                "path": relative,
                "seconds": time.perf_counter() - started,
                "pages": len(doc.pages),
                "bytes": len(data),
                "gray_bytes": len(engine.to_tiff(root / relative, gray=True)[0]),
            }
        )
        print(f"  tiff  {relative:<38} {rows[-1]['pages']}p {len(data) / 1e6:.1f} MB", flush=True)
    return {"files": rows}


def worker_scaling_pass(root: Path, truth: dict, models_dir: str | None) -> dict[str, object]:
    """Measures throughput on a multi-page scan as workers are added."""
    target = next(
        (
            r
            for r, e in sorted(truth.items())
            if e["category"] == "multipage-pdf" and e["pages"] >= 12
        ),
        None,
    )
    if target is None:
        return {}
    rows = []
    for workers in (1, 2, 4, 8):
        engine = ocrust.Ocr(models_dir=models_dir, page_workers=workers)
        engine.scan(root / target)  # warm the sessions
        started = time.perf_counter()
        doc = engine.scan(root / target)
        seconds = time.perf_counter() - started
        rows.append(
            {
                "workers": workers,
                "seconds": seconds,
                "pages": len(doc.pages),
                "pages_per_second": len(doc.pages) / seconds,
            }
        )
        print(f"  workers {workers}: {seconds:.2f} s for {len(doc.pages)} pages", flush=True)
    return {"file": target, "rows": rows}


def dpi_sweep_pass(root: Path, truth: dict, models_dir: str | None) -> dict[str, object]:
    """Accuracy and cost as PDF rasterization resolution changes."""
    targets = [
        (relative, entry)
        for relative, entry in sorted(truth.items())
        if entry["category"] in {"aged-pdf", "fax-pdf", "clean-pdf"} and entry["lines"]
    ][:6]
    rows = []
    for dpi in (100, 150, 200, 300):
        engine = ocrust.Ocr(models_dir=models_dir, pdf_dpi=dpi)
        cers, recalls, seconds = [], [], 0.0
        for relative, entry in targets:
            started = time.perf_counter()
            doc = engine.scan(root / relative)
            seconds += time.perf_counter() - started
            expected = " ".join(entry["lines"])
            cer, _ = error_rates(expected, doc.text)
            cers.append(cer)
            recalls.append(word_recall(expected, doc.text))
        rows.append(
            {
                "dpi": dpi,
                "mean_cer": statistics.mean(cers),
                "median_cer": statistics.median(cers),
                "mean_recall": statistics.mean(recalls),
                "seconds_total": seconds,
                "files": len(targets),
            }
        )
        print(f"  {dpi} dpi: mean CER {rows[-1]['mean_cer']:.3f} in {seconds:.1f} s", flush=True)
    return {"files": [t[0] for t in targets], "rows": rows}


def preprocess_pass(root: Path, truth: dict, models_dir: str | None) -> dict[str, object]:
    """Does the preprocessing actually help? Measured on skewed and aged pages."""
    targets = [
        (relative, entry)
        for relative, entry in sorted(truth.items())
        if entry["category"] in {"skewed", "aged", "inverted"} and entry["lines"]
    ]
    rows = []
    for enabled in (True, False):
        engine = ocrust.Ocr(models_dir=models_dir, preprocess=enabled)
        cers, recalls, seconds = [], [], 0.0
        for relative, entry in targets:
            started = time.perf_counter()
            doc = engine.scan(root / relative)
            seconds += time.perf_counter() - started
            expected = " ".join(entry["lines"])
            cer, _ = error_rates(expected, doc.text)
            cers.append(cer)
            recalls.append(word_recall(expected, doc.text))
        rows.append(
            {
                "preprocess": enabled,
                "mean_cer": statistics.mean(cers),
                "mean_recall": statistics.mean(recalls),
                "seconds_total": seconds,
                "files": len(targets),
            }
        )
        print(
            f"  preprocess={enabled}: mean CER {rows[-1]['mean_cer']:.3f} in {seconds:.1f} s",
            flush=True,
        )
    return {"files": [t[0] for t in targets], "rows": rows}


def batch_pass(engine: ocrust.Ocr, root: Path, truth: dict) -> dict[str, object]:
    """One-by-one versus the batch API on the same set of files."""
    files = [
        root / relative
        for relative, entry in sorted(truth.items())
        if entry["category"] in {"clean", "aged", "format"} and not entry.get("expect_error")
    ][:20]
    started = time.perf_counter()
    for path in files:
        engine.scan(path)
    sequential = time.perf_counter() - started

    started = time.perf_counter()
    docs = list(engine.scan_many(files))
    batched = time.perf_counter() - started
    return {
        "files": len(files),
        "sequential_seconds": sequential,
        "batched_seconds": batched,
        "speedup": sequential / batched if batched else 0.0,
        "documents": len(docs),
    }


# ---------------------------------------------------------------- reporting


def aggregate(results: list[FileResult]) -> dict[str, dict[str, float]]:
    buckets: dict[str, list[FileResult]] = {}
    for result in results:
        buckets.setdefault(result.category, []).append(result)
    out = {}
    for category, items in sorted(buckets.items()):
        scored = [i for i in items if i.cer is not None and not i.unsupported_script]
        out[category] = {
            "files": len(items),
            "ok": sum(1 for i in items if i.ok),
            "pages": sum(i.pages for i in items),
            "mean_ms": statistics.mean([i.seconds * 1000 for i in items]),
            "mean_ms_per_page": statistics.mean(
                [i.seconds * 1000 / max(1, i.pages) for i in items if i.pages]
            )
            if any(i.pages for i in items)
            else 0.0,
            "mean_cer": statistics.mean([i.cer for i in scored]) if scored else None,
            "median_cer": statistics.median([i.cer for i in scored]) if scored else None,
            "mean_wer": statistics.mean([i.wer for i in scored]) if scored else None,
            "mean_recall": statistics.mean([i.recall for i in scored]) if scored else None,
            "mean_confidence": statistics.mean(
                [i.confidence for i in items if i.confidence is not None]
            )
            if any(i.confidence is not None for i in items)
            else None,
        }
    return out


def markdown(report: Report, truth: dict, meta: dict) -> str:
    results = report.files
    scored = [r for r in results if r.cer is not None and not r.unsupported_script]
    held_out = [r for r in results if r.cer is not None and r.unsupported_script]
    total_pages = sum(r.pages for r in results)
    total_seconds = sum(r.seconds for r in results)
    expected_errors = {r for r, e in truth.items() if e.get("expect_error")}

    lines: list[str] = []
    add = lines.append
    add("# Corpus evaluation")
    add("")
    add(
        f"- corpus: **{len(results)} files**, {total_pages} recognized pages, "
        f"{sum(r.bytes for r in results) / 1e6:.0f} MB"
    )
    add(
        f"- models: `{meta['models']['recognition'].rsplit('/', 1)[-1]}`, "
        f"{meta['charset']} characters, {meta['languages']} languages"
    )
    add(f"- runtime: {meta['onnxruntime']} · {meta['cpu_count']} CPU cores")
    add(
        f"- total scan time: **{total_seconds:.1f} s** "
        f"({total_seconds / max(1, total_pages) * 1000:.0f} ms per page)"
    )
    if scored:
        add(
            f"- accuracy over {len(scored)} files with ground truth: "
            f"**mean CER {statistics.mean([r.cer for r in scored]):.3f}**, "
            f"median {statistics.median([r.cer for r in scored]):.3f}, "
            f"mean WER {statistics.mean([r.wer for r in scored]):.3f}, "
            f"**mean word recall {statistics.mean([r.recall for r in scored]):.3f}**"
        )
    if held_out:
        add(
            f"- held out of those figures: {len(held_out)} file(s) in a script the "
            f"bundled model cannot write "
            f"(mean CER {statistics.mean([r.cer for r in held_out]):.3f}) — see below"
        )
    failures = [r for r in results if not r.ok]
    unexpected = [r for r in failures if r.path not in expected_errors]
    missed = [r for r in results if r.ok and r.path in expected_errors]
    add(
        f"- failures: {len(failures)} total, "
        f"**{len(unexpected)} unexpected**, {len(missed)} broken file(s) that did not error"
    )
    add("")

    if held_out:
        add("## Scripts the bundled model cannot write")
        add("")
        add(
            "Scanned and scored, but excluded from every figure above: `ocrust"
            " languages` does not offer these, and asking for one fails with the"
            " characters it cannot emit. They are here so the cost of that gap is"
            " on the record rather than folded into an average."
        )
        add("")
        add("| file | language | CER | WER | word recall | confidence |")
        add("|---|---|---:|---:|---:|---:|")
        for r in sorted(held_out, key=lambda r: r.path):
            conf = f"{r.confidence:.3f}" if r.confidence is not None else "—"
            add(
                f"| `{r.path}` | {r.language} | {r.cer:.3f} | {r.wer:.3f} "
                f"| {r.recall:.3f} | {conf} |"
            )
        add("")

    add("## By category")
    add("")
    add(
        "| category | files | ok | pages | ms/page | mean CER | median CER"
        " | mean WER | word recall | confidence |"
    )
    add("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for category, stats in aggregate(results).items():
        cer = f"{stats['mean_cer']:.3f}" if stats["mean_cer"] is not None else "—"
        med = f"{stats['median_cer']:.3f}" if stats["median_cer"] is not None else "—"
        wer = f"{stats['mean_wer']:.3f}" if stats["mean_wer"] is not None else "—"
        rec = f"{stats['mean_recall']:.3f}" if stats.get("mean_recall") is not None else "—"
        conf = f"{stats['mean_confidence']:.3f}" if stats["mean_confidence"] is not None else "—"
        add(
            f"| {category} | {stats['files']} | {stats['ok']} | {stats['pages']} | "
            f"{stats['mean_ms_per_page']:.0f} | {cer} | {med} | {wer} | {rec} | {conf} |"
        )
    add("")

    add("## By language")
    add("")
    add("| language | files | mean CER | mean WER | word recall | confidence |")
    add("|---|---:|---:|---:|---:|---:|")
    by_language: dict[str, list[FileResult]] = {}
    for result in scored:
        by_language.setdefault(result.language, []).append(result)
    for language, items in sorted(by_language.items()):
        confidences = [i.confidence for i in items if i.confidence is not None]
        conf = f"{statistics.mean(confidences):.3f}" if confidences else "—"
        add(
            f"| {language} | {len(items)} | "
            f"{statistics.mean([i.cer for i in items]):.3f} | "
            f"{statistics.mean([i.wer for i in items]):.3f} | "
            f"{statistics.mean([i.recall for i in items]):.3f} | {conf} |"
        )
    add("")

    add("## Hardest files")
    add("")
    add("| file | category | CER | WER | word recall | confidence | ms |")
    add("|---|---|---:|---:|---:|---:|---:|")
    for result in sorted(scored, key=lambda r: -r.cer)[:12]:
        conf = f"{result.confidence:.3f}" if result.confidence is not None else "—"
        add(
            f"| `{result.path}` | {result.category} | {result.cer:.3f} | {result.wer:.3f} | "
            f"{result.recall:.3f} | {conf} | {result.seconds * 1000:.0f} |"
        )
    add("")

    add("## Slowest files")
    add("")
    add("| file | bytes | pages | seconds | pages/s | MB/s |")
    add("|---|---:|---:|---:|---:|---:|")
    for result in sorted(results, key=lambda r: -r.seconds)[:10]:
        add(
            f"| `{result.path}` | {result.bytes / 1e6:.1f} MB | {result.pages} | "
            f"{result.seconds:.2f} | {result.pages_per_second:.2f} | {result.mb_per_second:.1f} |"
        )
    add("")

    if failures:
        add("## Failures")
        add("")
        add("| file | expected? | error |")
        add("|---|---|---|")
        for result in failures:
            expected = "yes" if result.path in expected_errors else "**no**"
            add(f"| `{result.path}` | {expected} | {result.error} |")
        add("")
    if missed:
        add(
            "Broken inputs that were read without error (content sniffing may have recovered "
            "them, which is fine as long as the text is right):"
        )
        add("")
        for result in missed:
            add(f"- `{result.path}`: {result.lines} line(s) recognized")
        add("")

    extra = report.extra
    if "formats" in extra:
        add("## Export formats")
        add("")
        add(f"Rendered from `{extra['formats']['sample']}`:")
        add("")
        add("| format | characters | ms |")
        add("|---|---:|---:|")
        for name, info in extra["formats"]["formats"].items():
            add(f"| {name} | {info['chars']} | {info['ms']:.1f} |")
        add("")

    if "pdf_layer" in extra:
        rows = extra["pdf_layer"]["files"]
        ok_rows = [r for r in rows if r["ok"]]
        add("## PDF text layer")
        add("")
        add(f"- {len(rows)} PDFs processed, {len(ok_rows)} succeeded")
        if ok_rows:
            layered = sum(r["with_layer"] for r in ok_rows)
            skipped = sum(r["skipped"] for r in ok_rows)
            add(f"- {layered} page(s) got a layer, {skipped} skipped because they already had text")
            add(
                f"- mean time {statistics.mean([r['seconds'] for r in ok_rows]):.2f} s per document"
            )
            growth = statistics.mean([r["growth_bytes"] for r in ok_rows]) / 1024
            add(f"- mean growth {growth:.1f} KB")
            unmappable = sum(r["unmappable"] for r in ok_rows)
            add(f"- {unmappable} character(s) outside WinAnsi in the text layer")
            broken = [r for r in ok_rows if not r["searchable"]]
            add(f"- {len(broken)} document(s) without a detectable text layer")
        bad = [r for r in rows if not r["ok"]]
        if bad:
            add("")
            add("| file | error |")
            add("|---|---|")
            for row in bad:
                add(f"| `{row['path']}` | {row['error']} |")
        add("")

    if "tiff" in extra:
        add("## Archive TIFF")
        add("")
        add("| file | pages | colour | greyscale | seconds |")
        add("|---|---:|---:|---:|---:|")
        for row in extra["tiff"]["files"]:
            add(
                f"| `{row['path']}` | {row['pages']} | {row['bytes'] / 1e6:.1f} MB | "
                f"{row['gray_bytes'] / 1e6:.1f} MB | {row['seconds']:.2f} |"
            )
        add("")

    if extra.get("workers"):
        add("## Worker scaling")
        add("")
        add(f"On `{extra['workers']['file']}`:")
        add("")
        add("| workers | seconds | pages/s | speedup |")
        add("|---:|---:|---:|---:|")
        base = extra["workers"]["rows"][0]["seconds"]
        for row in extra["workers"]["rows"]:
            add(
                f"| {row['workers']} | {row['seconds']:.2f} | {row['pages_per_second']:.2f} | "
                f"{base / row['seconds']:.2f}x |"
            )
        add("")

    if extra.get("dpi"):
        add("## DPI sweep")
        add("")
        add(f"Over {len(extra['dpi']['files'])} PDFs (clean, aged and fax):")
        add("")
        add("| dpi | mean CER | median CER | word recall | seconds |")
        add("|---:|---:|---:|---:|---:|")
        for row in extra["dpi"]["rows"]:
            add(
                f"| {row['dpi']} | {row['mean_cer']:.3f} | {row['median_cer']:.3f} | "
                f"{row['mean_recall']:.3f} | {row['seconds_total']:.1f} |"
            )
        add("")

    if extra.get("preprocess"):
        add("## Does preprocessing pay off?")
        add("")
        add(f"Over {len(extra['preprocess']['files'])} skewed, aged and inverted pages:")
        add("")
        add("| preprocessing | mean CER | word recall | seconds |")
        add("|---|---:|---:|---:|")
        for row in extra["preprocess"]["rows"]:
            add(
                f"| {'on' if row['preprocess'] else 'off'} | {row['mean_cer']:.3f} | "
                f"{row['mean_recall']:.3f} | {row['seconds_total']:.1f} |"
            )
        add("")

    if extra.get("batch"):
        batch = extra["batch"]
        add("## Batch API")
        add("")
        add(
            f"{batch['files']} files: {batch['sequential_seconds']:.1f} s one by one versus "
            f"{batch['batched_seconds']:.1f} s with `scan_many` "
            f"(**{batch['speedup']:.2f}x**)."
        )
        add("")

    add("## Sample output")
    add("")
    for category in ("clean", "aged", "fax", "drawing", "receipt"):
        sample = next((r for r in results if r.category == category and r.text), None)
        if sample is None:
            continue
        preview = " | ".join(sample.text.split("\n")[:6])[:300]
        add(
            f"**{category}** (`{sample.path}`, CER {sample.cer:.3f}):"
            if sample.cer is not None
            else f"**{category}** (`{sample.path}`):"
        )
        add("")
        add(f"> {preview}")
        add("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=Path("corpus_report"))
    parser.add_argument("--models", help="model directory (defaults to the installed bundle)")
    parser.add_argument("--quick", action="store_true", help="skip the sweeps")
    args = parser.parse_args()

    truth_path = args.corpus / "ground_truth.json"
    if not truth_path.exists():
        raise SystemExit(f"no ground_truth.json in {args.corpus}; run make_corpus.py first")
    truth = json.loads(truth_path.read_text(encoding="utf-8"))

    engine = ocrust.Ocr(models_dir=args.models)
    meta = {
        "models": engine.models,
        "charset": engine.charset_size,
        "languages": len(engine.languages),
        "onnxruntime": str(ocrust.runtime_info().get("onnxruntime_version")),
        "cpu_count": __import__("os").cpu_count(),
        "ocrust": ocrust.__version__,
    }
    print(f"ocrust {meta['ocrust']}, {meta['charset']} characters, {meta['languages']} languages\n")

    report = Report()
    print("== scanning every file ==")
    report.files = scan_pass(engine, args.corpus, truth)

    print("\n== export formats ==")
    report.extra["formats"] = format_pass(engine, args.corpus, truth)

    print("\n== PDF text layer ==")
    report.extra["pdf_layer"] = pdf_layer_pass(engine, args.corpus, truth)

    print("\n== archive TIFF ==")
    report.extra["tiff"] = tiff_pass(engine, args.corpus, truth)

    print("\n== batch API ==")
    report.extra["batch"] = batch_pass(engine, args.corpus, truth)

    if not args.quick:
        print("\n== worker scaling ==")
        report.extra["workers"] = worker_scaling_pass(args.corpus, truth, args.models)
        print("\n== dpi sweep ==")
        report.extra["dpi"] = dpi_sweep_pass(args.corpus, truth, args.models)
        print("\n== preprocessing on/off ==")
        report.extra["preprocess"] = preprocess_pass(args.corpus, truth, args.models)

    text = markdown(report, truth, meta)
    md_path = args.out.with_suffix(".md")
    json_path = args.out.with_suffix(".json")
    md_path.write_text(text, encoding="utf-8")
    json_path.write_text(
        json.dumps(
            {
                "meta": meta,
                "files": [vars(result) for result in report.files],
                "extra": report.extra,
            },
            indent=1,
            ensure_ascii=False,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nreport -> {md_path}\nraw    -> {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
