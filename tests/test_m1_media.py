"""M1 gate tests: probe, decode_audio, mux, stream selection."""
from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import pytest

from opdub.media import decode_audio, mux, probe, select_audio_stream

FIXTURES = Path(__file__).parent.parent / "fixtures"
SRC = FIXTURES / "sources" / "src_00.mkv"
EDIT = FIXTURES / "edits" / "edit_00.mkv"
SR = 16000


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def test_probe_returns_streams():
    info = probe(SRC)
    assert "streams" in info
    streams = info["streams"]
    codec_types = {s["codec_type"] for s in streams}
    assert "video" in codec_types
    assert "audio" in codec_types


def test_probe_source_has_two_audio_streams():
    info = probe(SRC)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 2


# ---------------------------------------------------------------------------
# select_audio_stream
# ---------------------------------------------------------------------------

def test_select_jpn():
    info = probe(SRC)
    idx = select_audio_stream(info, lang="jpn", file_path=SRC)
    tags = info["streams"][idx].get("tags", {})
    assert tags.get("language", "").lower() == "jpn"


def test_select_eng():
    info = probe(SRC)
    idx = select_audio_stream(info, lang="eng", file_path=SRC)
    tags = info["streams"][idx].get("tags", {})
    assert tags.get("language", "").lower() == "eng"


def test_missing_lang_raises():
    info = probe(SRC)
    with pytest.raises(RuntimeError, match="language="):
        select_audio_stream(info, lang="fra", file_path=SRC)


def test_edit_single_untagged_fallback():
    """Edit files have one (jpn) audio stream; single-stream fallback applies."""
    info = probe(EDIT)
    audio = [s for s in info["streams"] if s["codec_type"] == "audio"]
    assert len(audio) == 1
    idx = select_audio_stream(info, lang="jpn", file_path=EDIT)
    assert idx == audio[0]["index"]


# ---------------------------------------------------------------------------
# decode_audio
# ---------------------------------------------------------------------------

def _stream_index(path: Path, lang: str) -> int:
    info = probe(path)
    return select_audio_stream(info, lang=lang, file_path=path)


def test_decode_returns_float32():
    idx = _stream_index(SRC, "jpn")
    audio = decode_audio(SRC, idx, sr=SR, duration=2.0)
    assert audio.dtype == np.float32


def test_decode_length_matches_duration():
    idx = _stream_index(SRC, "jpn")
    dur = 3.0
    audio = decode_audio(SRC, idx, sr=SR, duration=dur)
    expected = int(round(dur * SR))
    # Allow ±2 samples for rounding
    assert abs(len(audio) - expected) <= 2, f"got {len(audio)}, want {expected}"


def test_decode_offset_matches_full_decode():
    """Decode at an offset and compare against slicing a full decode."""
    idx = _stream_index(SRC, "jpn")
    offset = 5.0
    dur = 2.0

    full = decode_audio(SRC, idx, sr=SR)
    sliced = full[int(round(offset * SR)):int(round((offset + dur) * SR))]

    partial = decode_audio(SRC, idx, sr=SR, start=offset, duration=dur)

    # Align lengths
    n = min(len(sliced), len(partial))
    sliced, partial = sliced[:n], partial[:n]

    # Normalised correlation should be > 0.999 (sample-accurate)
    xs, ys = sliced - sliced.mean(), partial - partial.mean()
    denom = np.sqrt((xs @ xs) * (ys @ ys))
    assert denom > 1e-9
    corr = float(xs @ ys) / denom
    assert corr > 0.999, f"offset decode correlation={corr:.4f}, expected >0.999"


# ---------------------------------------------------------------------------
# mux
# ---------------------------------------------------------------------------

def test_mux_preserves_original_streams():
    """After muxing, all original streams should survive with codec copy."""
    orig_info = probe(SRC)
    orig_streams = orig_info["streams"]

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        # Create a silent audio file to add
        dummy_wav = tmp / "dummy.wav"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", "2.0", "-c:a", "pcm_s16le", str(dummy_wav),
        ], check=True)

        out_mkv = tmp / "out.mkv"
        mux(SRC, dummy_wav, out_mkv, audio_lang="eng", audio_title="Test Dub")

        out_info = probe(out_mkv)
        out_streams = out_info["streams"]

    # Original streams should all be present
    assert len(out_streams) == len(orig_streams) + 1

    # Check codecs preserved (copy)
    for i, orig in enumerate(orig_streams):
        out = out_streams[i]
        assert out["codec_name"] == orig["codec_name"], (
            f"stream {i}: codec changed {orig['codec_name']} -> {out['codec_name']}"
        )


def test_mux_new_track_tagged():
    """The new audio track should have the correct language and title tags."""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        dummy_wav = tmp / "dummy.wav"
        subprocess.run([
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", "1.0", "-c:a", "pcm_s16le", str(dummy_wav),
        ], check=True)

        out_mkv = tmp / "tagged.mkv"
        mux(SRC, dummy_wav, out_mkv, audio_lang="eng", audio_title="English Dub")

        out_info = probe(out_mkv)
        new_stream = out_info["streams"][-1]

    assert new_stream["codec_type"] == "audio"
    tags = new_stream.get("tags", {})
    assert tags.get("language", "").lower() == "eng"
    assert "English Dub" in tags.get("title", "")


def test_missing_dub_lang_fails_loudly():
    """select_audio_stream must raise if the requested language is absent."""
    info = probe(EDIT)  # edit only has jpn
    with pytest.raises(RuntimeError):
        select_audio_stream(info, lang="eng", file_path=EDIT)
