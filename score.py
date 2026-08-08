#!/usr/bin/env python3
"""Grade alignment output against the fixture ground truth.

Reads ground_truth.json plus one EDL per edit and reports how well the mapping
was recovered. Emits metrics.json and exits non-zero if any gate fails, so it
drops straight into a build loop.

    python3 score.py fixtures/ground_truth.json --edl-dir out/edls

Expected EDL shape (one file per edit, named <edit_stem>.json):

    {
      "edit": "edits/edit_00.mkv",
      "duration": 21.34,
      "sources": ["sources/src_00.mkv", "sources/src_01.mkv"],
      "segments": [
        {"t0": 0.0, "t1": 9.21, "src": 0, "src_t0": 12.50,
         "delta": 12.50, "conf": 0.94, "status": "ok"}
      ]
    }

`src` indexes into `sources`. A segment with `src: null` is an unmatched gap.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

GRID_HZ = 200.0          # sampling rate for timeline comparisons
CUT_TOLERANCE = 0.150    # seconds; how far a predicted cut may sit from truth

GATES = {
    "coverage_pct": ("min", 99.0),
    "source_accuracy_pct": ("min", 99.0),
    "offset_median_ms": ("max", 5.0),
    "offset_p95_ms": ("max", 20.0),
    "cut_median_ms": ("max", 42.0),      # one frame at 23.976 fps
    "cut_p95_ms": ("max", 84.0),
    "missed_cuts": ("max", 0),
    "spurious_cuts": ("max", 0),
}


def resolve_src(edl: dict, seg: dict, root: Path) -> str | None:
    if seg.get("src") is None:
        return None
    sources = edl.get("sources") or []
    try:
        return Path(sources[int(seg["src"])]).name
    except (IndexError, ValueError, TypeError):
        return None


def timeline(segments, duration, grid, getter):
    """Sample a per-segment property onto a fixed grid over the edit timeline."""
    out = np.full(grid.size, np.nan)
    labels: list = [None] * grid.size
    for seg in segments:
        lo = np.searchsorted(grid, seg["t0"], "left")
        hi = np.searchsorted(grid, seg["t1"], "right")
        if hi <= lo:
            continue
        name, value = getter(seg)
        for i in range(lo, min(hi, grid.size)):
            labels[i] = name
            out[i] = value + (grid[i] - seg["t0"]) if value is not None else np.nan
    return labels, out


def boundaries(segments, duration) -> list[float]:
    edges = sorted({round(s["t1"], 6) for s in segments})
    return [e for e in edges if 0.05 < e < duration - 0.05]


def match_cuts(pred: list[float], truth: list[float]):
    """Greedy nearest matching within tolerance. Returns (errors, missed, spurious)."""
    remaining = list(pred)
    errors, missed = [], 0
    for t in truth:
        if not remaining:
            missed += 1
            continue
        i = int(np.argmin([abs(p - t) for p in remaining]))
        if abs(remaining[i] - t) <= CUT_TOLERANCE:
            errors.append(abs(remaining.pop(i) - t))
        else:
            missed += 1
    return errors, missed, len(remaining)


def audio_agreement(rendered: Path, expected: Path) -> float | None:
    """Fraction of the timeline where the rendered dub matches the expected one."""
    if not rendered.exists() or not expected.exists():
        return None

    def decode(p):
        raw = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(p), "-ac", "1", "-ar", "16000",
             "-f", "f32le", "-"], capture_output=True).stdout
        return np.frombuffer(raw, dtype="<f4")

    a, b = decode(rendered), decode(expected)
    n = min(a.size, b.size)
    if n < 16000:
        return None
    a, b = a[:n], b[:n]
    win = 16000
    good = total = 0
    for i in range(0, n - win, win):
        x, y = a[i:i + win], b[i:i + win]
        xs, ys = x - x.mean(), y - y.mean()
        denom = np.sqrt((xs @ xs) * (ys @ ys))
        if denom < 1e-9:
            continue
        total += 1
        if (xs @ ys) / denom > 0.9:
            good += 1
    return 100.0 * good / total if total else None


def score_edit(truth: dict, edl: dict, root: Path) -> dict:
    duration = truth["duration"]
    grid = np.arange(0, duration, 1.0 / GRID_HZ)

    t_labels, t_times = timeline(
        truth["segments"], duration, grid,
        lambda s: (Path(s["src_file"]).name, s["src_t0"]))
    p_labels, p_times = timeline(
        edl.get("segments", []), duration, grid,
        lambda s: (resolve_src(edl, s, root), s.get("src_t0")))

    covered = np.array([lbl is not None for lbl in p_labels])
    correct = np.array([p == t for p, t in zip(p_labels, t_labels)]) & covered

    if correct.any():
        err_ms = np.abs(p_times[correct] - t_times[correct]) * 1000.0
        err_ms = err_ms[np.isfinite(err_ms)]
    else:
        err_ms = np.array([np.inf])

    cut_err, missed, spurious = match_cuts(
        boundaries(edl.get("segments", []), duration),
        boundaries(truth["segments"], duration))
    cut_ms = np.array(cut_err) * 1000.0 if cut_err else np.array([np.inf])

    rendered = root / f"{Path(truth['edit']).stem}.dub.wav"
    agreement = audio_agreement(rendered, root / truth["expected_dub"])

    return {
        "edit": truth["edit"],
        "coverage_pct": round(100.0 * covered.mean(), 2),
        "source_accuracy_pct": round(100.0 * correct.mean(), 2),
        "offset_median_ms": round(float(np.median(err_ms)), 2),
        "offset_p95_ms": round(float(np.percentile(err_ms, 95)), 2),
        "offset_max_ms": round(float(err_ms.max()), 2),
        "cut_median_ms": round(float(np.median(cut_ms)), 2),
        "cut_p95_ms": round(float(np.percentile(cut_ms, 95)), 2),
        "missed_cuts": missed,
        "spurious_cuts": spurious,
        "segments_predicted": len(edl.get("segments", [])),
        "segments_truth": len(truth["segments"]),
        "audio_agreement_pct": agreement,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ground_truth")
    ap.add_argument("--edl-dir", required=True)
    ap.add_argument("--out", default="metrics.json")
    args = ap.parse_args()

    gt_path = Path(args.ground_truth)
    truth = json.loads(gt_path.read_text())
    root = gt_path.parent
    edl_dir = Path(args.edl_dir)

    results, failures = [], []
    for entry in truth["edits"]:
        stem = Path(entry["edit"]).stem
        edl_path = edl_dir / f"{stem}.json"
        if not edl_path.exists():
            failures.append(f"{stem}: no EDL at {edl_path}")
            results.append({"edit": entry["edit"], "error": "missing EDL"})
            continue
        results.append(score_edit(entry, json.loads(edl_path.read_text()), root))

    print(f"{'edit':<22} {'cover':>6} {'src':>6} {'off50':>7} {'off95':>7} "
          f"{'cut50':>7} {'miss':>5} {'spur':>5} {'audio':>6}")
    print("-" * 80)
    for r in results:
        if "error" in r:
            print(f"{Path(r['edit']).name:<22} {r['error']}")
            continue
        audio = "—" if r["audio_agreement_pct"] is None else f"{r['audio_agreement_pct']:.0f}%"
        print(f"{Path(r['edit']).name:<22} {r['coverage_pct']:>5.1f}% "
              f"{r['source_accuracy_pct']:>5.1f}% {r['offset_median_ms']:>7.1f} "
              f"{r['offset_p95_ms']:>7.1f} {r['cut_median_ms']:>7.1f} "
              f"{r['missed_cuts']:>5} {r['spurious_cuts']:>5} {audio:>6}")

        for key, (mode, limit) in GATES.items():
            value = r.get(key)
            if value is None:
                continue
            if (mode == "min" and value < limit) or (mode == "max" and value > limit):
                failures.append(
                    f"{Path(r['edit']).name}: {key}={value} fails {mode} {limit}")

    Path(args.out).write_text(json.dumps(
        {"results": results, "failures": failures, "gates": GATES}, indent=2))

    print()
    if failures:
        for f in failures:
            print("FAIL:", f)
        print(f"\n{len(failures)} gate(s) failed — see {args.out}")
        return 1
    print(f"All gates passed — see {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
