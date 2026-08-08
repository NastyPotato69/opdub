"""Audio fingerprinting via spectral-peak constellation hashing.

Stages 2 and 3 build and query an index that maps
  packed(freq_bin_1, freq_bin_2, delta_frame) → CSR row of (source_id, anchor_frame)

All DSP takes arrays and returns arrays; no filesystem I/O here.

Memory notes
------------
fingerprint() returns a (N, 4) uint32 array — 16 bytes per hash.
Index stores postings as a CSR numpy array: 8 bytes per posting vs ~128 bytes
for the equivalent Python tuple.  For four 25-minute episodes (~18 M postings)
this cuts index memory from ~4.5 GB (Python tuples) to ~200 MB (numpy CSR).
"""
from __future__ import annotations

import numpy as np
from scipy.signal import spectrogram

# ---------------------------------------------------------------------------
# STFT parameters
# ---------------------------------------------------------------------------
SR = 16_000          # expected sample rate coming in
HOP = 256            # ~16 ms hop — 256 samples at 16 kHz
WIN = 1024           # ~64 ms window
N_FFT = WIN          # no zero-padding; frequency bins are WIN//2+1 = 513

# Peak-picking parameters
PEAK_NEIGHBOURHOOD = 8  # ±frames around each peak for local max test
PEAKS_PER_FRAME = 5     # keep only the N tallest peaks per frame after NMS
FAN_SIZE = 10           # pair each peak with this many future peaks
DELTA_MAX = 40          # max pairing distance in frames (~640 ms)

# Pruning: hashes seen in more than this many positions are from silence/tone
MAX_POSTINGS = 500

# Bit-packing layout for index keys:
#   bits  0-9   : f1  (max 512, needs 10 bits)
#   bits 10-19  : f2  (max 512, needs 10 bits)
#   bits 20-25  : dt  (max 40,  needs  6 bits)
# Total: 26 bits → fits comfortably in uint32.
_F2_SHIFT = np.uint32(10)
_DT_SHIFT = np.uint32(20)


# ---------------------------------------------------------------------------
# STFT helpers (float32 throughout to halve memory vs float64)
# ---------------------------------------------------------------------------

def _stft_db(audio: np.ndarray) -> np.ndarray:
    """Return log-magnitude spectrogram as float32, shape (n_freq, n_frames)."""
    _, _, Sxx = spectrogram(
        audio, fs=SR, window="hann",
        nperseg=WIN, noverlap=WIN - HOP,
        nfft=N_FFT, scaling="spectrum",
    )
    Sxx = Sxx.astype(np.float32, copy=False)
    return np.maximum(
        np.float32(10.0) * np.log10(np.maximum(Sxx, np.float32(1e-8))),
        np.float32(-80.0),
    )


def _pick_peaks(db: np.ndarray) -> np.ndarray:
    """Return (N, 2) uint32 array of (frame, freq_bin) local maxima.

    Fully vectorised — no Python per-frame loop.
    A point is a peak if it is the temporal max in its ±PEAK_NEIGHBOURHOOD
    frame window and among the PEAKS_PER_FRAME tallest in its frame.
    """
    from scipy.ndimage import maximum_filter1d

    n_freq, n_frames = db.shape
    if n_frames == 0:
        return np.empty((0, 2), dtype=np.uint32)

    # Local max along time axis: mode='constant', cval=-inf matches the
    # original clamped-boundary behaviour (out-of-range positions contribute
    # nothing to the max, so boundary frames can still be peaks).
    db_max_t = maximum_filter1d(
        db,
        size=2 * PEAK_NEIGHBOURHOOD + 1,
        axis=1,
        mode="constant",
        cval=np.float32(-np.inf),
    )
    is_local_max = db >= db_max_t  # (n_freq, n_frames)

    # Mask non-maxima then select the top PEAKS_PER_FRAME per frame.
    db_masked = np.where(is_local_max, db, np.float32(-np.inf))
    k = min(PEAKS_PER_FRAME, n_freq)
    # argpartition along frequency axis: top-k freq bins for each frame
    part = np.argpartition(db_masked, -k, axis=0)[-k:]   # (k, n_frames)

    selected = np.zeros((n_freq, n_frames), dtype=bool)
    frame_idx = np.broadcast_to(np.arange(n_frames), part.shape)
    selected[part.ravel(), frame_idx.ravel()] = True
    selected &= is_local_max

    freq_idx, frm_idx = np.where(selected)
    if len(frm_idx) == 0:
        return np.empty((0, 2), dtype=np.uint32)
    return np.column_stack([frm_idx, freq_idx]).astype(np.uint32)


def _hash_peaks(peaks: np.ndarray) -> np.ndarray:
    """peaks: (N, 2) uint32 of (frame, freq).  Returns (M, 4) uint32 hashes.

    Hash layout: [f1, f2, delta_frame, anchor_frame].

    Fully vectorised: each peak fans out to the next FAN_SIZE peaks by index.
    Since peaks are time-sorted, those are exactly the FAN_SIZE nearest future
    peaks in time — equivalent to the original per-peak searchsorted loop but
    ~100x faster on real-episode sizes.
    """
    if len(peaks) == 0:
        return np.empty((0, 4), dtype=np.uint32)

    order = np.argsort(peaks[:, 0], kind="stable")
    peaks = peaks[order]
    # int64 for index arithmetic; cast back to uint32 at the end
    frames = peaks[:, 0].astype(np.int64)
    freqs = peaks[:, 1].astype(np.int64)
    n = len(peaks)

    # All (anchor, partner) index pairs: each anchor fans to the next FAN_SIZE
    fan = np.arange(1, FAN_SIZE + 1, dtype=np.int64)  # [1 … FAN_SIZE]
    i_idx = np.repeat(np.arange(n, dtype=np.int64), FAN_SIZE)
    j_idx = i_idx + np.tile(fan, n)

    # Keep only in-bounds pairs
    valid = j_idx < n
    i_idx = i_idx[valid]
    j_idx = j_idx[valid]

    # Keep only pairs within DELTA_MAX frames of each other
    dts = frames[j_idx] - frames[i_idx]
    in_range = dts <= DELTA_MAX
    i_idx = i_idx[in_range]
    j_idx = j_idx[in_range]
    dts = dts[in_range]

    m = len(i_idx)
    if m == 0:
        return np.empty((0, 4), dtype=np.uint32)

    out = np.empty((m, 4), dtype=np.uint32)
    out[:, 0] = freqs[i_idx]                    # f1
    out[:, 1] = freqs[j_idx]                    # f2
    out[:, 2] = dts                              # delta_frame
    out[:, 3] = frames[i_idx]                   # anchor_frame
    return out


def fingerprint(audio: np.ndarray) -> np.ndarray:
    """Return (N, 4) uint32 array: [f1, f2, delta_frame, anchor_frame]."""
    db = _stft_db(audio)
    peaks = _pick_peaks(db)
    return _hash_peaks(peaks)


# ---------------------------------------------------------------------------
# Index — memory-efficient CSR storage
# ---------------------------------------------------------------------------

class Index:
    """Inverted index using numpy CSR arrays for postings.

    Memory: ~8 bytes per posting (two uint32 columns: src_id, anchor_frame)
    vs ~128 bytes for the equivalent Python tuple in a dict list.

    Build:
        add(audio, source_id)  — one call per source
        build()                — sort, prune, build CSR
    Query:
        query(hashes)          — accepts (N, 4) uint32 numpy array
    """

    def __init__(self) -> None:
        # Accumulate (N, 5) chunks during add(): [f1, f2, dt, src_id, anchor]
        self._pending: list[np.ndarray] = []
        self._n_sources = 0
        # Set by build():
        self._pack_keys: np.ndarray | None = None  # (K,)   uint32 sorted unique keys
        self._offsets: np.ndarray | None = None     # (K+1,) uint32 CSR row pointers
        self._src_anchor: np.ndarray | None = None  # (M, 2) uint32 [src_id, anchor]

    def add(self, audio: np.ndarray, source_id: int) -> None:
        """Fingerprint audio and stage it for the index."""
        hashes = fingerprint(audio)  # (N, 4) uint32
        n = len(hashes)
        if n == 0:
            return
        chunk = np.empty((n, 5), dtype=np.uint32)
        chunk[:, :3] = hashes[:, :3]           # f1, f2, dt
        chunk[:, 3] = np.uint32(source_id)
        chunk[:, 4] = hashes[:, 3]             # anchor_frame
        self._pending.append(chunk)
        self._n_sources = max(self._n_sources, source_id + 1)

    def build(self) -> None:
        """Sort all staged hashes, prune common keys, build CSR arrays."""
        if not self._pending:
            return

        data = np.concatenate(self._pending, axis=0)  # (total, 5)
        self._pending.clear()

        packed = (data[:, 0] |
                  (data[:, 1] << _F2_SHIFT) |
                  (data[:, 2] << _DT_SHIFT))   # uint32 key

        order = np.argsort(packed, kind="stable")
        data = data[order]
        packed = packed[order]

        unique_pk, first_idx, counts = np.unique(
            packed, return_index=True, return_counts=True
        )

        # Prune keys with too many postings (silence / repeated patterns)
        prune = counts > MAX_POSTINGS
        if prune.any():
            keep = np.ones(len(data), dtype=bool)
            for fi, cnt in zip(first_idx[prune], counts[prune]):
                keep[fi : fi + cnt] = False
            data = data[keep]
            packed = packed[keep]
            del keep
            unique_pk, first_idx, counts = np.unique(
                packed, return_index=True, return_counts=True
            )
        del packed, first_idx

        offsets = np.zeros(len(unique_pk) + 1, dtype=np.uint32)
        offsets[1:] = np.cumsum(counts)

        self._pack_keys = unique_pk
        self._offsets = offsets
        # Contiguous copy so src_anchor doesn't hold a ref to the big data array
        self._src_anchor = np.ascontiguousarray(data[:, 3:5])  # (M, 2)
        del data

    def query(
        self, hashes: np.ndarray
    ) -> list[tuple[int, int, int]]:
        """Return (src_id, src_anchor, query_anchor) triples.

        hashes: (N, 4) uint32 array [f1, f2, dt, anchor].
        For numpy input, all N lookups are batched in one searchsorted call,
        then only matching rows drive the inner posting loop.
        """
        if self._pack_keys is None or len(hashes) == 0:
            return []

        if isinstance(hashes, np.ndarray):
            return self._query_array(hashes)

        # Scalar/list path (kept for tests that pass tuples/lists)
        hits: list[tuple[int, int, int]] = []
        for row in hashes:
            f1, f2, dt, qa = row
            pk = (np.uint32(f1) |
                  (np.uint32(f2) << _F2_SHIFT) |
                  (np.uint32(dt) << _DT_SHIFT))
            idx = int(np.searchsorted(self._pack_keys, pk))
            if idx >= len(self._pack_keys) or self._pack_keys[idx] != pk:
                continue
            start = int(self._offsets[idx])
            end = int(self._offsets[idx + 1])
            for posting in self._src_anchor[start:end]:
                hits.append((int(posting[0]), int(posting[1]), qa))
        return hits

    def _query_array(self, hashes: np.ndarray) -> list[tuple[int, int, int]]:
        """Batch query for numpy hash arrays: one searchsorted over all N hashes."""
        packed = (hashes[:, 0] |
                  (hashes[:, 1] << _F2_SHIFT) |
                  (hashes[:, 2] << _DT_SHIFT)).astype(np.uint32)

        # Single batch searchsorted — O(N log K) in C, no Python loop
        csr_idx = np.searchsorted(self._pack_keys, packed)

        # Keep only positions where the key actually exists
        in_bounds = csr_idx < len(self._pack_keys)
        qpos = np.where(in_bounds)[0]
        if len(qpos) == 0:
            return []
        mi = csr_idx[qpos]
        exact = self._pack_keys[mi] == packed[qpos]
        qpos = qpos[exact]
        mi = mi[exact]

        # Gather postings — Python loop only over matched hashes
        hits: list[tuple[int, int, int]] = []
        query_anchors = hashes[:, 3]
        for q, m in zip(qpos.tolist(), mi.tolist()):
            qa = int(query_anchors[q])
            start = int(self._offsets[m])
            end = int(self._offsets[m + 1])
            for posting in self._src_anchor[start:end]:
                hits.append((int(posting[0]), int(posting[1]), qa))
        return hits

    def query_to_bucket_counts(
        self,
        hashes: np.ndarray,
        n_edit_frames: int,
        bucket_frames: int,
        dquant: int,
        chunk_size: int = 50_000,
    ) -> dict:
        """Accumulate Hough-transform bucket counts without materialising hits.

        Uses a pre-allocated numpy accumulator rather than a Python dict to
        avoid the 14M-element-per-chunk Python loop that dominates at real-
        episode scale.  Internally uses HOUGH_DQUANT = dquant × 4 bins so
        the accumulator fits in ~400 MB rather than 1.6 GB.  Output keys are
        converted back to dquant-granularity before returning.

        Returns {(bucket, src_id, quantised_delta): hit_count}
        where quantised_delta is in dquant (not HOUGH_DQUANT) units so that
        coarse_align's delta_frames = dq × DQUANT remains correct.
        """
        if self._pack_keys is None or len(hashes) == 0:
            return {}

        # Pack all query hashes, find matches with one C-level searchsorted call
        packed = (hashes[:, 0] |
                  (hashes[:, 1] << _F2_SHIFT) |
                  (hashes[:, 2] << _DT_SHIFT)).astype(np.uint32)

        csr_idx = np.searchsorted(self._pack_keys, packed)
        in_bounds = csr_idx < len(self._pack_keys)
        qpos_all = np.where(in_bounds)[0]
        if len(qpos_all) == 0:
            return {}

        mi_all = csr_idx[qpos_all]
        exact = self._pack_keys[mi_all] == packed[qpos_all]
        qpos_all = qpos_all[exact]
        mi_all = mi_all[exact]
        del packed, csr_idx, in_bounds

        # Sort by CSR row index so src_anchor reads are sequential rather than
        # random — eliminates cache thrashing on the 127 MB array.
        sort_order = np.argsort(mi_all, kind="stable")
        qpos_all = qpos_all[sort_order]
        mi_all = mi_all[sort_order]
        del sort_order

        # Use the same dquant as the caller — no coarsening — so Stage 4's
        # search radius (= 2 × DQUANT × HOP) comfortably covers the ±dquant/2
        # frame quantisation error.  The 1.6 GB accumulator fits in 16 GB RAM.
        HOUGH_DQUANT = dquant

        n_buckets = n_edit_frames // bucket_frames + 1
        n_src = max(1, self._n_sources)

        # dq range (in HOUGH_DQUANT frame units) tight-bounded from actual data
        max_src_frame = int(self._src_anchor[:, 1].max()) if len(self._src_anchor) else 0
        dq_min = -(n_edit_frames // HOUGH_DQUANT) - 1
        dq_max = max_src_frame // HOUGH_DQUANT + 1
        DQ_RANGE = dq_max - dq_min + 1

        SRC_STRIDE = DQ_RANGE
        B_STRIDE = n_src * DQ_RANGE
        acc = np.zeros(n_buckets * n_src * DQ_RANGE, dtype=np.int32)

        query_anchors = hashes[:, 3]
        n_matched = len(qpos_all)

        for chunk_start in range(0, n_matched, chunk_size):
            chunk_end = min(chunk_start + chunk_size, n_matched)
            qpos = qpos_all[chunk_start:chunk_end]
            mi = mi_all[chunk_start:chunk_end]

            starts = self._offsets[mi].astype(np.int64)
            ends = self._offsets[mi + 1].astype(np.int64)
            counts = ends - starts
            total = int(counts.sum())
            if total == 0:
                continue

            # CSR → COO expansion (fully vectorised, sequential after mi-sort)
            hash_fp = np.repeat(np.arange(len(qpos), dtype=np.int64), counts)
            cum_sh = np.empty(len(qpos), dtype=np.int64)
            cum_sh[0] = 0
            if len(qpos) > 1:
                cum_sh[1:] = np.cumsum(counts[:-1])
            posting_idx = (np.repeat(starts, counts)
                           + np.arange(total, dtype=np.int64)
                           - np.repeat(cum_sh, counts))
            del cum_sh

            src_ids = self._src_anchor[posting_idx, 0].astype(np.int64)
            src_frames = self._src_anchor[posting_idx, 1].astype(np.int64)
            del posting_idx

            qry_frames = query_anchors[qpos[hash_fp]].astype(np.int64)
            del hash_fp

            b = qry_frames // bucket_frames
            dq_coarse = (src_frames - qry_frames) // HOUGH_DQUANT
            del src_frames, qry_frames

            dq_idx = dq_coarse - dq_min
            del dq_coarse

            # Clip to valid range (most postings should be in range already)
            valid = (b >= 0) & (b < n_buckets) & (dq_idx >= 0) & (dq_idx < DQ_RANGE)
            if not valid.all():
                b = b[valid]; src_ids = src_ids[valid]; dq_idx = dq_idx[valid]
            del valid

            flat_idx = (b * B_STRIDE + src_ids * SRC_STRIDE + dq_idx).astype(np.int64)
            del b, src_ids, dq_idx

            # Sort → unique → indexed add.  unique_flat is in increasing order
            # so acc[unique_flat] += ... is a sequential write — cache-friendly.
            flat_idx.sort()
            unique_flat, flat_cnt = np.unique(flat_idx, return_counts=True)
            del flat_idx
            acc[unique_flat] += flat_cnt.astype(np.int32)

        # Extract the winning (src, dq) per bucket with vectorised argmax
        acc_3d = acc.reshape(n_buckets, n_src, DQ_RANGE)
        winner_dq_idx = acc_3d.argmax(axis=2)   # (n_buckets, n_src)
        winner_count = acc_3d.max(axis=2)         # (n_buckets, n_src)
        del acc, acc_3d

        # Build compact output dict — at most n_buckets × n_src entries
        bucket_counts: dict[tuple[int, int, int], int] = {}
        b_arr, src_arr = np.where(winner_count > 0)
        for b_i, src_i in zip(b_arr.tolist(), src_arr.tolist()):
            count = int(winner_count[b_i, src_i])
            dq = int(winner_dq_idx[b_i, src_i]) + dq_min   # in dquant units
            bucket_counts[(b_i, src_i, dq)] = count

        return bucket_counts

    @property
    def n_sources(self) -> int:
        return self._n_sources

    def __len__(self) -> int:
        return int(self._offsets[-1]) if self._offsets is not None else 0
