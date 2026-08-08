"""Stage 0 dub-offset measurement and dub audio rendering.

Stage 0: Match each Japanese source to its English dub counterpart, then
measure the timing offset between the two.

Matching strategy (in priority order):
1. Same-file dual-audio: measure offset via full-file cross-correlation.
2. Separate files, same episode number in filename ("Episode 313"): name match,
   then measure offset with a mid-episode windowed cross-correlation that skips
   the opening theme (which is shared across ALL episodes and would otherwise
   dominate the full-file correlation and produce a spurious peak at lag≈0).
3. No episode number in filename: fall back to full-file correlation score to
   rank candidates.

Render: Use the EDL + Stage 0 offsets to extract dub segments, apply
equal-power crossfades, and write a WAV file.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import numpy as np

from .media import decode_audio, first_audio_stream, lang_from_filename, probe, select_audio_stream

# Low SR for Stage 0: enough for peak localisation, 4× faster than 16 kHz.
STAGE0_SR = 4_000


def _corr_lag_offset(jpn: np.ndarray, dub: np.ndarray, sr: int) -> tuple[float, float]:
    """Return (offset_seconds, peak_to_rms_ratio).

    offset: dub_time = jpn_time - offset
    peak_to_rms: higher means better-matched pair (judge match quality by this,
    not by the absolute correlation value which is low for mixed jpn/eng tracks).
    """
    from scipy.signal import correlate

    corr = correlate(jpn, dub, mode="full")
    abs_corr = np.abs(corr)
    peak_idx = int(np.argmax(abs_corr))
    peak = float(abs_corr[peak_idx])
    rms = float(np.sqrt(np.mean(corr ** 2)))
    lag = peak_idx - (len(dub) - 1)
    return lag / sr, (peak / rms if rms > 0 else 0.0)


def measure_dub_offset(
    source_path: Path | str,
    dub_lang: str = "eng",
    jpn_lang: str = "jpn",
    sr: int = STAGE0_SR,
) -> float:
    """Measure dub offset when both tracks are in the same file.

    Returns offset in seconds: dub_time = jpn_time - offset.
    For sources where both tracks are perfectly aligned, offset ≈ 0.
    Raises RuntimeError if no dub stream is found in the file.
    """
    source_path = Path(source_path)
    info = probe(source_path)

    jpn_idx = select_audio_stream(info, lang=jpn_lang, file_path=source_path)
    dub_idx = select_audio_stream(info, lang=dub_lang, file_path=source_path)

    jpn = decode_audio(source_path, jpn_idx, sr=sr)
    dub = decode_audio(source_path, dub_idx, sr=sr)

    offset, _ = _corr_lag_offset(jpn, dub, sr)
    return offset


def find_best_dub(
    jpn_path: Path | str,
    dub_candidates: list[Path],
    dub_lang: str = "eng",
    jpn_lang: str = "jpn",
    sr: int = STAGE0_SR,
) -> tuple[Path, float]:
    """Find which dub file corresponds to jpn_path and return (path, offset).

    Cross-correlates the full jpn track against every dub candidate at low SR.
    The candidate with the highest peak-to-RMS ratio is selected; that ratio is
    typically 10–50× higher for the correct match than for any wrong episode.

    Returns (best_path, offset) where dub_time = jpn_time - offset.
    Raises RuntimeError if no candidate has a dub stream.
    """
    jpn_path = Path(jpn_path)
    info = probe(jpn_path)
    jpn_idx = select_audio_stream(info, lang=jpn_lang, file_path=jpn_path)
    jpn = decode_audio(jpn_path, jpn_idx, sr=sr)

    best_path: Path | None = None
    best_offset = 0.0
    best_score = -1.0

    for dub_path in dub_candidates:
        dub_info = probe(dub_path)
        try:
            dub_idx = select_audio_stream(dub_info, lang=dub_lang, file_path=dub_path)
        except RuntimeError:
            continue
        dub = decode_audio(dub_path, dub_idx, sr=sr)
        offset, score = _corr_lag_offset(jpn, dub, sr)
        if score > best_score:
            best_score = score
            best_offset = offset
            best_path = dub_path

    if best_path is None:
        raise RuntimeError(
            f"No dub candidate with lang={dub_lang!r} found for {jpn_path.name}"
        )
    return best_path, best_offset


def _extract_episode_number(filename: str) -> int | None:
    """Parse an episode number from filenames like 'Episode 313 Title.mp4'."""
    m = re.search(r'[Ee]pisode\s+(\d+)', filename)
    return int(m.group(1)) if m else None


def _windowed_offset(
    jpn: np.ndarray, dub: np.ndarray, sr: int,
    skip_s: float = 120.0, window_s: float = 60.0,
) -> float:
    """Cross-correlate a mid-episode window to measure the jpn→dub timing offset.

    Skips the first skip_s seconds to avoid the shared opening theme, which
    dominates full-file correlations and produces a spurious lag-0 peak for
    any jpn/eng pair from the same arc.  Returns 0.0 if either window is too
    short or the peak quality is poor (threshold: peak/rms < 2).
    """
    skip = int(skip_s * sr)
    n    = int(window_s * sr)
    jw   = jpn[skip : skip + n]
    dw   = dub[skip : skip + n]
    if len(jw) < sr or len(dw) < sr:
        return 0.0
    offset, quality = _corr_lag_offset(jw, dw, sr)
    # A matched jpn/eng pair from the same broadcast differs only by an
    # encoder delay (<200ms). Anything larger is a spurious correlation peak.
    if quality < 2.0 or abs(offset) > 0.2:
        return 0.0
    return offset


def batch_dub_offsets(
    source_audios: list[np.ndarray],
    source_sr: int,
    dub_candidates: list[Path],
    dub_lang: str = "eng",
    sr: int = STAGE0_SR,
    source_paths: "list[Path] | None" = None,
) -> list[tuple["Path | None", float]]:
    """Return (best_dub_path_or_None, offset_seconds) for each source audio.

    Matching priority:
    1. Episode-number match from filename (reliable for standard naming like
       "Episode 313 …") — avoids shared-OP false peaks in full-file correlation.
    2. Full-file correlation score (fallback when names carry no episode number).

    Offset is measured with a mid-episode windowed cross-correlation that skips
    the opening theme.
    """
    from scipy.signal import decimate as _decimate

    if not dub_candidates or not source_audios:
        return [(None, 0.0)] * len(source_audios)

    # Resample existing jpn audio arrays: source_sr → sr via decimation.
    q = source_sr // sr
    jpn_arrays: list[np.ndarray] = []
    for a in source_audios:
        down = _decimate(a, q=q, ftype="fir", zero_phase=True).astype(np.float32)
        jpn_arrays.append(down)

    # Decode each dub candidate ONCE; build episode-number index.
    dub_decoded: list[tuple[Path, np.ndarray]] = []
    dub_by_epnum: dict[int, tuple[Path, np.ndarray]] = {}
    for p in dub_candidates:
        file_lang = lang_from_filename(p)
        if file_lang in ("jpn", "skip"):
            continue  # not a dub candidate
        try:
            info = probe(p)
            if file_lang == "eng":
                # Filename says English — use even when stream tag is wrong.
                try:
                    idx = select_audio_stream(info, lang=dub_lang, file_path=p)
                except RuntimeError:
                    idx = first_audio_stream(info)
            else:
                idx = select_audio_stream(info, lang=dub_lang, file_path=p)
            arr = decode_audio(p, idx, sr=sr)
            dub_decoded.append((p, arr))
            epnum = _extract_episode_number(p.name)
            if epnum is not None and epnum not in dub_by_epnum:
                dub_by_epnum[epnum] = (p, arr)
        except RuntimeError:
            pass

    if not dub_decoded:
        return [(None, 0.0)] * len(source_audios)

    results: list[tuple[Path | None, float]] = []
    for i, jpn in enumerate(jpn_arrays):
        # --- strategy 1: episode-number name match ---
        jpn_path = source_paths[i] if source_paths else None
        jpn_epnum = _extract_episode_number(jpn_path.name) if jpn_path else None
        if jpn_epnum is not None and jpn_epnum in dub_by_epnum:
            matched_path, matched_arr = dub_by_epnum[jpn_epnum]
            offset = _windowed_offset(jpn, matched_arr, sr)
            results.append((matched_path, offset))
            continue

        # --- strategy 2: full-file correlation score (fallback) ---
        best_path: Path | None = None
        best_offset = 0.0
        best_score  = -1.0
        for dub_path, dub in dub_decoded:
            offset, score = _corr_lag_offset(jpn, dub, sr)
            if score > best_score:
                best_score  = score
                best_offset = offset
                best_path   = dub_path
        results.append((best_path, best_offset))

    return results


def render_dub(
    edl: dict,
    source_dir: Path | str,
    output_path: Path | str,
    dub_lang: str = "eng",
    sr_out: int = 48_000,
    crossfade_s: float = 0.0,
) -> None:
    """Extract dub audio segments from sources and write a WAV file.

    Uses full-decode-and-slice (same strategy as alignment decoding) to avoid
    fast-seek AAC encoder-delay offsets at segment boundaries.

    dub_files in the EDL maps source index → dub filename.  When present, the
    dub is read from that file (separate jpn/eng file workflow).
    When absent, the dub is looked up by language tag in the same file
    (fixture workflow: both tracks in one MKV).
    """
    source_dir = Path(source_dir)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    source_names: list[str] = edl.get("sources", [])
    source_paths: list[Path] = []
    for name in source_names:
        candidate = source_dir / Path(name).name
        source_paths.append(candidate if candidate.exists() else Path(name))

    edl_offsets: dict = edl.get("dub_offsets", {})
    edl_dub_files: dict = edl.get("dub_files", {})

    dub_offsets: dict[int, float] = {
        i: float(edl_offsets.get(str(i), 0.0)) for i in range(len(source_paths))
    }

    # Resolve which file to read dub audio from for each source index.
    def _dub_path_for(i: int, sp: Path) -> Path:
        fname = edl_dub_files.get(str(i))
        if fname:
            candidate = source_dir / fname
            return candidate if candidate.exists() else Path(fname)
        return sp  # same file (fixture case)

    # Full-decode dub tracks at the output sample rate.
    dub_audios: dict[int, np.ndarray] = {}
    for i, sp in enumerate(source_paths):
        dub_file = _dub_path_for(i, sp)
        if not dub_file.exists():
            continue
        info = probe(dub_file)
        file_lang = lang_from_filename(dub_file)
        try:
            dub_idx = select_audio_stream(info, lang=dub_lang, file_path=dub_file)
        except RuntimeError:
            if file_lang == "eng":
                # Filename says English but tag is wrong — use first audio stream.
                try:
                    dub_idx = first_audio_stream(info)
                except RuntimeError:
                    continue
            else:
                continue
        dub_audios[i] = decode_audio(dub_file, dub_idx, sr=sr_out)

    # Extract a chunk for each segment.
    cf = max(0, int(crossfade_s * sr_out))
    pieces: list[np.ndarray] = []

    for seg in edl.get("segments", []):
        src_i = seg.get("src")
        duration = seg["t1"] - seg["t0"]
        n = int(round(duration * sr_out))

        if src_i is None or src_i not in dub_audios:
            pieces.append(np.zeros(n, dtype=np.float32))
            continue

        dub = dub_audios[src_i]
        offset = dub_offsets.get(src_i, 0.0)
        dub_start = seg["src_t0"] - offset
        s0 = max(0, int(round(dub_start * sr_out)))

        chunk = dub[s0 : s0 + n]
        if len(chunk) < n:
            chunk = np.concatenate(
                [chunk, np.zeros(n - len(chunk), dtype=np.float32)]
            )
        pieces.append(chunk.astype(np.float32))

    if not pieces:
        print("WARNING: no segments to render", file=sys.stderr)
        return

    # Equal-power fades applied IN-PLACE at segment boundaries — no content
    # is shifted or lost.
    if cf > 0 and len(pieces) > 1:
        ramp = np.linspace(0.0, 1.0, cf, dtype=np.float32)
        fade_in = np.sqrt(ramp)
        fade_out = np.sqrt(1.0 - ramp)
        for i in range(len(pieces) - 1):
            if len(pieces[i]) >= cf:
                pieces[i][-cf:] *= fade_out
            if len(pieces[i + 1]) >= cf:
                pieces[i + 1][:cf] *= fade_in
    combined = np.concatenate(pieces)

    _write_wav(combined, output_path, sr_out)


def _write_wav(audio: np.ndarray, path: Path, sr: int) -> None:
    """Pipe float32 audio into ffmpeg and write a WAV file."""
    raw = np.ascontiguousarray(audio, dtype=np.float32).tobytes()
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "f32le", "-ar", str(sr), "-ac", "1", "-i", "pipe:0",
        "-c:a", "pcm_s16le",
        str(path),
    ]
    proc = subprocess.run(cmd, input=raw, capture_output=True)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"ffmpeg WAV write failed:\n{err}")
