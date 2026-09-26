from __future__ import annotations

import pytest

from vedit.schema import Word
from vedit.stage.cuts import CutsError, compute_segments


def _words(spans: list[tuple[float, list[tuple[float, float]]]]) -> list[Word]:
    """spans: [(start, [(offset, dur), ...]), ...] flattened into indexed words."""
    words: list[Word] = []
    for start, items in spans:
        for offset, dur in items:
            s = start + offset
            words.append(Word(i=len(words), t=f"w{len(words)}", s=s, e=s + dur))
    return words


def _burst(
    start: float, n: int = 5, step: float = 0.3, dur: float = 0.28
) -> tuple[float, list[tuple[float, float]]]:
    return start, [(i * step, dur) for i in range(n)]


def test_long_gaps_are_cut_small_gaps_kept(cfg):
    words = _words([_burst(1.0), _burst(5.0), _burst(11.0), _burst(15.0)])
    # gaps: 1.6s (cut), 2.7s (cut), 1.6s (cut)
    segs = compute_segments(words, 20.0, cfg.cuts)
    assert len(segs) == 4


def test_boundaries_sit_inside_silence(cfg):
    words = _words([_burst(1.0), _burst(3.0)])
    # gap between word ends: 1.0 + 4*0.3 + 0.28 = 2.48 -> next start 3.0 => 0.52s gap
    segs = compute_segments(words, 8.0, cfg.cuts)
    assert len(segs) == 1  # 0.52s is below the adaptive threshold
    gap_words = [w for w in words if w.i in (4, 5)]
    assert segs[0].start <= gap_words[0].e
    assert segs[0].end >= gap_words[1].s


def test_pre_and_post_roll_trim_dead_air(cfg):
    words = _words([_burst(2.0)])
    segs = compute_segments(words, 10.0, cfg.cuts)
    assert segs[0].start == pytest.approx(2.0 - cfg.cuts.pre_roll_ms / 1000)
    assert segs[0].end == pytest.approx(words[-1].e + cfg.cuts.post_roll_ms / 1000)


def test_cuts_never_clip_speech(cfg):
    words = _words([_burst(1.0), _burst(6.0)])
    segs = compute_segments(words, 12.0, cfg.cuts)
    for seg in segs:
        for w in words:
            if seg.keep_from_word <= w.i <= seg.keep_to_word:
                assert seg.start <= w.s and w.e <= seg.end


def test_relaxes_when_output_would_be_too_short(cfg):
    # 3 words with big gaps: aggressive cutting would leave < min_output_s
    words = _words([_burst(1.0, n=3), _burst(4.0, n=3), _burst(7.0, n=3)])
    segs = compute_segments(words, 12.0, cfg.cuts)
    kept = sum(s.end - s.start for s in segs)
    assert kept >= cfg.cuts.min_kept_segment_ms / 1000
    assert all(
        len([w for w in words if s.keep_from_word <= w.i <= s.keep_to_word]) >= 1
        for s in segs
    )


def test_no_words_raises(cfg):
    with pytest.raises(CutsError):
        compute_segments([], 10.0, cfg.cuts)


def test_contiguous_word_indices_required_by_schema(cfg):
    words = _words([_burst(1.0)])
    segs = compute_segments(words, 5.0, cfg.cuts)
    assert segs[0].keep_from_word == 0
    assert segs[0].keep_to_word == len(words) - 1
