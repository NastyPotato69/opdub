"""Stage 0 dub-offset measurement and dub audio rendering.

Stage 0: Cross-correlate the full dub track against the full Japanese track
to find their timing offset. The shared music-and-effects bed produces a sharp
peak even though the dialogue layers differ (correlation ≈ 0.05–0.15 is normal;
judge by peak-to-sidelobe ratio, not absolute value).

For real One Pace sources, the dub lives in a separate file from the Japanese
audio. find_best_dub() handles this by cross-correlating each jpn source against
every candidate dub file and returning the best match.

Render: Use the EDL + Stage 0 offsets to extract dub segments, apply
equal-power crossfades, and write a WAV file.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import numpy as np

from .media import decode_audio, probe, select_audio_stream

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


def batch_dub_offsets(
    source_audios: list[np.ndarray],
    source_sr: int,
    dub_candidates: list[Path],
    dub_lang: str = "eng",
    sr: int = STAGE0_SR,
) -> list[tuple["Path | None", float]]:
    """Return (best_dub_path_or_None, offset_seconds) for each source audio.

    Avoids the O(N²) decode cost of calling find_best_dub per source by:
    - Decimating existing source audio (no subprocess) instead of re-decoding jpn
    - Decoding each dub candidate exactly once regardless of how many jpn sources there are
    """
    from scipy.signal import decimate as _decimate

    if not dub_candidates or not source_audios:
        return [(None, 0.0)] * len(source_audios)

    # Resample existing jpn audio arrays: source_sr → sr via anti-aliased decimation.
    # Avoids re-running ffmpeg for sources we already decoded at 16 kHz.
    q = source_sr // sr
    jpn_arrays: list[np.ndarray] = []
    for a in source_audios:
        down = _decimate(a, q=q, ftype="fir", zero_phase=True).astype(np.float32)
        jpn_arrays.append(down)

    # Decode each dub candidate ONCE
    dub_decoded: list[tuple[Path, np.ndarray]] = []
    for p in dub_candidates:
        try:
            info = probe(p)
            idx = select_audio_stream(info, lang=dub_lang, file_path=p)
            dub_decoded.append((p, decode_audio(p, idx, sr=sr)))
        except RuntimeError:
            pass

    if not dub_decoded:
        return [(None, 0.0)] * len(source_audios)

    results: list[tuple[Path | None, float]] = []
    for jpn in jpn_arrays:
        best_path: Path | None = None
        best_offset = 0.0
        best_score = -1.0
        for dub_path, dub in dub_decoded:
            offset, score = _corr_lag_offset(jpn, dub, sr)
            if score > best_score:
                best_score = score
                best_offset = offset
                best_path = dub_path
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
    dub is read from that file (real One Pace workflow: separate jpn/eng files).
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
        try:
            dub_idx = select_audio_stream(info, lang=dub_lang, file_path=dub_file)
        except RuntimeError:
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
