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


def _insert_words():
    from vedit.schema import Word

    starts = [1.0, 1.5, 2.0, 2.5, 5.0, 5.5, 6.0, 7.5]
    return [Word(i=i, t=f"w{i}", s=s, e=s + 0.3) for i, s in enumerate(starts)]


def _segments():
    return [
        Segment(keep_from_word=0, keep_to_word=3, start=1.0, end=3.0),
        Segment(keep_from_word=4, keep_to_word=7, start=5.0, end=8.0),
    ]


def _insert(**kw):
    from vedit.schema import Insert

    data = {
        "id": "i1",
        "file": "inserts/clip-1.mp4",
        "from_word": 4,
        "duration": 2.0,
    }
    data.update(kw)
    return Insert(**data)


def test_insert_shifts_later_output_and_stretches_duration():
    timeline = TimelineMap(_segments(), [_insert()], _insert_words())
    assert timeline.output_dur == pytest.approx(7.0)
    assert timeline.to_output(4.0) == pytest.approx(2.0)  # before insert
    assert timeline.to_output(5.0) == pytest.approx(4.0)  # insert first
    assert timeline.to_output(5.5) == pytest.approx(4.5)


def test_output_inside_insert_pins_to_anchor_and_roundtrips_after():
    timeline = TimelineMap(_segments(), [_insert()], _insert_words())
    assert timeline.to_source(3.0) == pytest.approx(5.0)
    assert timeline.to_source(4.0) == pytest.approx(5.0)
    assert timeline.to_source(4.5) == pytest.approx(5.5)


def test_insert_at_end_adds_tail_without_shifting_words():
    words = _insert_words()
    timeline = TimelineMap(_segments(), [_insert(from_word=8)], words)
    assert timeline.to_output(words[-1].e) == pytest.approx(4.8)
    assert timeline.to_output(8.0) == pytest.approx(7.0)


def test_multiple_inserts_at_same_anchor_stack_in_plan_order():
    timeline = TimelineMap(
        _segments(),
        [_insert(id="i1", duration=1.0), _insert(id="i2", duration=3.0)],
        _insert_words(),
    )
    assert timeline.output_dur == pytest.approx(9.0)
    assert timeline.insert_aabb() == [
        ("i1", 2.0, 3.0),
        ("i2", 3.0, 6.0),
    ]
    assert timeline.to_source(3.5) == pytest.approx(5.0)
    assert timeline.to_source(6.5) == pytest.approx(5.5)
