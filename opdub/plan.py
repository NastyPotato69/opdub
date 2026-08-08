"""Human-authored run plan: every ambiguous decision is an explicit input.

The CLI's `align` command infers things — which files hold Japanese audio,
which audio stream to use, which dub file pairs with which source, which
source episodes an edit draws from.  Each of those inferences is a place the
run can go wrong silently.

A plan removes all of them.  The operator states, in full:

  * which audio stream of the edit is the Japanese track
  * which source episodes this edit was cut from
  * for each source episode, the exact jpn file+stream and dub file+stream
  * optionally, the jpn->dub offset (otherwise it is measured and reported)
  * which time ranges of the edit are passthrough (opening theme, credits)

Nothing here reads a language tag or parses a filename.  If the plan is
wrong, it is wrong in a way the operator can see in the UI before pressing
run, which is the whole point.

Passthrough ranges are carved out of the aligned segment list after
alignment rather than being masked before it.  Alignment inside the opening
theme is unreliable — the theme is identical across every episode of the arc,
so votes there are genuinely ambiguous — but its output is discarded by the
carve, and forcing the boundaries at the operator's times stops a bad intro
match from dragging the first real cut with it.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .align import build_index, coarse_align, fill_gaps, place_cuts, refine_delta
from .media import decode_audio, probe
from .render import STAGE0_SR, _corr_lag_offset

# Alignment sample rate. Fixed by the fingerprinter, not a tuning knob.
ALIGN_SR = 16_000

# A matched jpn/eng pair from the same broadcast master differs only by an
# encoder delay, which is well under 200 ms.  A larger measurement means the
# correlator locked onto something else and the number must not be used
# without a human looking at it.
MAX_PLAUSIBLE_OFFSET_S = 0.2

# Peak-to-RMS ratio below which a measured offset is not trustworthy.  Same
# threshold render._windowed_offset uses; kept here so the UI can show the
# operator the number it was judged against.
MIN_OFFSET_QUALITY = 2.0

# Offset measurement skips this far into the episode to avoid the opening
# theme, which is shared across the whole arc and produces a spurious lag-0
# peak for any pair drawn from it.
OFFSET_SKIP_S = 120.0
OFFSET_WINDOW_S = 60.0

# Segments shorter than this after carving are dropped as numerical debris
# rather than kept as zero-length entries the renderer would have to guard.
MIN_SEGMENT_S = 1e-4


class PlanError(ValueError):
    """A plan is missing something or points at something that is not there."""


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def _require(obj: dict, key: str, where: str) -> Any:
    if key not in obj or obj[key] is None:
        raise PlanError(f"{where}: missing required field {key!r}")
    return obj[key]


def _check_stream(path: Path, stream_index: int, where: str) -> dict:
    """Confirm the named stream exists in the file and is audio."""
    if not path.exists():
        raise PlanError(f"{where}: file does not exist: {path}")
    try:
        info = probe(path)
    except RuntimeError as exc:
        raise PlanError(f"{where}: cannot probe {path.name}: {exc}") from exc

    for s in info.get("streams", []):
        if s.get("index") == stream_index:
            if s.get("codec_type") != "audio":
                raise PlanError(
                    f"{where}: stream {stream_index} of {path.name} is "
                    f"{s.get('codec_type')!r}, not audio"
                )
            return s

    available = ", ".join(
        f"{s['index']}:{s.get('codec_type')}"
        for s in info.get("streams", [])
    )
    raise PlanError(
        f"{where}: {path.name} has no stream {stream_index}. Streams: {available}"
    )


def validate_plan(plan: dict) -> dict:
    """Check a plan end to end and return it with paths resolved.

    Raises PlanError with an actionable message on the first problem found.
    Every file and stream named in the plan is probed here, before any
    expensive decoding starts, so a typo costs a second rather than an hour.
    """
    edit = _require(plan, "edit", "plan")
    edit_path = Path(_require(edit, "path", "plan.edit")).expanduser()
    edit_stream = int(_require(edit, "audio_stream", "plan.edit"))
    _check_stream(edit_path, edit_stream, "plan.edit")

    sources = _require(plan, "sources", "plan")
    if not sources:
        raise PlanError(
            "plan.sources is empty — select at least one source episode. "
            "Source episodes are chosen by hand; nothing is auto-detected."
        )

    for i, src in enumerate(sources):
        where = f"plan.sources[{i}]"
        jpn = _require(src, "jpn", where)
        jpn_path = Path(_require(jpn, "path", f"{where}.jpn")).expanduser()
        jpn_stream = int(_require(jpn, "audio_stream", f"{where}.jpn"))
        _check_stream(jpn_path, jpn_stream, f"{where}.jpn")

        dub = src.get("dub")
        if dub:
            dub_path = Path(_require(dub, "path", f"{where}.dub")).expanduser()
            dub_stream = int(_require(dub, "audio_stream", f"{where}.dub"))
            _check_stream(dub_path, dub_stream, f"{where}.dub")

    duration = float(edit.get("duration") or 0.0)
    for i, rng in enumerate(plan.get("passthrough") or []):
        where = f"plan.passthrough[{i}]"
        t0 = float(_require(rng, "t0", where))
        t1 = float(_require(rng, "t1", where))
        if t1 <= t0:
            raise PlanError(f"{where}: t1 ({t1}) must be greater than t0 ({t0})")
        if t0 < 0:
            raise PlanError(f"{where}: t0 ({t0}) is negative")
        if duration and t0 >= duration:
            raise PlanError(
                f"{where}: t0 ({t0:.3f}s) is past the end of the edit "
                f"({duration:.3f}s)"
            )

    return plan


def load_plan(path: Path | str) -> dict:
    plan = json.loads(Path(path).read_text())
    return validate_plan(plan)


# --------------------------------------------------------------------------
# passthrough ranges
# --------------------------------------------------------------------------

def merge_ranges(ranges: list[dict]) -> list[dict]:
    """Sort passthrough ranges and merge any that touch or overlap."""
    if not ranges:
        return []
    ordered = sorted(
        ({"t0": float(r["t0"]), "t1": float(r["t1"]),
          "label": r.get("label") or "passthrough"} for r in ranges),
        key=lambda r: r["t0"],
    )
    merged = [ordered[0]]
    for r in ordered[1:]:
        last = merged[-1]
        if r["t0"] <= last["t1"]:
            last["t1"] = max(last["t1"], r["t1"])
            if r["label"] not in last["label"]:
                last["label"] = f"{last['label']}+{r['label']}"
        else:
            merged.append(r)
    return merged


def _subtract(seg: dict, t0: float, t1: float) -> list[dict]:
    """Remove [t0, t1) from one segment, returning 0, 1 or 2 pieces.

    The right-hand piece has its source time advanced by the amount removed,
    so the segment keeps pointing at the same moment of the source.
    """
    if seg["t1"] <= t0 or seg["t0"] >= t1:
        return [seg]

    pieces: list[dict] = []
    if seg["t0"] < t0:
        left = dict(seg)
        left["t1"] = t0
        pieces.append(left)
    if seg["t1"] > t1:
        right = dict(seg)
        right["t0"] = t1
        if right.get("src_t0") is not None:
            right["src_t0"] = seg["src_t0"] + (t1 - seg["t0"])
        pieces.append(right)
    return [p for p in pieces if p["t1"] - p["t0"] > MIN_SEGMENT_S]


def carve_passthrough(
    segments: list[dict], ranges: list[dict], duration: float
) -> list[dict]:
    """Replace aligned segments inside passthrough ranges with markers.

    The result stays contiguous and sorted.  Passthrough segments carry
    src=None and status='passthrough'; the renderer fills them from the
    edit's own audio rather than from a source episode.
    """
    ranges = merge_ranges(ranges)
    if not ranges:
        return segments

    kept: list[dict] = list(segments)
    for r in ranges:
        nxt: list[dict] = []
        for seg in kept:
            nxt.extend(_subtract(seg, r["t0"], r["t1"]))
        kept = nxt

    for r in ranges:
        t1 = min(r["t1"], duration) if duration else r["t1"]
        if t1 - r["t0"] <= MIN_SEGMENT_S:
            continue
        kept.append({
            "t0": r["t0"],
            "t1": t1,
            "src": None,
            "src_t0": None,
            "delta": None,
            "conf": None,
            "status": "passthrough",
            "label": r["label"],
        })

    kept.sort(key=lambda s: s["t0"])
    return kept


# --------------------------------------------------------------------------
# offset measurement
# --------------------------------------------------------------------------

def measure_pair_offset(
    jpn_path: Path | str,
    jpn_stream: int,
    dub_path: Path | str,
    dub_stream: int,
    sr: int = STAGE0_SR,
) -> dict:
    """Measure the jpn->dub timing offset for one explicitly paired episode.

    Unlike render._windowed_offset this reports what it found instead of
    silently returning 0.0 on a poor match, because the operator is going to
    look at the number and decide.

    Returns {offset, quality, accepted, reason}.  dub_time = jpn_time - offset.
    """
    jpn = decode_audio(jpn_path, jpn_stream, sr=sr)
    dub = decode_audio(dub_path, dub_stream, sr=sr)

    skip = int(OFFSET_SKIP_S * sr)
    n = int(OFFSET_WINDOW_S * sr)
    jw, dw = jpn[skip:skip + n], dub[skip:skip + n]

    if len(jw) < sr or len(dw) < sr:
        # Episode shorter than the skip window — fall back to the whole file.
        jw, dw = jpn, dub
        if len(jw) < sr or len(dw) < sr:
            return {"offset": 0.0, "quality": 0.0, "accepted": False,
                    "reason": "audio too short to measure"}

    offset, quality = _corr_lag_offset(jw, dw, sr)

    if quality < MIN_OFFSET_QUALITY:
        return {"offset": offset, "quality": quality, "accepted": False,
                "reason": f"weak correlation peak ({quality:.1f} < "
                          f"{MIN_OFFSET_QUALITY}) — are these the same episode?"}
    if abs(offset) > MAX_PLAUSIBLE_OFFSET_S:
        return {"offset": offset, "quality": quality, "accepted": False,
                "reason": f"offset {offset*1000:.0f} ms exceeds the "
                          f"{MAX_PLAUSIBLE_OFFSET_S*1000:.0f} ms an encoder "
                          f"delay can explain — likely the wrong pairing"}
    return {"offset": offset, "quality": quality, "accepted": True, "reason": ""}


# --------------------------------------------------------------------------
# the run
# --------------------------------------------------------------------------

def run_plan(plan: dict, log=print) -> dict:
    """Execute a validated plan and return the EDL.

    log() receives one line per stage; the server maps those lines to a
    progress bar and streams them to the browser.
    """
    plan = validate_plan(plan)

    edit = plan["edit"]
    edit_path = Path(edit["path"]).expanduser()
    edit_stream = int(edit["audio_stream"])
    sources = plan["sources"]

    log(f"Plan: {edit_path.name}, {len(sources)} source episode(s), "
        f"{len(plan.get('passthrough') or [])} passthrough range(s)")

    log("Stage 1: decoding source episodes…")
    source_audios: list[np.ndarray] = []
    for i, src in enumerate(sources):
        jpn = src["jpn"]
        log(f"  [{i}] {Path(jpn['path']).name} stream {jpn['audio_stream']}")
        source_audios.append(
            decode_audio(jpn["path"], int(jpn["audio_stream"]), sr=ALIGN_SR)
        )

    log("Stage 2: building fingerprint index…")
    index = build_index(source_audios)
    log(f"  index postings: {len(index)}")

    log(f"Stage 3: decoding edit {edit_path.name} stream {edit_stream}…")
    edit_audio = decode_audio(edit_path, edit_stream, sr=ALIGN_SR)
    edit_duration = len(edit_audio) / ALIGN_SR
    log(f"  edit duration: {edit_duration:.1f}s")

    log("Stage 4: coarse alignment…")
    segments = coarse_align(edit_audio, index, edit_duration)
    log(f"  {len(segments)} segments")

    log("Stage 5: refining deltas…")
    segments = [refine_delta(s, edit_audio, source_audios) for s in segments]

    log("Stage 6: filling gaps…")
    segments = fill_gaps(segments, edit_audio, source_audios, edit_duration)

    log("Stage 7: placing cuts…")
    segments = place_cuts(segments, edit_audio, source_audios)

    edl_segments = [
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
    ]

    passthrough = merge_ranges(plan.get("passthrough") or [])
    if passthrough:
        before = len(edl_segments)
        edl_segments = carve_passthrough(edl_segments, passthrough, edit_duration)
        log(f"Stage 8: carving {len(passthrough)} passthrough range(s) "
            f"({before} → {len(edl_segments)} segments)")
        for r in passthrough:
            log(f"  {r['label']}: {r['t0']:.2f}s → {r['t1']:.2f}s "
                f"(edit's own audio kept)")

    log("Stage 9: dub offsets…")
    dub_offsets: dict[str, float] = {}
    dub_files: dict[str, str] = {}
    dub_streams: dict[str, int] = {}
    offset_reports: dict[str, dict] = {}

    for i, src in enumerate(sources):
        dub = src.get("dub")
        if not dub:
            log(f"  [{i}] no dub file assigned — segments will be silent")
            continue

        dub_files[str(i)] = Path(dub["path"]).name
        dub_streams[str(i)] = int(dub["audio_stream"])

        stated = src.get("dub_offset")
        if stated is not None:
            dub_offsets[str(i)] = float(stated)
            offset_reports[str(i)] = {"offset": float(stated), "quality": None,
                                      "accepted": True, "reason": "set by hand"}
            log(f"  [{i}] offset {float(stated)*1000:.1f} ms (set by hand)")
            continue

        report = measure_pair_offset(
            src["jpn"]["path"], int(src["jpn"]["audio_stream"]),
            dub["path"], int(dub["audio_stream"]),
        )
        offset_reports[str(i)] = report
        if report["accepted"]:
            dub_offsets[str(i)] = report["offset"]
            log(f"  [{i}] offset {report['offset']*1000:.1f} ms "
                f"(quality {report['quality']:.1f})")
        else:
            # Refuse to invent a number. 0.0 is the honest default and the
            # report travels with the EDL so the UI can flag it.
            dub_offsets[str(i)] = 0.0
            log(f"  [{i}] WARNING: {report['reason']} — using 0 ms, check this")

    edl = {
        "edit": edit_path.name,
        "edit_path": str(edit_path),
        "edit_stream": edit_stream,
        "duration": edit_duration,
        "sources": [Path(s["jpn"]["path"]).name for s in sources],
        "source_paths": [str(Path(s["jpn"]["path"])) for s in sources],
        "source_streams": {str(i): int(s["jpn"]["audio_stream"])
                           for i, s in enumerate(sources)},
        "dub_offsets": dub_offsets,
        "dub_files": dub_files,
        "dub_paths": {str(i): str(Path(s["dub"]["path"]))
                      for i, s in enumerate(sources) if s.get("dub")},
        "dub_streams": dub_streams,
        "offset_reports": offset_reports,
        "passthrough": passthrough,
        "segments": edl_segments,
        "plan": plan,
    }
    return edl
