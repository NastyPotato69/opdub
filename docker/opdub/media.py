"""Media I/O: probe, stream selection, audio decode, mux.

All subprocess calls to ffmpeg/ffprobe live here.
Nothing in this module touches numpy or does DSP.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(cmd, capture_output=True)
    if check and result.returncode != 0:
        err = result.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(f"ffmpeg command failed:\n{err}\nCommand: {' '.join(cmd)}")
    return result


def lang_from_filename(path: Path | str) -> str | None:
    """Return 'jpn', 'eng', or 'skip' if the filename encodes it as .lang.ext.

    Recognises: .jpn .jap (→ 'jpn'),  .eng .dub (→ 'eng'),  .skip (→ 'skip').
    Returns None when no language suffix is present.
    """
    suffixes = Path(path).suffixes  # e.g. ['.jpn', '.mp4']
    if len(suffixes) >= 2:
        lang = suffixes[-2].lstrip(".").lower()
        if lang in ("jpn", "jap"):
            return "jpn"
        if lang in ("eng", "dub"):
            return "eng"
        if lang == "skip":
            return "skip"
    return None


def first_audio_stream(probe_data: dict) -> int:
    """Return the stream index of the first audio track (fallback for mislabelled files)."""
    for s in probe_data.get("streams", []):
        if s.get("codec_type") == "audio":
            return s["index"]
    raise RuntimeError("No audio streams found")


def probe(path: Path | str) -> dict:
    """Return ffprobe stream info as a dict with 'streams' and 'format' keys."""
    path = Path(path)
    result = _run([
        "ffprobe", "-v", "error",
        "-show_streams", "-show_format",
        "-of", "json",
        str(path),
    ])
    return json.loads(result.stdout)


def select_audio_stream(
    probe_data: dict,
    lang: str = "jpn",
    file_path: Path | str | None = None,
) -> int:
    """Return stream index of the best audio track matching lang.

    Selection order:
      1. Stream with tags.language == lang
      2. Stream with title containing lang (case-insensitive)
      3. If only one audio stream exists and lang is explicitly 'jpn' or
         the caller signals single-stream fallback, return it.

    On failure, raise RuntimeError that lists what streams are present.
    """
    streams = probe_data.get("streams", [])
    audio = [s for s in streams if s.get("codec_type") == "audio"]

    if not audio:
        raise RuntimeError(
            f"No audio streams found in {file_path or '(unknown)'}"
        )

    # Exact language tag match
    for s in audio:
        if s.get("tags", {}).get("language", "").lower() == lang.lower():
            return s["index"]

    # Title keyword fallback
    for s in audio:
        title = s.get("tags", {}).get("title", "").lower()
        if lang.lower() in title:
            return s["index"]

    # Single stream with no language tag — accept it rather than failing
    if len(audio) == 1 and not audio[0].get("tags", {}).get("language"):
        return audio[0]["index"]

    desc = ", ".join(
        f"stream {s['index']} lang={s.get('tags', {}).get('language', '?')!r} "
        f"title={s.get('tags', {}).get('title', '?')!r}"
        for s in audio
    )
    raise RuntimeError(
        f"No audio stream matching language={lang!r} in "
        f"{file_path or '(unknown)'}. Available: {desc}"
    )


def decode_audio(
    path: Path | str,
    stream_index: int,
    sr: int = 16000,
    start: float | None = None,
    duration: float | None = None,
) -> "numpy.ndarray":
    """Decode one audio stream to mono float32 at the requested sample rate.

    Always decodes from the stream start so that AAC encoder-delay handling is
    consistent and seeking never shifts samples by one codec frame (~21 ms).
    The start/duration crop is applied as numpy slicing after full decode.
    For alignment-stage audio (16 kHz mono), a 20-minute episode is ~77 MB —
    well within memory.  For the render stage, use extract_audio_segment.
    """
    import numpy as np

    path = Path(path)
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(path),
        "-map", f"0:{stream_index}",
        "-ac", "1", "-ar", str(sr), "-f", "f32le",
        "pipe:1",
    ]

    result = _run(cmd)
    audio = np.frombuffer(result.stdout, dtype="<f4")

    if start is not None:
        s0 = int(round(start * sr))
        audio = audio[s0:]

    if duration is not None:
        n_want = int(round(duration * sr))
        audio = audio[:n_want]

    return audio


def extract_audio_segment(
    path: Path | str,
    stream_index: int,
    start: float,
    duration: float,
) -> "numpy.ndarray":
    """Extract a short audio segment at native sample rate for render/mux use.

    Uses fast-seek + pre-roll (trap 6).  Not sample-accurate vs a full decode
    due to AAC encoder-delay skipping (~21 ms), but the fine-alignment offset
    from Stage 4 corrects for this when we render.
    """
    import numpy as np

    path = Path(path)
    # Pre-roll of 0.5 s absorbs fast-seek landing jitter and resampler warmup.
    pre_roll = 0.5
    actual_start = max(0.0, start - pre_roll)

    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-ss", f"{actual_start:.6f}",
        "-i", str(path),
        "-map", f"0:{stream_index}",
        "-ac", "1", "-f", "f32le",
        "-t", f"{duration + (start - actual_start):.6f}",
        "pipe:1",
    ]

    result = _run(cmd)
    audio = np.frombuffer(result.stdout, dtype="<f4")

    trim = int(round((start - actual_start) * _native_sr(path, stream_index)))
    audio = audio[trim:]
    n_want = int(round(duration * _native_sr(path, stream_index)))
    return audio[:n_want]


def _native_sr(path: Path | str, stream_index: int) -> int:
    """Return the sample rate of the given stream."""
    info = probe(path)
    for s in info["streams"]:
        if s["index"] == stream_index:
            return int(s.get("sample_rate", 48000))
    return 48000


def mux(
    source: Path | str,
    new_audio: Path | str,
    output: Path | str,
    audio_lang: str = "eng",
    audio_title: str = "English Dub",
    make_default: bool = False,
) -> None:
    """Mux source (video + existing streams) with a new audio track.

    Copies all streams from source unchanged; appends new_audio as an
    additional track. Never re-encodes video.
    """
    source = Path(source)
    new_audio = Path(new_audio)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)

    # Count existing streams to know the map index
    source_info = probe(source)
    n_source_streams = len(source_info.get("streams", []))

    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-i", str(source),
        "-i", str(new_audio),
        "-map", "0",        # all streams from source
        "-map", "1:a:0",    # new audio from second input
        "-c", "copy",
    ]

    new_idx = n_source_streams  # index of the new audio track in output
    default_flag = "1" if make_default else "0"
    cmd += [
        f"-metadata:s:{new_idx}", f"language={audio_lang}",
        f"-metadata:s:{new_idx}", f"title={audio_title}",
        f"-disposition:s:{new_idx}", "0" if not make_default else "default",
    ]

    cmd.append(str(output))
    _run(cmd)
