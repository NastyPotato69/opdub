"""CLI: align and render commands.

    python3 -m opdub.cli align <edit.mkv> --sources <dir> --out <dir>
    python3 -m opdub.cli render <edl.json> --sources <dir> --out <dir>

align  writes one EDL JSON (with Stage 0 dub offsets) per edit to --out.
render reads an EDL and writes a .dub.wav next to the ground-truth dir.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

from .align import build_index, coarse_align, fill_gaps, place_cuts, refine_delta
from .media import decode_audio, first_audio_stream, lang_from_filename, probe, select_audio_stream
from .render import batch_dub_offsets, find_best_dub, measure_dub_offset, render_dub

# Match on the real extension (.mp4, .mkv, .avi) regardless of any language
# prefix (.jpn.mp4, .eng.mkv, …).
_MEDIA_EXTS = {".mkv", ".mp4", ".avi"}


def _try_decode_jpn(path: Path, verbose: bool = False) -> np.ndarray | None:
    """Decode jpn audio; return None if the file is not a Japanese source.

    Detection order:
    1. Filename suffix (.jpn.ext → yes, .eng.ext → no)
    2. Stream language tag (jpn → yes)
    3. Single untagged audio stream with no language hint → yes (legacy fallback)
    """
    file_lang = lang_from_filename(path)

    # Filename explicitly says English or excluded — skip without probing.
    if file_lang in ("eng", "skip"):
        if verbose:
            reason = "filename says eng" if file_lang == "eng" else "marked skip"
            print(f"  skipping {path.name}: {reason}")
        return None

    try:
        info = probe(path)

        if file_lang == "jpn":
            # Filename says Japanese — use it even when the stream tag is wrong.
            try:
                idx = select_audio_stream(info, lang="jpn", file_path=path)
            except RuntimeError:
                idx = first_audio_stream(info)
        else:
            # No filename hint — rely on stream tag.
            idx = select_audio_stream(info, lang="jpn", file_path=path)

        if verbose:
            print(f"  decoding {path.name}…", flush=True)
        return decode_audio(path, idx, sr=16000)

    except RuntimeError:
        if verbose:
            print(f"  skipping {path.name}: no jpn stream")
        return None


def cmd_align(args: argparse.Namespace) -> int:
    edit_path = Path(args.edit)
    source_dir = Path(args.sources)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    verbose = args.verbose
    dub_lang = args.dub_lang

    # Discover all media files in source dir.
    all_paths = sorted(p for p in source_dir.iterdir() if p.suffix.lower() in _MEDIA_EXTS)

    # Apply source-file filter before jpn detection so only selected episodes
    # are fingerprinted; dub candidates are still drawn from all_paths below.
    if getattr(args, "source_files", None):
        selected = set(args.source_files)
        candidates = [p for p in all_paths if p.name in selected]
        if not candidates:
            print(f"ERROR: none of {args.source_files} found in {source_dir}", file=sys.stderr)
            return 1
    else:
        candidates = all_paths

    if verbose:
        print(f"Scanning {len(candidates)} source file(s) in {source_dir}…")
    source_paths, source_audios = [], []
    for p in candidates:
        audio = _try_decode_jpn(p, verbose)
        if audio is not None:
            source_paths.append(p)
            source_audios.append(audio)

    if not source_paths:
        print(f"ERROR: no jpn-audio source files found in {source_dir}", file=sys.stderr)
        return 1

    if verbose:
        print(f"  {len(source_paths)} jpn sources: {[p.name for p in source_paths]}", flush=True)

    # Stage 2: build fingerprint index
    if verbose:
        print("Stage 2: building fingerprint index…", flush=True)
    index = build_index(source_audios)
    if verbose:
        print(f"  index postings: {len(index)}", flush=True)

    # Decode edit (jpn)
    if verbose:
        print(f"Decoding edit {edit_path.name}…", flush=True)
    info = probe(edit_path)
    edit_idx = select_audio_stream(info, lang="jpn", file_path=edit_path)
    edit_audio = decode_audio(edit_path, edit_idx, sr=16000)
    edit_duration = len(edit_audio) / 16000
    if verbose:
        print(f"  edit duration: {edit_duration:.1f}s", flush=True)

    # Stage 3: coarse alignment
    if verbose:
        print("Stage 3: coarse alignment…", flush=True)
    segments = coarse_align(edit_audio, index, edit_duration)
    if verbose:
        print(f"  {len(segments)} segments", flush=True)

    # Stage 4: refine deltas
    if verbose:
        print("Stage 4: refining deltas…", flush=True)
    segments = [refine_delta(s, edit_audio, source_audios) for s in segments]

    # Stage 5: fill gaps
    if verbose:
        print("Stage 5: filling gaps…", flush=True)
    segments = fill_gaps(segments, edit_audio, source_audios, edit_duration)

    # Stage 6: exact cut placement
    if verbose:
        print("Stage 6: placing cuts…", flush=True)
    segments = place_cuts(segments, edit_audio, source_audios)

    # Stage 0: for each jpn source find its dub counterpart and measure offset.
    # Dub candidates = all media files in source_dir that are NOT jpn sources.
    if verbose:
        print("Stage 0: matching dub files and measuring offsets…", flush=True)

    jpn_names = {p.name for p in source_paths}
    dub_candidates = [p for p in all_paths if p.name not in jpn_names]

    dub_offsets: dict[str, float] = {}
    dub_files: dict[str, str] = {}

    # First pass: try same-file dub (fixture / dual-audio case).
    need_batch: list[int] = []
    for i, sp in enumerate(source_paths):
        try:
            off = measure_dub_offset(sp, dub_lang=dub_lang)
            dub_offsets[str(i)] = off
            dub_files[str(i)] = sp.name
            if verbose:
                print(f"  src {i} ({sp.name}): dub in same file, offset {off*1000:.1f} ms",
                      flush=True)
        except RuntimeError:
            need_batch.append(i)

    # Second pass: batch-match separate dub files.
    # batch_dub_offsets decodes each dub candidate once and resamples the
    # already-decoded jpn audio — avoids O(sources × candidates) subprocesses.
    if need_batch and dub_candidates:
        if verbose:
            print(f"  batch-matching {len(dub_candidates)} dub candidate(s) "
                  f"for {len(need_batch)} source(s)…", flush=True)
        batch_results = batch_dub_offsets(
            source_audios=[source_audios[i] for i in need_batch],
            source_sr=16_000,
            dub_candidates=dub_candidates,
            dub_lang=dub_lang,
            source_paths=[source_paths[i] for i in need_batch],
        )
        for i, (best_path, off) in zip(need_batch, batch_results):
            sp = source_paths[i]
            if best_path is not None:
                dub_offsets[str(i)] = off
                dub_files[str(i)] = best_path.name
                if verbose:
                    print(f"  src {i} ({sp.name}): dub → {best_path.name}, "
                          f"offset {off*1000:.1f} ms", flush=True)
            else:
                dub_offsets[str(i)] = 0.0
                if verbose:
                    print(f"  src {i} ({sp.name}): no dub found", flush=True)
    elif need_batch:
        for i in need_batch:
            dub_offsets[str(i)] = 0.0
            if verbose:
                print(f"  src {i} ({source_paths[i].name}): no dub candidates", flush=True)

    # Write EDL
    edl: dict = {
        "edit": str(edit_path.name),
        "duration": edit_duration,
        "sources": [str(p.name) for p in source_paths],
        "dub_offsets": dub_offsets,
        "dub_files": dub_files,
        "segments": [
            {
                "t0": s["t0"],
                "t1": s["t1"],
                "src": s["src"],
                "src_t0": s["src_t0"],
                "delta": s["delta"],
                "conf": float(s.get("vote_density", 0.0)),
                "status": s.get("status", "ok"),
            }
            for s in segments
        ],
    }

    out_path = out_dir / f"{edit_path.stem}.json"
    out_path.write_text(json.dumps(edl, indent=2))
    if verbose:
        print(f"wrote {out_path}")
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    edl_path = Path(args.edl)
    source_dir = Path(args.sources)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    verbose = args.verbose

    edl = json.loads(edl_path.read_text())
    stem = Path(edl.get("edit", edl_path.stem)).stem
    output_wav = out_dir / f"{stem}.dub.wav"

    if verbose:
        print(f"Rendering {stem} → {output_wav}")

    render_dub(
        edl=edl,
        source_dir=source_dir,
        output_path=output_wav,
        dub_lang=args.dub_lang,
    )

    if verbose:
        print(f"wrote {output_wav}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="opdub")
    sub = ap.add_subparsers(dest="cmd")

    al = sub.add_parser("align", help="Align edit to sources and write EDL")
    al.add_argument("edit", help="Edit MKV/MP4 file")
    al.add_argument("--sources", required=True, help="Directory of source files")
    al.add_argument("--out", required=True, help="Output directory for EDL JSONs")
    al.add_argument("--dub-lang", default="eng", help="Language tag for dub track")
    al.add_argument("--source-files", nargs="+", metavar="FILE",
                    help="Specific filenames within --sources to align (default: all)")
    al.add_argument("--verbose", "-v", action="store_true")

    rn = sub.add_parser("render", help="Render dub audio from EDL")
    rn.add_argument("edl", help="EDL JSON file")
    rn.add_argument("--sources", required=True, help="Directory of source files")
    rn.add_argument("--out", required=True, help="Output directory for .dub.wav files")
    rn.add_argument("--dub-lang", default="eng", help="Language tag for dub track")
    rn.add_argument("--verbose", "-v", action="store_true")

    args = ap.parse_args()
    if args.cmd is None:
        ap.print_help()
        return 1
    if args.cmd == "align":
        return cmd_align(args)
    if args.cmd == "render":
        return cmd_render(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
