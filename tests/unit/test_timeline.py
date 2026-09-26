from __future__ import annotations

import pytest

from vedit.schema import Segment
from vedit.stage.timeline import TimelineMap


@pytest.fixture
def timeline() -> TimelineMap:
    return TimelineMap(
        [
            Segment(keep_from_word=0, keep_to_word=3, start=1.0, end=3.0),
            Segment(keep_from_word=4, keep_to_word=7, start=5.0, end=8.0),
        ]
    )


def test_output_offsets_accumulate(timeline):
    assert timeline.output_dur == pytest.approx(5.0)
    assert timeline.to_output(1.0) == pytest.approx(0.0)
    assert timeline.to_output(3.0) == pytest.approx(2.0)
    assert timeline.to_output(5.0) == pytest.approx(2.0)
    assert timeline.to_output(8.0) == pytest.approx(5.0)


def test_time_inside_gap_collapses_to_next_segment(timeline):
    # 4.0s sits in the removed gap -> output time of the next segment start
    assert timeline.to_output(4.0) == pytest.approx(2.0)
    assert timeline.to_output(4.999) == pytest.approx(2.0)


def test_before_first_segment_clamps_to_zero(timeline):
    assert timeline.to_output(0.0) == pytest.approx(0.0)


def test_roundtrip_inside_segments(timeline):
    for source_t in (1.0, 2.37, 5.0, 6.11, 7.99):
        out = timeline.to_output(source_t)
        back = timeline.to_source(out)
        assert back == pytest.approx(source_t, abs=1e-6)


def test_after_last_segment_clamps(timeline):
    assert timeline.to_output(99.0) == pytest.approx(timeline.output_dur)
