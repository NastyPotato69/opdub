"""M2 gate: fingerprint a source and query it against itself."""
from __future__ import annotations

import numpy as np
import pytest

from opdub.fingerprint import Index, SR, HOP, fingerprint


def _noise_audio(seed: int, seconds: float) -> np.ndarray:
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    audio = np.zeros_like(t, dtype=np.float32)
    # Sweeping tones — same construction as the fixture generator
    for _ in range(int(seconds * 7)):
        start = rng.uniform(0, max(0.01, seconds - 0.7))
        dur = rng.uniform(0.12, 0.5)
        f0 = rng.uniform(90, 1400)
        f1 = f0 * rng.uniform(0.55, 1.8)
        a, b = int(start * SR), min(len(t), int((start + dur) * SR))
        if b - a < 32:
            continue
        seg = t[a:b] - t[a]
        wave = np.sin(2 * np.pi * (f0 * seg + (f1 - f0) / (2 * dur) * seg ** 2))
        audio[a:b] += (wave * np.hanning(b - a) * rng.uniform(0.4, 1.0)).astype(np.float32)
    peak = np.abs(audio).max()
    return (audio / peak * 0.8).astype(np.float32) if peak > 1e-6 else audio


def _vote_margin(hits, expected_delta_frames: int, tolerance_frames: int = 4):
    """Return (top_votes, second_votes) over delta buckets."""
    deltas = [src - qry for _, src, qry in hits]
    if not deltas:
        return 0, 0
    counts = {}
    for d in deltas:
        counts[d] = counts.get(d, 0) + 1
    sorted_counts = sorted(counts.values(), reverse=True)
    top = sorted_counts[0]
    second = sorted_counts[1] if len(sorted_counts) > 1 else 0
    return top, second


def _votes(hits) -> dict[int, int]:
    """Map delta → vote count."""
    from collections import Counter
    return dict(Counter(src - qry for _, src, qry in hits))


def test_self_query_recovers_zero_offset():
    """Querying an index built from audio against itself should vote strongly
    for delta=0 with a high margin over second place."""
    audio = _noise_audio(seed=42, seconds=30.0)

    idx = Index()
    idx.add(audio, source_id=0)
    idx.build()

    query_hashes = fingerprint(audio)
    hits = idx.query(query_hashes)

    assert len(hits) > 100, f"Only {len(hits)} hits — index may be empty"

    vote = _votes(hits)
    top_delta, top_count = max(vote.items(), key=lambda x: x[1])
    second = sorted(vote.values(), reverse=True)[1] if len(vote) > 1 else 0

    assert top_delta == 0, f"Top delta is {top_delta}, expected 0"
    assert top_count > 50, f"Top vote count too low: {top_count}"
    assert top_count > second * 3, (
        f"Margin too low: top={top_count} second={second}"
    )


def test_degraded_copy_still_recovers_offset():
    """A gain-shifted + noise-added copy should still vote for delta=0."""
    rng = np.random.default_rng(99)
    audio = _noise_audio(seed=7, seconds=30.0)

    idx = Index()
    idx.add(audio, source_id=0)
    idx.build()

    # Degrade: same as the fixture generator
    degraded = (audio * 0.74 + rng.normal(0, 0.004, audio.size)).astype(np.float32)
    query_hashes = fingerprint(degraded)
    hits = idx.query(query_hashes)

    assert len(hits) > 50, f"Only {len(hits)} hits with degraded copy"

    vote = _votes(hits)
    top_delta, top_count = max(vote.items(), key=lambda x: x[1])
    second = sorted(vote.values(), reverse=True)[1] if len(vote) > 1 else 0

    assert top_delta == 0, f"Top delta is {top_delta}, expected 0"
    assert top_count > 10, f"Top vote count too low: {top_count}"
    assert top_count > second * 5, (
        f"Margin too low: top={top_count} second={second}"
    )


def test_known_offset_is_recovered():
    """Querying a slice of audio against a full index finds the right offset."""
    audio = _noise_audio(seed=5, seconds=60.0)

    idx = Index()
    idx.add(audio, source_id=0)
    idx.build()

    # Query with a 15s slice starting at 20s
    offset_sec = 20.0
    offset_frames = int(round(offset_sec * SR / HOP))
    query_audio = audio[int(offset_sec * SR):int((offset_sec + 15) * SR)]
    query_hashes = fingerprint(query_audio)
    hits = idx.query(query_hashes)

    assert len(hits) > 20, f"Only {len(hits)} hits for offset query"

    # Compute deltas: for a hit (src_id, src_frame, qry_frame),
    # delta = src_frame - qry_frame should equal offset_frames
    deltas = [src - qry for _, src, qry in hits]
    counts = {}
    for d in deltas:
        counts[d] = counts.get(d, 0) + 1
    best_delta = max(counts, key=counts.__getitem__)
    top_count = counts[best_delta]
    all_counts = sorted(counts.values(), reverse=True)
    second = all_counts[1] if len(all_counts) > 1 else 0

    assert abs(best_delta - offset_frames) <= 2, (
        f"Recovered delta={best_delta} frames, expected ~{offset_frames}±2"
    )
    assert top_count > second * 3, (
        f"Margin too low: top={top_count} second={second} "
        f"(delta={best_delta}, expected={offset_frames})"
    )
