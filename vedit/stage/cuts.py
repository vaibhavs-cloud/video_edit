"""Deterministic silence-cut logic.

The LLM never produces a cut timestamp. Cuts are derived purely from
word-level transcript timings:

    gap >= cut_gap_ms                 -> cut
    gap <= keep_gap_ms                -> keep
    in between                        -> adaptive vs speaking rate (median gap * multiplier)

Because whisper stretches word timings across real pauses (hiding them from
the gap rules), word spans are first cross-checked against energy-based
silence intervals (`correct_words`): the silent part is subtracted and the
longest speaking piece kept; words with no energy anywhere in their span are
dropped as phantom timestamps. Cut boundaries sit in the silence *between* words,
padded by pad_ms, so they can never clip speech. Leading/trailing dead air
beyond pre/post roll is also trimmed. All functions here are pure —
easy to unit test.
"""

from __future__ import annotations

import statistics
from dataclasses import replace

from vedit.config import CutsCfg
from vedit.schema import Segment, Word


class CutsError(ValueError):
    pass


def correct_words(
    words: list[Word], silences: list[tuple[float, float]], cfg: CutsCfg
) -> tuple[list[Word], dict]:
    """Shrink word spans around detected silences; drop phantom words.

    Returns (corrected_words, {"shrunk": n, "dropped": n}). Indices are
    reassigned 0..n so every downstream stage stays consistent — call this
    before anything else consumes word indices.
    """
    pad = cfg.silence_pad_ms / 1000.0
    spans = [(max(0.0, a - pad), b + pad) for a, b in silences if b > a]
    kept: list[Word] = []
    shrunk = dropped = 0
    for w in words:
        dur = w.e - w.s
        pieces = [(w.s, w.e)]
        for a, b in spans:
            rest: list[tuple[float, float]] = []
            for s, e in pieces:
                if b <= s or a >= e:
                    rest.append((s, e))
                    continue
                if a > s:
                    rest.append((s, min(a, e)))
                if b < e:
                    rest.append((max(b, s), e))
            pieces = rest
        pieces = [(s, e) for s, e in pieces if e - s >= 0.02]
        if not pieces:
            # No energy anywhere in the span: a phantom timestamp, unless the
            # word is so short this may be detector jitter.
            if dur >= cfg.word_drop_min_s:
                dropped += 1
                continue
            kept.append(Word(i=len(kept), s=w.s, e=w.e, t=w.t))
            continue
        best = max(pieces, key=lambda p: p[1] - p[0])
        if (best[0], best[1]) != (w.s, w.e):
            shrunk += 1
        kept.append(Word(i=len(kept), s=round(best[0], 3), e=round(best[1], 3), t=w.t))
    return kept, {"shrunk": shrunk, "dropped": dropped}


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
    """Folding sub-minimum segments into their neighbour keeps the cut rhythm natural.

    An island between two longer runs folds into whichever side re-admits less
    of the silence the cut just removed — never blindly left, which could
    re-span a pause on the larger of the two gaps.

    The epsilon matters: bounds are float arithmetic on rounded word times, so
    a 0.70s island must not read as 0.6999s and get glued back (re-admitting
    the pause the cut just removed).
    """
    if not segments:
        return segments
    eps = 1e-6
    segs = list(segments)
    # every pass removes exactly one island, so this terminates
    while len(segs) > 1:
        idx = next((i for i, s in enumerate(segs) if s[1] - s[0] < min_dur - eps), None)
        if idx is None:
            break
        seg = segs[idx]
        left_gap = (seg[0] - segs[idx - 1][1]) if idx > 0 else float("inf")
        right_gap = (segs[idx + 1][0] - seg[1]) if idx + 1 < len(segs) else float("inf")
        if right_gap < left_gap:
            segs[idx + 1] = (seg[0], segs[idx + 1][1])
        elif idx > 0:
            segs[idx - 1] = (segs[idx - 1][0], seg[1])
        else:
            segs[idx + 1] = (seg[0], segs[idx + 1][1])
        del segs[idx]
    return segs


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
        relaxed = replace(cfg, cut_gap_ms=int(cfg.cut_gap_ms * 1.6))
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
