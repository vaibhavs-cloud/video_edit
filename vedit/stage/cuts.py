"""Deterministic silence-cut logic.

The LLM never produces a cut timestamp. Cuts are derived purely from
word-level transcript timings:

    gap >= cut_gap_ms                 -> cut
    gap <= keep_gap_ms                -> keep
    in between                        -> adaptive vs speaking rate (median gap * multiplier)

Cut boundaries sit in the silence *between* words, padded by pad_ms, so they
can never clip speech. Leading/trailing dead air beyond pre/post roll is also
trimmed. All functions here are pure — easy to unit test.
"""

from __future__ import annotations

import statistics

from vedit.config import CutsCfg
from vedit.schema import Segment, Word


class CutsError(ValueError):
    pass


def _cut_flags(words: list[Word], cfg: CutsCfg) -> list[bool]:
    """cut_flags[i] == True means a cut between words[i] and words[i+1]."""
    gaps = [max(0.0, words[i + 1].s - words[i].e) for i in range(len(words) - 1)]
    if not gaps:
        return []

    cut_s = cfg.cut_gap_ms / 1000.0
    keep_s = cfg.keep_gap_ms / 1000.0
    mid = [g for g in gaps if keep_s < g < cut_s]
    if mid:
        threshold = min(
            cut_s, max(keep_s, statistics.median(mid) * cfg.adaptive_multiplier)
        )
    else:
        threshold = cut_s
    return [g >= threshold for g in gaps]


def _merge_tiny(
    segments: list[tuple[float, float]], min_dur: float
) -> list[tuple[float, float]]:
    """Folding sub-minimum segments into their neighbour keeps the cut rhythm natural."""
    if not segments:
        return segments
    merged = [segments[0]]
    for seg in segments[1:]:
        if seg[1] - seg[0] < min_dur or merged[-1][1] - merged[-1][0] < min_dur:
            merged[-1] = (merged[-1][0], seg[1])
        else:
            merged.append(seg)
    return merged


def _intervals(
    words: list[Word], cfg: CutsCfg, dur: float, cut_flags: list[bool]
) -> tuple[list[tuple[float, float]], float]:
    pad = cfg.pad_ms / 1000.0
    pre = cfg.pre_roll_ms / 1000.0
    post = cfg.post_roll_ms / 1000.0

    start = max(0.0, words[0].s - pre)
    end = min(dur, words[-1].e + post)
    if end <= start:
        raise CutsError("transcript does not fit inside the media duration")

    cuts: list[tuple[float, float]] = []
    for i, is_cut in enumerate(cut_flags):
        if not is_cut:
            continue
        c_start = words[i].e + pad
        c_end = words[i + 1].s - pad
        if c_end <= c_start:
            continue
        cuts.append((max(start, c_start), min(end, c_end)))

    segments: list[tuple[float, float]] = []
    cursor = start
    for c_start, c_end in cuts:
        if c_start > cursor:
            segments.append((cursor, c_start))
        cursor = max(cursor, c_end)
    if cursor < end:
        segments.append((cursor, end))

    segments = [s for s in segments if s[1] > s[0]]
    segments = _merge_tiny(segments, cfg.min_kept_segment_ms / 1000.0)
    kept = sum(e - s for s, e in segments)
    return segments, kept


def compute_segments(words: list[Word], dur: float, cfg: CutsCfg) -> list[Segment]:
    if not words:
        raise CutsError("no speech words in transcript")
    words = sorted(words, key=lambda w: (w.s, w.e))

    intervals, kept = _intervals(words, cfg, dur, _cut_flags(words, cfg))
    if kept < cfg.min_output_s:
        # Too aggressive for this recording: cut only unambiguous dead air once.
        relaxed = CutsCfg(
            cut_gap_ms=int(cfg.cut_gap_ms * 1.6),
            keep_gap_ms=cfg.keep_gap_ms,
            pad_ms=cfg.pad_ms,
            min_kept_segment_ms=cfg.min_kept_segment_ms,
            min_output_s=cfg.min_output_s,
            adaptive_multiplier=cfg.adaptive_multiplier,
            pre_roll_ms=cfg.pre_roll_ms,
            post_roll_ms=cfg.post_roll_ms,
        )
        intervals, kept = _intervals(words, relaxed, dur, _cut_flags(words, relaxed))

    segments: list[Segment] = []
    for start, end in intervals:
        inside = [w for w in words if start <= w.s and w.e <= end]
        if not inside:
            continue
        segments.append(
            Segment(
                keep_from_word=inside[0].i,
                keep_to_word=inside[-1].i,
                start=round(start, 3),
                end=round(end, 3),
            )
        )
    if not segments:
        raise CutsError("no segments survived cutting")
    return segments
