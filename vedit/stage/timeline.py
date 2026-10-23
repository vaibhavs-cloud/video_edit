"""Source-time <-> output-time mapping.

edit_plan.json lives entirely in source time; only this map (derived
deterministically from the cut list + inserted clips) translates a timestamp
into the concatenated output timeline. Corrections always address source time.
"""

from __future__ import annotations

from bisect import bisect_right

from vedit.schema import Insert, Segment, Word, insert_anchor


def crossfade_joins(
    segments: list[Segment], inserts: list[Insert], words: list[Word]
) -> int:
    """Segment joins that still crossfade (an insert at a boundary cuts it)."""
    joins = max(0, len(segments) - 1)
    if not inserts:
        return joins
    boundaries = {ins.from_word for ins in inserts if ins.from_word < len(words)}
    blocked = sum(1 for seg in segments[1:] if seg.keep_from_word in boundaries)
    return max(0, joins - blocked)


class TimelineMap:
    def __init__(
        self,
        segments: list[Segment],
        inserts: list[Insert] | None = None,
        words: list[Word] | None = None,
    ):
        if not segments:
            raise ValueError("TimelineMap needs at least one segment")
        if inserts and not words:
            raise ValueError("TimelineMap needs words to place inserts")
        self.segments = list(segments)
        self.inserts = list(inserts or [])
        self._starts = [s.start for s in segments]
        self._ends = [s.end for s in segments]
        self._offsets: list[float] = []
        total = 0.0
        for s in segments:
            self._offsets.append(total)
            total += s.end - s.start
        self.base_output_dur = total
        self.source_dur = segments[-1].end

        # output anchor of each insert, in plan order, together with its span
        anchors = [self._anchor(ins, words or [], segments) for ins in self.inserts]
        ordered = sorted(range(len(self.inserts)), key=lambda i: anchors[i])
        self._insert_order = ordered
        self._insert_out: list[tuple[float, float, float]] = []
        shift = 0.0
        for i in ordered:
            start = self._base_to_output(anchors[i]) + shift
            self._insert_out.append(
                (start, start + self.inserts[i].duration, anchors[i])
            )
            shift += self.inserts[i].duration
        self._insert_ends = [span[1] for span in self._insert_out]
        self.output_dur = total + shift

    @staticmethod
    def _anchor(ins: Insert, words: list[Word], segments: list[Segment]) -> float:
        return insert_anchor(ins, words, segments)

    def _base_to_output(self, t: float) -> float:
        idx = bisect_right(self._starts, t) - 1
        if idx < 0:
            return 0.0
        if t <= self._ends[idx]:
            return self._offsets[idx] + (t - self._starts[idx])
        # inside a removed gap -> collapses to the moment the next segment starts
        if idx + 1 < len(self._offsets):
            return self._offsets[idx + 1]
        return self.base_output_dur

    def _base_to_source(self, o: float) -> float:
        idx = bisect_right(self._offsets, o) - 1
        if idx < 0:
            return self._starts[0]
        within = o - self._offsets[idx]
        seg_end = self._offsets[idx] + (self._ends[idx] - self._starts[idx])
        if o >= seg_end:
            if idx + 1 < len(self._starts):
                return self._starts[idx + 1]
            return self._ends[idx]
        return self._starts[idx] + within

    def to_output(self, t: float) -> float:
        out = self._base_to_output(t)
        for a, _b, anchor in self._insert_out:
            if anchor <= t:
                out += _b - a
        return out

    def to_source(self, o: float) -> float:
        # inside an inserted clip -> pin to its source anchor
        for a, b, anchor in self._insert_out:
            if a <= o < b:
                return anchor
        # subtract clips that finish before this output instant
        shift = 0.0
        for a, b, _anchor in self._insert_out:
            if b <= o:
                shift += b - a
        return self._base_to_source(max(0.0, o - shift))

    def insert_aabb(self) -> list[tuple[str, float, float]]:
        """[(insert_id, output_start, output_end)] sorted by output time."""
        pairs = list(zip(self._insert_order, self._insert_out, strict=True))
        pairs.sort(key=lambda p: p[1][0])
        return [(self.inserts[idx].id, span[0], span[1]) for idx, span in pairs]
