from __future__ import annotations

import pytest

from vedit.schema import Segment, Word
from vedit.stage.anchor import (
    AnchorError,
    parse_time_tokens,
    resolve_point,
    resolve_span,
)


def _words() -> list[Word]:
    # 10 words, one per second; word 5 sits in a cut gap (see _segments).
    return [Word(i=i, t=f"w{i}", s=float(i), e=float(i) + 0.5) for i in range(10)]


def _segments() -> list[Segment]:
    return [
        Segment(keep_from_word=0, keep_to_word=4, start=0.0, end=4.5),
        Segment(keep_from_word=6, keep_to_word=9, start=6.0, end=9.5),
    ]


def test_parse_mmss_forms():
    assert parse_time_tokens("place image 1 at 0:20") == [20.0]
    assert parse_time_tokens("move the visual at 0:20 to 0:30") == [20.0, 30.0]
    assert parse_time_tokens("at 1:05") == [65.0]
    assert parse_time_tokens("at 1:05.5") == [65.5]


def test_parse_n_seconds_forms():
    assert parse_time_tokens("at 30 seconds") == [30.0]
    assert parse_time_tokens("at 30s") == [30.0]
    assert parse_time_tokens("at 30 sec") == [30.0]
    assert parse_time_tokens("put it at 0.5s") == [0.5]


def test_parse_ignores_bare_numbers_and_image_ids():
    assert parse_time_tokens("place image 1 at the start") == []
    assert parse_time_tokens("put it at 30") == []
    assert parse_time_tokens("no times here") == []


def test_parse_no_double_count_and_clamp():
    assert parse_time_tokens("at 0:30s") == [30.0]
    assert parse_time_tokens("at 5:00", source_dur=29.5) == [29.5]
    assert parse_time_tokens("at 0:20 then at 0:20") == [20.0]


def test_resolve_mid_word_expands_forward():
    assert resolve_span(2.3, _words(), _segments(), 2.5) == (2, 4)


def test_resolve_exact_boundary():
    assert resolve_span(2.0, _words(), _segments(), 2.5) == (2, 4)


def test_resolve_gap_snaps_to_nearest_kept():
    # t=5.2 is inside gap word 5; nearest kept word is 4 (segment A end).
    assert resolve_span(5.2, _words(), _segments(), 2.5) == (3, 4)


def test_resolve_clamps_out_of_range():
    assert resolve_span(99.0, _words(), _segments(), 2.5) == (8, 9)
    assert resolve_span(-3.0, _words(), _segments(), 2.5) == (0, 2)


def test_resolve_without_segments_expands_on_time_only():
    assert resolve_span(2.0, _words(), [], 2.5) == (2, 4)


def test_resolve_empty_transcript_raises():
    with pytest.raises(AnchorError):
        resolve_span(1.0, [], _segments(), 2.5)


def test_resolve_single_word_segment_returns_degenerate_span():
    words = [Word(i=0, t="solo", s=1.0, e=1.4)]
    segs = [Segment(keep_from_word=0, keep_to_word=0, start=1.0, end=1.4)]
    assert resolve_span(1.2, words, segs, 2.5) == (0, 0)


def test_resolve_point_containing_word():
    assert resolve_point(2.3, _words(), _segments()) == 2


def test_resolve_point_gap_snaps_to_nearest_kept():
    assert resolve_point(5.2, _words(), _segments()) == 4
    assert resolve_point(5.8, _words(), _segments()) == 6


def test_resolve_point_clamps():
    assert resolve_point(99.0, _words(), _segments()) == 9
    assert resolve_point(-3.0, _words(), _segments()) == 0


def test_resolve_point_empty_raises():
    with pytest.raises(AnchorError):
        resolve_point(1.0, [], _segments())
