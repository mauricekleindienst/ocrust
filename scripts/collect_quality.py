#!/usr/bin/env python3
"""Pairs every recognized line with how much of it is actually right.

    python scripts/make_corpus.py --out /tmp/corpus
    python scripts/collect_quality.py /tmp/corpus -o lines.json
    python scripts/fit_quality.py lines.json corpus_report.json

The target is *precision*: how much of what the engine printed appears in the
ground truth. A line that was split in two is not punished for being short, and
a line invented out of noise scores near zero — which is what a reader means by
asking whether a line can be trusted.

Ground-truth lines are matched singly and in runs of two and three, because the
layout joins boxes that share a baseline into one line.
"""

from __future__ import annotations

import argparse
import difflib
import json
from pathlib import Path

import ocrust


def candidates(truth_lines: list[str]) -> list[str]:
    """Every ground-truth line, and every run of two or three neighbours."""
    runs = list(truth_lines)
    runs += [f"{a} {b}" for a, b in zip(truth_lines, truth_lines[1:])]
    runs += [f"{a} {b} {c}" for a, b, c in zip(truth_lines, truth_lines[1:], truth_lines[2:])]
    return runs


def precision(text: str, against: list[str]) -> float:
    """The best share of `text` that any candidate accounts for."""
    best = 0.0
    for candidate in against:
        matcher = difflib.SequenceMatcher(None, text, candidate, autojunk=False)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        best = max(best, matched / len(text))
        if best > 0.999:
            break
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("corpus", type=Path)
    parser.add_argument("-o", "--out", type=Path, default=Path("lines.json"))
    parser.add_argument("--models", type=Path)
    args = parser.parse_args()

    truth_path = args.corpus / "ground_truth.json"
    if not truth_path.exists():
        raise SystemExit(f"no ground_truth.json in {args.corpus}; run make_corpus.py first")
    truth = json.loads(truth_path.read_text())

    ocr = ocrust.Ocr(models_dir=args.models)
    rows: list[dict] = []
    for name, entry in sorted(truth.items()):
        if entry.get("expect_error") in (True, "true"):
            continue
        lines = entry.get("lines")
        if isinstance(lines, str):
            lines = json.loads(lines)
        path = args.corpus / name
        if not lines or not path.exists():
            continue
        try:
            doc = ocr.scan(path)
        except Exception as exc:  # noqa: BLE001 - a broken fixture is not the subject
            print(f"skip {name}: {exc}")
            continue

        against = candidates(lines)
        raw = doc.to_dict()
        for page in raw["pages"]:
            for block in page["blocks"]:
                for line in block["lines"]:
                    text = line["text"].strip()
                    if not text:
                        continue
                    rows.append(
                        {
                            "file": name,
                            "category": entry.get("category", "?"),
                            "accuracy": round(precision(text, against), 5),
                            "raw_confidence": line["confidence"],
                            "margin": line.get("margin", 0.0),
                            "text_len": len(text),
                            "det_score": line["det_score"],
                            "height": line["bbox"]["y1"] - line["bbox"]["y0"],
                            "page_height": page["height"],
                        }
                    )

    args.out.write_text(json.dumps(rows))
    print(f"{len(rows)} lines from {len({r['file'] for r in rows})} files -> {args.out}")


if __name__ == "__main__":
    main()
