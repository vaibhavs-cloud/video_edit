"""Source-time <-> output-time mapping.

edit_plan.json lives entirely in source time; only this map (derived
deterministically from the cut list) translates a timestamp into the
concatenated output timeline. Corrections always address source time.
"""

from __future__ import annotations

from bisect import bisect_right

from vedit.schema import Segment


class TimelineMap:
    def __init__(self, segments: list[Segment]):
        if not segments:
            raise ValueError("TimelineMap needs at least one segment")
        self._starts = [s.start for s in segments]
        self._ends = [s.end for s in segments]
        self._offsets: list[float] = []
        total = 0.0
        for s in segments:
            self._offsets.append(total)
            total += s.end - s.start
        self.output_dur = total
        self.source_dur = segments[-1].end

    def to_output(self, t: float) -> float:
        idx = bisect_right(self._starts, t) - 1
        if idx < 0:
            return 0.0
        if t <= self._ends[idx]:
            return self._offsets[idx] + (t - self._starts[idx])
        # inside a removed gap -> collapses to the moment the next segment starts
        if idx + 1 < len(self._offsets):
            return self._offsets[idx + 1]
        return self.output_dur

    def to_source(self, o: float) -> float:
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
