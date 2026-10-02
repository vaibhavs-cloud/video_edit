"""Deterministic timestamp -> transcript word-span resolution.

Owner instructions name times on the INPUT video timeline ("place image 1 at
0:30"). The LLM picks *which* op / file / visual, but mapping a time to exact
word indices is pure code here: parse explicit time tokens, anchor to the
containing (or nearest kept) word, and expand to a phrase span inside one kept
segment. Shared by the fix loop (fix.py) and initial planning (plan.py).
"""

from __future__ import annotations

import re

from vedit.schema import Segment, Word

_MMSS = re.compile(r"(?<!\d)(\d+):(\d{1,2}(?:\.\d+)?)(?!\d)")
_NSEC = re.compile(
    r"(?<!\d)(\d+(?:\.\d+)?)\s*(?:s|sec|secs|second|seconds)\b", re.IGNORECASE
)


class AnchorError(RuntimeError):
    pass


def parse_time_tokens(text: str, source_dur: float = float("inf")) -> list[float]:
    """Explicit timestamps in an instruction, in input-video seconds.

    Understands ``0:30`` / ``1:05.5`` and ``30s`` / ``30 sec`` / ``30 seconds``.
    Bare numbers are ignored (they may be word indices or image numbers like
    "image 1"). Returns values in instruction order, clamped to the media.
    """
    spans: list[tuple[int, int, float]] = []
    for m in _MMSS.finditer(text or ""):
        spans.append((m.start(), m.end(), int(m.group(1)) * 60 + float(m.group(2))))
    for m in _NSEC.finditer(text or ""):
        if any(s < m.end() and m.start() < e for s, e, _ in spans):
            continue  # e.g. the "30s" inside an already-matched "0:30s"
        spans.append((m.start(), m.end(), float(m.group(1))))
    spans.sort()
    out: list[float] = []
    for _, _, value in spans:
        clamped = max(0.0, min(value, source_dur))
        if not out or out[-1] != clamped:
            out.append(clamped)
    return out


def _segment_index(segments: list[Segment], wi: int) -> int | None:
    for idx, seg in enumerate(segments):
        if seg.keep_from_word <= wi <= seg.keep_to_word:
            return idx
    return None


def resolve_point(t: float, words: list[Word], segments: list[Segment]) -> int:
    """Nearest kept word index to an input-video timestamp.

    Anchor = the word containing ``t``, else the nearest word by edge
    distance; an anchor in a cut gap snaps to the nearest kept word.
    Used by cut/keep/zoom ops, which address points rather than spans.
    """
    n = len(words)
    if n == 0:
        raise AnchorError("no transcript words to anchor to")
    t = max(0.0, min(t, words[-1].e))
    anchor: int | None = None
    for i, w in enumerate(words):
        if w.s <= t <= w.e:
            anchor = i
            break
    if anchor is None:
        anchor = min(
            range(n), key=lambda i: min(abs(words[i].s - t), abs(words[i].e - t))
        )
    if segments and _segment_index(segments, anchor) is None:
        kept = [
            i
            for seg in segments
            for i in range(seg.keep_from_word, seg.keep_to_word + 1)
            if 0 <= i < n
        ]
        if not kept:
            raise AnchorError("no kept words to anchor to")
        anchor = min(kept, key=lambda i: min(abs(words[i].s - t), abs(words[i].e - t)))
    return anchor


def resolve_span(
    t: float, words: list[Word], segments: list[Segment], span_s: float
) -> tuple[int, int]:
    """Map an input-video timestamp to a word span inside one kept segment.

    Anchor = the word containing ``t``, else the nearest word by edge distance.
    An anchor landing in a cut gap snaps to the nearest kept word. The span
    then expands forward up to ``span_s`` seconds (backward if the anchor is
    the segment's last word). Guarantees ``hi > lo`` unless the whole segment
    is a single word, which callers treat as unplaceable.
    """
    n = len(words)
    if n == 0:
        raise AnchorError("no transcript words to anchor to")
    t = max(0.0, min(t, words[-1].e))

    anchor: int | None = None
    for i, w in enumerate(words):
        if w.s <= t <= w.e:
            anchor = i
            break
    if anchor is None:
        anchor = min(
            range(n),
            key=lambda i: min(abs(words[i].s - t), abs(words[i].e - t)),
        )

    seg_idx = _segment_index(segments, anchor) if segments else None
    if segments and seg_idx is None:
        kept = [
            i
            for seg in segments
            for i in range(seg.keep_from_word, seg.keep_to_word + 1)
            if 0 <= i < n
        ]
        if not kept:
            raise AnchorError("no kept words to anchor to")
        anchor = min(kept, key=lambda i: min(abs(words[i].s - t), abs(words[i].e - t)))
        seg_idx = _segment_index(segments, anchor)

    lo = hi = anchor
    if seg_idx is not None:
        seg = segments[seg_idx]
        while (
            hi + 1 <= seg.keep_to_word
            and hi + 1 < n
            and words[hi + 1].s - words[lo].s < span_s
        ):
            hi += 1
        if hi == lo and lo - 1 >= seg.keep_from_word:
            lo -= 1  # anchor is the segment's last word: grow backward
    else:
        while hi + 1 < n and words[hi + 1].s - words[lo].s < span_s:
            hi += 1
    return lo, min(hi, n - 1)
