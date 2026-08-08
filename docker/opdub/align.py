"""Coarse-to-fine alignment: Stages 3, 4, 5, and 6.

Stage 3 — Hough-transform fingerprint hits into (episode, delta) clusters.
Stage 4 — Cross-correlate probe windows to refine deltas to sample accuracy.
Stage 5 — Fill remaining gaps by extending neighbours.
Stage 6 — Place each cut at the exact sample using the O(N) cumsum split.
"""
from __future__ import annotations

import numpy as np

from .fingerprint import HOP, SR, Index, fingerprint

# ---------------------------------------------------------------------------
# Coarse alignment constants
# ---------------------------------------------------------------------------
# 1-second buckets: stable against quiet passages.
BUCKET_FRAMES = max(1, int(round(SR / HOP)))   # ≈ 62 frames = 0.99 s
# 4-frame delta quantisation (≈64 ms): fuses ±1-frame fingerprint jitter so
# the true votes are not split across two adjacent delta bins.
DQUANT = 4

MIN_SEGMENT_DURATION = 1.5   # seconds — slivers below this get merged

# Stage 4: fine delta refinement
FINE_PROBE_DUR = 4.0         # seconds per probe window
FINE_PROBE_COUNT = 3         # probes per segment

# Stage 5: gap-fill
GAP_FILL_PROBE = 4.0         # seconds of audio for each gap-fill probe

# Stage 6: boundary refinement search range
BOUNDARY_SEARCH = 1.5        # ±seconds around detected boundary


def build_index(source_audios: list[np.ndarray]) -> Index:
    """Fingerprint all source tracks and return a built Index."""
    idx = Index()
    for i, audio in enumerate(source_audios):
        idx.add(audio, source_id=i)
    idx.build()
    return idx


# ---------------------------------------------------------------------------
# Stage 3 — Coarse Hough-transform segmentation
# ---------------------------------------------------------------------------

def coarse_align(
    edit_audio: np.ndarray,
    index: Index,
    edit_duration: float,
) -> list[dict]:
    """Stage 3: Hough-transform fingerprint hits into a coarse segment list.

    Algorithm:
      1. Bucket edit timeline into 1-second bins.
      2. For each bucket, count hits per (src, quantised_delta).
         Quantising to ±64 ms bins merges ±1-frame fingerprint jitter so
         the true delta is not split across adjacent bins.
      3. Assign each bucket to its local winner.
      4. Smooth out isolated wrong buckets (a bucket flanked by the same
         (src, quantised_delta) on both sides with fewer votes is overridden).
      5. Extract contiguous same-label runs into segment dicts.
    """
    query_hashes = fingerprint(edit_audio)

    n_edit_frames = len(edit_audio) // HOP + 1
    n_buckets = n_edit_frames // BUCKET_FRAMES + 1

    # Accumulate bucket counts in chunks — avoids materialising the full hits
    # list which can be O(200M) tuples for real episodes (~22 GB of RAM).
    bucket_counts = index.query_to_bucket_counts(
        query_hashes, n_edit_frames, BUCKET_FRAMES, DQUANT)

    # For each bucket, pick the (src, dq) with the most LOCAL hits.
    # Store the quantised delta (dq*DQUANT) so comparisons are stable.
    bucket_label = np.full(n_buckets, -1, dtype=np.int32)
    bucket_dq = np.zeros(n_buckets, dtype=np.int64)   # quantised delta
    bucket_votes = np.zeros(n_buckets, dtype=np.int32)

    for (b, src, dq), count in bucket_counts.items():
        if count > bucket_votes[b]:
            bucket_votes[b] = count
            bucket_label[b] = src
            bucket_dq[b] = dq

    # Smooth: override an isolated wrong bucket when both immediate neighbours
    # agree on the same (src, quantised_delta) and have higher confidence.
    for _ in range(4):
        for b in range(1, n_buckets - 1):
            lbl, l_lbl, r_lbl = bucket_label[b], bucket_label[b-1], bucket_label[b+1]
            dq, l_dq, r_dq = bucket_dq[b], bucket_dq[b-1], bucket_dq[b+1]
            if (lbl != l_lbl and l_lbl == r_lbl and l_lbl >= 0 and
                    l_dq == r_dq and
                    bucket_votes[b] < (bucket_votes[b-1] + bucket_votes[b+1]) / 2):
                bucket_label[b] = l_lbl
                bucket_dq[b] = l_dq
                bucket_votes[b] = int((bucket_votes[b-1] + bucket_votes[b+1]) / 2)

    # Expand bucket arrays to frame-level
    label = np.repeat(bucket_label, BUCKET_FRAMES)[:n_edit_frames]
    delta_arr = np.repeat(bucket_dq * DQUANT, BUCKET_FRAMES)[:n_edit_frames]
    vote_arr = np.repeat(bucket_votes, BUCKET_FRAMES)[:n_edit_frames]

    segments = _extract_runs(label, delta_arr, vote_arr, n_edit_frames, edit_duration)
    return segments


def _extract_runs(
    label: np.ndarray,
    delta: np.ndarray,
    vote_per_frame: np.ndarray,
    n_frames: int,
    duration: float,
) -> list[dict]:
    """Convert per-frame label/delta arrays into a segment list."""
    if n_frames == 0:
        return []

    segs = []
    i = 0
    while i < n_frames:
        src = int(label[i])
        d = int(delta[i])
        j = i + 1
        while j < n_frames and int(label[j]) == src and int(delta[j]) == d:
            j += 1
        t0 = i * HOP / SR
        t1 = min(j * HOP / SR, duration)
        votes = int(vote_per_frame[i:j].sum())
        segs.append({
            "t0": t0,
            "t1": t1,
            "src": src if src >= 0 else None,
            "delta_frames": d,
            "votes": votes,
        })
        i = j

    segs = _merge_slivers(segs, MIN_SEGMENT_DURATION)

    for s in segs:
        # Coarse delta in seconds (refined in Stage 4)
        s["delta"] = s["delta_frames"] * HOP / SR if s["src"] is not None else None
        s["src_t0"] = s["t0"] + s["delta"] if s["delta"] is not None else None
        dur = s["t1"] - s["t0"]
        s["vote_density"] = s["votes"] / dur if dur > 0 else 0.0
        s["status"] = "ok" if s["src"] is not None else "gap"

    return segs


def _merge_slivers(segs: list[dict], min_dur: float) -> list[dict]:
    """Absorb runs shorter than min_dur into their dominant neighbour.

    Interior slivers flanked by the same (src, delta) are absorbed into
    the run on either side.  For boundary slivers surrounded by different
    sources, they are absorbed into the longer neighbour (trap 7).
    """
    changed = True
    while changed:
        changed = False
        merged = []
        i = 0
        while i < len(segs):
            s = segs[i]
            dur = s["t1"] - s["t0"]
            if dur < min_dur and 0 < i < len(segs) - 1:
                prev = merged[-1]
                nxt = segs[i + 1]
                # Flanked by the same (src, delta): absorb into prev
                if (prev["src"] == nxt["src"] == s["src"] and
                        prev["delta_frames"] == nxt["delta_frames"] == s["delta_frames"]):
                    merged[-1] = {**prev, "t1": s["t1"],
                                  "votes": prev["votes"] + s["votes"]}
                    changed = True
                    i += 1
                    continue
                # Different neighbours: absorb into the longer one
                prev_dur = prev["t1"] - prev["t0"]
                nxt_dur = nxt["t1"] - nxt["t0"]
                if prev_dur >= nxt_dur:
                    merged[-1] = {**prev, "t1": s["t1"],
                                  "votes": prev["votes"] + s["votes"]}
                else:
                    # Defer: let the next iteration absorb into nxt
                    # by absorbing nxt here and letting nxt claim it
                    segs[i + 1] = {**nxt, "t0": s["t0"],
                                   "votes": nxt["votes"] + s["votes"]}
                    changed = True
                    i += 1
                    continue
                changed = True
                i += 1
                continue
            merged.append(s)
            i += 1
        segs = merged
    return segs


# ---------------------------------------------------------------------------
# Stage 4 — Fine delta refinement
# ---------------------------------------------------------------------------

def _normalised_xcorr(a: np.ndarray, b: np.ndarray) -> float:
    """Scalar normalised cross-correlation coefficient."""
    n = min(len(a), len(b))
    if n < 16:
        return 0.0
    a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
    denom = np.sqrt((a @ a) * (b @ b))
    if denom < 1e-9:
        return 0.0
    return float(a @ b) / denom


def _xcorr_peak_lag(
    source_padded: np.ndarray,
    edit_win: np.ndarray,
    search_radius: int,
) -> int:
    """Return the lag (in samples) that maximises normalised cross-correlation.

    source_padded has length len(edit_win) + 2*search_radius;
    the lag is measured relative to the centre of the source_padded window
    (i.e. lag=0 → source starts search_radius samples before edit_win).

    Uses FFT cross-correlation for O(N log N) efficiency.
    """
    from scipy.signal import correlate
    n_edit = len(edit_win)
    n_src = len(source_padded)
    if n_src < n_edit or n_edit < 16:
        return 0

    e = edit_win - edit_win.mean()
    s = source_padded - source_padded.mean()

    # Full cross-correlation; valid mode gives one value per src alignment
    xcorr = correlate(s, e, mode="valid")  # length = n_src - n_edit + 1

    best_idx = int(np.argmax(xcorr))
    # best_idx = 0 means s[0:n_edit] aligns with edit_win
    # We want lag relative to centre (search_radius samples from left)
    lag = best_idx - search_radius
    return lag


def refine_delta(
    seg: dict,
    edit_audio: np.ndarray,
    source_audios: list[np.ndarray],
) -> dict:
    """Stage 4: refine a segment's source offset via cross-correlation probes.

    Each probe window cross-correlates edit audio against a ±(2 × DQUANT × HOP)
    sample search window in the source, covering the full quantisation error
    from Stage 3 (DQUANT * HOP ≈ 64 ms) plus one extra bin of margin.
    The median of all probe lags gives the refined delta.
    A nonzero slope across probes would indicate speed drift; on this arc
    the slope is expected to be zero (confirmed by the corpus measurement).
    """
    src = seg["src"]
    if src is None:
        return seg

    source = source_audios[src]
    delta_coarse = seg["delta"]   # seconds, ±64 ms error from Stage 3
    dur = seg["t1"] - seg["t0"]

    if dur < 0.5:
        return seg

    # Probe windows: evenly spaced within the segment, 0.5 s from each edge
    margin = min(0.5, dur / 4)
    n_probes = min(FINE_PROBE_COUNT, max(1, int(dur / (FINE_PROBE_DUR + 0.5))))
    probe_dur = min(FINE_PROBE_DUR, max(0.5, dur - 2 * margin))
    probe_starts = np.linspace(
        seg["t0"] + margin,
        max(seg["t0"] + margin, seg["t1"] - margin - probe_dur),
        n_probes,
    )

    # Search radius in samples: covers ±2 quantisation bins = ±128 ms.
    # Fingerprint coarse bins are DQUANT * HOP wide; two bins gives margin.
    SEARCH_RADIUS_S = int(2 * DQUANT * HOP)   # samples at SR ≈ 2048 samples

    probe_lags = []
    for ps in probe_starts:
        e0 = int(round(ps * SR))
        e1 = e0 + int(round(probe_dur * SR))
        edit_win = edit_audio[e0:e1]
        n_e = len(edit_win)
        if n_e < 16:
            continue

        src_centre = int(round((ps + delta_coarse) * SR))
        s0 = max(0, src_centre - SEARCH_RADIUS_S)
        s1 = min(len(source), src_centre + n_e + SEARCH_RADIUS_S)
        src_padded = source[s0:s1]

        # Compute actual search radius after clamping to source boundaries
        actual_left_pad = src_centre - SEARCH_RADIUS_S - s0
        if len(src_padded) < n_e:
            continue

        lag = _xcorr_peak_lag(src_padded, edit_win, SEARCH_RADIUS_S - actual_left_pad)
        probe_lags.append(lag)

    if not probe_lags:
        return seg

    best_lag = int(np.median(probe_lags))
    refined_delta = delta_coarse + best_lag / SR

    return {
        **seg,
        "delta": refined_delta,
        "delta_frames": int(round(refined_delta * SR / HOP)),
        "src_t0": seg["t0"] + refined_delta,
    }


# ---------------------------------------------------------------------------
# Stage 5 — Gap fill
# ---------------------------------------------------------------------------

def fill_gaps(
    segments: list[dict],
    edit_audio: np.ndarray,
    source_audios: list[np.ndarray],
    edit_duration: float,
) -> list[dict]:
    """Stage 5: extend neighbours into each unmatched gap via correlation."""
    result = []
    for i, seg in enumerate(segments):
        if seg["src"] is not None or seg["t1"] - seg["t0"] < 0.05:
            result.append(seg)
            continue

        left = _find_neighbour(result, direction="left")
        right = _find_neighbour(segments[i + 1:], direction="right")

        t0, t1 = seg["t0"], seg["t1"]
        best_seg = seg
        best_corr = 0.3

        if left is not None:
            c, filled = _test_extend(left, t0, t1, edit_audio, source_audios)
            if c > best_corr:
                best_corr, best_seg = c, filled

        if right is not None:
            c, filled = _test_extend(right, t0, t1, edit_audio, source_audios)
            if c > best_corr:
                best_corr, best_seg = c, filled

        result.append(best_seg)

    return _merge_adjacent(result)


def _find_neighbour(segs: list[dict], direction: str) -> dict | None:
    iterable = reversed(segs) if direction == "left" else iter(segs)
    for s in iterable:
        if s["src"] is not None:
            return s
    return None


def _test_extend(
    neighbour: dict,
    gap_t0: float,
    gap_t1: float,
    edit_audio: np.ndarray,
    source_audios: list[np.ndarray],
) -> tuple[float, dict]:
    src_id = neighbour["src"]
    delta = neighbour["delta"]
    gap_dur = gap_t1 - gap_t0

    probe_dur = min(gap_dur, GAP_FILL_PROBE)
    src_t0 = gap_t0 + delta

    e0 = int(round(gap_t0 * SR))
    e1 = int(round((gap_t0 + probe_dur) * SR))
    edit_seg = edit_audio[e0:e1]
    if len(edit_seg) < SR // 4:
        return 0.0, {}

    s0 = int(round(src_t0 * SR))
    source = source_audios[src_id]
    s1 = min(s0 + len(edit_seg), len(source))
    if s0 < 0 or s1 <= s0:
        return 0.0, {}

    src_seg = source[s0:s1]
    corr = _normalised_xcorr(edit_seg, src_seg)

    if corr > 0.3:
        return corr, {
            "t0": gap_t0, "t1": gap_t1,
            "src": src_id,
            "delta": delta,
            "delta_frames": int(round(delta * SR / HOP)),
            "src_t0": src_t0,
            "votes": 0,
            "vote_density": 0.0,
            "status": "gap-filled",
        }
    return corr, {}


def _merge_adjacent(segments: list[dict]) -> list[dict]:
    """Merge consecutive segments that share (src, delta_frames)."""
    if not segments:
        return segments
    merged = [segments[0].copy()]
    for seg in segments[1:]:
        prev = merged[-1]
        if (prev["src"] == seg["src"] and prev["src"] is not None and
                prev.get("delta_frames") == seg.get("delta_frames")):
            merged[-1] = {**prev, "t1": seg["t1"],
                          "votes": prev["votes"] + seg["votes"]}
        else:
            merged.append(seg.copy())
    return merged


# ---------------------------------------------------------------------------
# Stage 6 — Exact cut placement (O(N) cumsum split, trap 5)
# ---------------------------------------------------------------------------

def place_cuts(
    segments: list[dict],
    edit_audio: np.ndarray,
    source_audios: list[np.ndarray],
) -> list[dict]:
    """Stage 6: refine each inter-segment boundary to sample accuracy.

    For the boundary between seg_left and seg_right, a ±BOUNDARY_SEARCH second
    window around the detected boundary is evaluated.  Both hypotheses are
    expanded to that window, then the O(N) cumsum split finds the optimal
    split point (trap 5).
    """
    if len(segments) < 2:
        return segments

    result = [segments[0]]
    for seg in segments[1:]:
        prev = result[-1]
        if prev["src"] is None or seg["src"] is None:
            result.append(seg)
            continue

        # Don't split between same (src, delta) — they should have been merged
        if prev["src"] == seg["src"] and abs(
                (prev.get("delta") or 0) - (seg.get("delta") or 0)) < 0.05:
            result.append(seg)
            continue

        # Detected boundary
        boundary = prev["t1"]
        t0 = max(0.0, boundary - BOUNDARY_SEARCH)
        t1 = min(len(edit_audio) / SR, boundary + BOUNDARY_SEARCH)

        e0 = int(round(t0 * SR))
        e1 = int(round(t1 * SR))
        edit_win = edit_audio[e0:e1]

        # Left hypothesis
        src_l = source_audios[prev["src"]]
        sl0 = int(round((t0 + prev["delta"]) * SR))
        sl1 = sl0 + len(edit_win)
        sl0 = max(0, sl0); sl1 = min(len(src_l), sl1)
        src_l_win = src_l[sl0:sl1]

        # Right hypothesis
        src_r = source_audios[seg["src"]]
        sr0 = int(round((t0 + seg["delta"]) * SR))
        sr1 = sr0 + len(edit_win)
        sr0 = max(0, sr0); sr1 = min(len(src_r), sr1)
        src_r_win = src_r[sr0:sr1]

        n = min(len(edit_win), len(src_l_win), len(src_r_win))
        if n < SR // 4:
            result.append(seg)
            continue

        e = edit_win[:n]
        l_h = src_l_win[:n]
        r_h = src_r_win[:n]

        # O(N) cumulative-error split (trap 5)
        err_l = (e - l_h) ** 2
        err_r = (e - r_h) ** 2
        total_r = err_r.sum()
        cum_l = np.cumsum(err_l)
        cum_r = np.cumsum(err_r)
        split_cost = cum_l + (total_r - cum_r)

        best_sample = int(np.argmin(split_cost))
        new_boundary = t0 + best_sample / SR

        # Clamp to sensible range (don't move the cut more than BOUNDARY_SEARCH)
        new_boundary = float(np.clip(
            new_boundary,
            prev["t0"] + 0.05,
            seg["t1"] - 0.05,
        ))

        result[-1] = {**prev, "t1": new_boundary}
        new_seg = {**seg, "t0": new_boundary}
        # Recompute src_t0 after moving t0
        if new_seg.get("delta") is not None:
            new_seg["src_t0"] = new_boundary + new_seg["delta"]
        result.append(new_seg)

    return result
