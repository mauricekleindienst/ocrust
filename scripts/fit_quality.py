#!/usr/bin/env python3
"""Fits the page-quality estimate and reports whether it generalizes.

    python scripts/collect_quality.py /tmp/corpus -o lines.json
    python scripts/fit_quality.py lines.json corpus_report.json

`Page.quality` answers a question `Page.confidence` structurally cannot. The
recognizer's confidence is its certainty about the characters it emitted, and it
is good at that — over this corpus a line's mean character probability lands
within 1.4% of the share of that line's text that is actually right. But a page's
error rate is dominated by what recognition never saw: text the detector missed,
a column read out of order, a label broken into fragments. So confidence ranks
pages poorly, while the *shape* of the output ranks them well.

The weights printed here go into `crates/ocrust-core/src/quality.rs`. Re-run this
when the models, the detector or the layout change, and paste the new numbers in
only if the held-out columns still justify them.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path

#: Each feature, scaled exactly as `quality.rs` scales it.
FEATURES: dict[str, object] = {
    "chars per line": lambda ls: min(statistics.fmean(row["text_len"] for row in ls), 60) / 30.0,
    "weak confidence": lambda ls: (
        _logit(sorted(row["raw_confidence"] for row in ls)[max(0, len(ls) // 10)]) / 5.0
    ),
    "line height": lambda ls: min(
        statistics.fmean(row["height"] / max(row["page_height"], 1) for row in ls) * 40, 3.0
    ),
    "mean confidence": lambda ls: (
        _logit(statistics.fmean(row["raw_confidence"] for row in ls)) / 5.0
    ),
    "weak margin": lambda ls: min(row["margin"] for row in ls),
}


def _logit(p: float, lo: float = 1e-3) -> float:
    p = min(max(p, lo), 1 - lo)
    return math.log(p / (1 - p))


def design(by_file: dict, files: list[str], keys: list[str]) -> list[list[float]]:
    return [[1.0] + [FEATURES[k](by_file[f]) for k in keys] for f in files]


def fit(
    x: list[list[float]], y: list[float], steps: int = 6000, rate: float = 0.5, l2: float = 2e-3
) -> list[float]:
    """Logistic regression by gradient descent — five features, no dependencies."""
    w = [0.0] * len(x[0])
    w[0] = _logit(sum(y) / len(y))
    n = len(x)
    for _ in range(steps):
        grad = [0.0] * len(w)
        for row, target in zip(x, y):
            z = sum(a * b for a, b in zip(w, row))
            p = 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))
            for i, xi in enumerate(row):
                grad[i] += (p - target) * xi
        for i in range(len(w)):
            w[i] -= rate * (grad[i] / n + (l2 * w[i] if i else 0.0))
    return w


def predict(w: list[float], row: list[float]) -> float:
    z = sum(a * b for a, b in zip(w, row))
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, z))))


def spearman(a: list[float], b: list[float]) -> float:
    def ranks(v: list[float]) -> list[float]:
        order = sorted(range(len(v)), key=lambda i: v[i])
        out = [0.0] * len(v)
        for rank, i in enumerate(order):
            out[i] = rank
        return out

    ra, rb = ranks(a), ranks(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    sd = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return cov / sd if sd else 0.0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("lines", type=Path, help="output of collect_quality.py")
    parser.add_argument("report", type=Path, help="corpus report JSON, for the truth")
    parser.add_argument("--splits", type=int, default=20)
    args = parser.parse_args()

    rows = json.loads(args.lines.read_text())
    report = json.loads(args.report.read_text())
    cer = {f["path"]: f["cer"] for f in report["files"] if f.get("cer") is not None}

    by_file: dict[str, list[dict]] = {}
    for row in rows:
        by_file.setdefault(row["file"], []).append(row)
    files = [f for f in by_file if f in cer]
    truth = {f: 1.0 - min(cer[f], 1.0) for f in files}
    keys = list(FEATURES)
    print(f"{len(rows)} lines over {len(files)} files with ground truth\n")

    base_rho, model_rho, base_mae, model_mae, gaps = [], [], [], [], []
    for seed in range(args.splits):
        shuffled = list(files)
        random.Random(seed).shuffle(shuffled)
        cut = len(shuffled) * 2 // 3
        train, test = shuffled[:cut], shuffled[cut:]
        want = [truth[f] for f in test]
        base = [statistics.fmean(r["raw_confidence"] for r in by_file[f]) for f in test]
        w = fit(design(by_file, train, keys), [truth[f] for f in train])
        pred = [predict(w, r) for r in design(by_file, test, keys)]
        base_rho.append(spearman(base, want))
        model_rho.append(spearman(pred, want))
        base_mae.append(sum(abs(p - t) for p, t in zip(base, want)) / len(test))
        model_mae.append(sum(abs(p - t) for p, t in zip(pred, want)) / len(test))
        order = sorted(range(len(test)), key=lambda i: want[i])
        q = max(1, len(test) // 4)
        gaps.append(
            (
                statistics.fmean(pred[i] for i in order[-q:])
                - statistics.fmean(pred[i] for i in order[:q]),
                statistics.fmean(base[i] for i in order[-q:])
                - statistics.fmean(base[i] for i in order[:q]),
            )
        )

    print(f"{args.splits} random two-thirds/one-third splits, held-out columns\n")
    print(f"{'':<22}{'Spearman':>11}{'MAE':>10}{'best-worst gap':>17}")
    print("-" * 60)
    print(
        f"{'mean confidence':<22}{statistics.fmean(base_rho):>+11.3f}"
        f"{statistics.fmean(base_mae):>10.4f}{statistics.fmean(g[1] for g in gaps):>+17.4f}"
    )
    print(
        f"{'fitted quality':<22}{statistics.fmean(model_rho):>+11.3f}"
        f"{statistics.fmean(model_mae):>10.4f}{statistics.fmean(g[0] for g in gaps):>+17.4f}"
    )
    wins = sum(1 for a, b in zip(model_rho, base_rho) if a > b)
    print(f"\nthe fit ranks better on {wins} of {args.splits} splits")

    final = fit(design(by_file, files, keys), [truth[f] for f in files])
    print("\nweights, refitted on every file — paste into crates/ocrust-core/src/quality.rs:")
    print("const WEIGHTS: [f32; 6] = [")
    for label, value in zip(["bias", *keys], final):
        print(f"    {value:+.5f}, // {label}")
    print("];")


if __name__ == "__main__":
    main()
