#!/usr/bin/env python3
"""Build synthetic media fixtures with known ground truth.

Produces fake "original episodes" (dual audio, jpn + eng) and fake "edits" cut
from them (Japanese only), plus the exact cut list that produced each edit.
Everything is small and fast, so the alignment pipeline can be exercised end to
end in seconds without touching real media.

The fake English track shares a music-and-effects bed with the Japanese track
and differs only in its "dialogue" layer, which mirrors how a real dub is built
and lets you test the harder dub-to-sub matching mode later.

    python3 make_fixtures.py out/fixtures --sources 6 --edits 3

Writes:
    sources/src_00.mkv ...     dual audio, 23.976 fps
    edits/edit_00.mkv ...      Japanese only, re-encoded to look like a rip
    edits/edit_00.expected.wav the dub track a perfect run should produce
    ground_truth.json          cut list for every edit
"""
from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np

SR = 48000
FPS = "24000/1001"


def _run(cmd: list[str]) -> None:
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        tail = proc.stderr.decode("utf-8", "replace").strip().splitlines()[-8:]
        raise RuntimeError("ffmpeg failed:\n" + "\n".join(tail))


def _layer(rng, seconds: float, events_per_sec: float,
           f_lo: float, f_hi: float, gain: float) -> np.ndarray:
    """Dense sweeping tones — spectrally rich enough to fingerprint like speech."""
    n = int(seconds * SR)
    out = np.zeros(n, dtype=np.float32)
    t = np.arange(n) / SR
    for _ in range(int(seconds * events_per_sec)):
        start = rng.uniform(0, max(0.01, seconds - 0.7))
        dur = rng.uniform(0.12, 0.5)
        f0 = rng.uniform(f_lo, f_hi)
        f1 = f0 * rng.uniform(0.55, 1.8)
        a, b = int(start * SR), min(n, int((start + dur) * SR))
        if b - a < 32:
            continue
        seg = t[a:b] - t[a]
        wave = np.sin(2 * np.pi * (f0 * seg + (f1 - f0) / (2 * dur) * seg ** 2))
        out[a:b] += (wave * np.hanning(b - a) * rng.uniform(0.4, 1.0) * gain)
    return out


def make_source_audio(rng, seconds: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (japanese, english). Shared bed, different dialogue layers."""
    bed = _layer(rng, seconds, 7, 90, 1400, 0.32)
    bed += rng.normal(0, 0.01, bed.size).astype(np.float32)
    jp = bed + _layer(rng, seconds, 5, 300, 3800, 0.45)
    en = bed + _layer(rng, seconds, 5, 260, 3600, 0.45)
    peak = max(np.abs(jp).max(), np.abs(en).max(), 1e-6)
    return (jp / peak * 0.8).astype(np.float32), (en / peak * 0.8).astype(np.float32)


def write_raw(path: Path, data: np.ndarray) -> None:
    path.write_bytes(np.ascontiguousarray(data, dtype=np.float32).tobytes())


def build_source(out: Path, jp: np.ndarray, en: np.ndarray, seconds: float,
                 tmp: Path, index: int) -> None:
    jp_raw, en_raw = tmp / f"jp{index}.raw", tmp / f"en{index}.raw"
    write_raw(jp_raw, jp)
    write_raw(en_raw, en)
    _run([
        "ffmpeg", "-hide_banner", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={FPS}:duration={seconds}",
        "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(jp_raw),
        "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(en_raw),
        "-map", "0:v", "-map", "1:a", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-metadata:s:a:0", "language=jpn", "-metadata:s:a:0", "title=Japanese",
        "-metadata:s:a:1", "language=eng", "-metadata:s:a:1", "title=English Dub",
        str(out),
    ])


def build_edit(out: Path, sources: list[Path], cuts: list[dict],
               jp_audio: np.ndarray, tmp: Path, index: int) -> None:
    """Concatenate the chosen video segments and attach the degraded JP audio."""
    used = sorted({c["src"] for c in cuts})
    slot = {src: i for i, src in enumerate(used)}

    parts, filters = [], []
    for i, c in enumerate(cuts):
        s = slot[c["src"]]
        filters.append(
            f"[{s}:v]trim=start={c['src_t0']:.6f}:end={c['src_t1']:.6f},"
            f"setpts=PTS-STARTPTS[v{i}]"
        )
        parts.append(f"[v{i}]")
    filters.append("".join(parts) + f"concat=n={len(cuts)}:v=1:a=0[v]")

    raw = tmp / f"edit{index}.raw"
    write_raw(raw, jp_audio)

    cmd = ["ffmpeg", "-hide_banner", "-v", "error", "-y"]
    for src in used:
        cmd += ["-i", str(sources[src])]
    cmd += ["-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(raw)]
    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "[v]", "-map", f"{len(used)}:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k",
        "-metadata:s:a:0", "language=jpn",
        str(out),
    ]
    _run(cmd)



def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--sources", type=int, default=6)
    ap.add_argument("--edits", type=int, default=3)
    ap.add_argument("--source-secs", type=float, default=75.0)
    ap.add_argument("--cuts-per-edit", type=int, default=6)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    root = Path(args.out)
    (root / "sources").mkdir(parents=True, exist_ok=True)
    (root / "edits").mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        print(f"building {args.sources} source episodes…")
        jp_tracks, en_tracks, source_paths = [], [], []
        for i in range(args.sources):
            jp, en = make_source_audio(rng, args.source_secs)
            path = root / "sources" / f"src_{i:02d}.mkv"
            build_source(path, jp, en, args.source_secs, tmp, i)
            jp_tracks.append(jp)
            en_tracks.append(en)
            source_paths.append(path)

        truth = {"sample_rate": SR, "fps": 24000 / 1001, "edits": []}

        print(f"building {args.edits} edits…")
        for e in range(args.edits):
            cuts, jp_parts, en_parts, cursor = [], [], [], 0.0
            for _ in range(args.cuts_per_edit):
                src = int(rng.integers(0, args.sources))
                dur = float(rng.uniform(3.5, 14.0))
                start = float(rng.uniform(0.5, args.source_secs - dur - 0.5))
                a, b = int(start * SR), int((start + dur) * SR)
                jp_parts.append(jp_tracks[src][a:b])
                en_parts.append(en_tracks[src][a:b])
                cuts.append({
                    "src": src,
                    "src_file": f"sources/src_{src:02d}.mkv",
                    "t0": cursor,
                    "t1": cursor + (b - a) / SR,
                    "src_t0": start,
                    "src_t1": start + (b - a) / SR,
                    "delta": start - cursor,
                })
                cursor += (b - a) / SR

            jp_edit = np.concatenate(jp_parts)
            # stand in for a different rip: gain change plus encoder-ish noise
            jp_edit = (jp_edit * 0.74
                       + rng.normal(0, 0.004, jp_edit.size)).astype(np.float32)

            edit_path = root / "edits" / f"edit_{e:02d}.mkv"
            build_edit(edit_path, source_paths, cuts, jp_edit, tmp, e)

            expected = root / "edits" / f"edit_{e:02d}.expected.wav"
            raw = tmp / f"exp{e}.raw"
            write_raw(raw, np.concatenate(en_parts))
            _run([
                "ffmpeg", "-hide_banner", "-v", "error", "-y",
                "-f", "f32le", "-ar", str(SR), "-ac", "1", "-i", str(raw),
                "-c:a", "pcm_s16le", str(expected),
            ])

            truth["edits"].append({
                "edit": f"edits/edit_{e:02d}.mkv",
                "expected_dub": f"edits/edit_{e:02d}.expected.wav",
                "duration": cursor,
                "segments": cuts,
            })
            print(f"  edit_{e:02d}: {len(cuts)} segments, {cursor:.1f}s")

    (root / "ground_truth.json").write_text(json.dumps(truth, indent=2))
    print(f"\nwrote {root / 'ground_truth.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
