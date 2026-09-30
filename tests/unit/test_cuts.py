from __future__ import annotations

import pytest

from vedit import ff
from vedit.schema import Word
from vedit.stage.cuts import CutsError, _merge_tiny, compute_segments, correct_words


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


def _w(i: int, s: float, e: float, t: str = "w") -> Word:
    return Word(i=i, t=t, s=s, e=e)


def test_correct_words_shrinks_stretched_span(cfg):
    words = [_w(0, 10.0, 10.4, "a"), _w(1, 10.4, 13.0, "b"), _w(2, 13.0, 13.4, "c")]
    fixed, stats = correct_words(words, [(11.0, 12.6)], cfg.cuts)
    assert stats == {"shrunk": 1, "dropped": 0}
    assert (fixed[1].s, fixed[1].e) == (10.4, 10.92)
    assert [w.i for w in fixed] == [0, 1, 2]


def test_correct_words_drops_zero_energy_word(cfg):
    words = [_w(0, 19.0, 19.4, "a"), _w(1, 19.5, 20.6, "x"), _w(2, 20.7, 21.0, "b")]
    fixed, stats = correct_words(words, [(19.2, 20.7)], cfg.cuts)
    assert stats["dropped"] == 1
    assert [w.t for w in fixed] == ["a", "b"]
    assert [w.i for w in fixed] == [0, 1]


def test_correct_words_keeps_sliver_of_speech(cfg):
    words = [_w(0, 30.0, 30.2, "a"), _w(1, 30.2, 31.6, "y")]
    fixed, stats = correct_words(words, [(30.0, 31.0)], cfg.cuts)
    assert stats == {"shrunk": 1, "dropped": 0}
    assert fixed[1].s == pytest.approx(31.08)
    assert fixed[1].e == 31.6


def test_correct_words_keeps_short_jitter_word(cfg):
    words = [_w(0, 40.0, 40.3, "z")]
    fixed, stats = correct_words(words, [(39.0, 41.0)], cfg.cuts)
    assert stats == {"shrunk": 0, "dropped": 0}
    assert (fixed[0].s, fixed[0].e) == (40.0, 40.3)


def test_correct_words_without_silences_is_identity(cfg):
    words = _words([_burst(1.0)])
    fixed, stats = correct_words(words, [], cfg.cuts)
    assert stats == {"shrunk": 0, "dropped": 0}
    assert [w.i for w in fixed] == [w.i for w in words]
    for f, w in zip(fixed, words):
        assert f.s == pytest.approx(w.s) and f.e == pytest.approx(w.e)


def test_detect_silences_parses_and_merges(monkeypatch, tmp_path):
    out = (
        "[Parsed_silencedetect_0 @ x] silence_start: 1.0\n"
        "[Parsed_silencedetect_0 @ x] silence_end: 1.5 | silence_duration: 0.5\n"
        "[Parsed_silencedetect_0 @ x] silence_start: 1.6\n"
        "[Parsed_silencedetect_0 @ x] silence_end: 2.2 | silence_duration: 0.6\n"
        "[Parsed_silencedetect_0 @ x] silence_start: 5.0\n"
        "[Parsed_silencedetect_0 @ x] silence_end: 5.8 | silence_duration: 0.8\n"
    )
    monkeypatch.setattr(ff, "run", lambda *a, **k: out)
    got = ff.detect_silences(tmp_path / "x.wav", -25.0, 0.4, 0.25)
    assert got == [(1.0, 2.2), (5.0, 5.8)]


def test_merge_tiny_folds_toward_smaller_re_admitted_gap():
    # island at [6.0, 6.4] between runs: left gap 1.0s, right gap 0.2s
    segs = [(0.0, 5.0), (6.0, 6.4), (6.6, 12.0)]
    assert _merge_tiny(segs, 0.5) == [(0.0, 5.0), (6.0, 12.0)]


def test_merge_tiny_folds_left_when_left_gap_smaller():
    # left gap 0.2s < right gap 1.4s -> fold into the left neighbour
    segs = [(0.0, 5.0), (5.2, 5.6), (7.0, 12.0)]
    assert _merge_tiny(segs, 0.5) == [(0.0, 5.6), (7.0, 12.0)]


def test_merge_tiny_epsilon_boundary_keeps_segment():
    # 0.4999999s reads below min_dur but within eps -> must NOT be re-glued
    segs = [(0.0, 5.0), (6.0, 6.4999999), (7.0, 12.0)]
    assert _merge_tiny(segs, 0.5) == segs


def test_merge_tiny_leading_island_folds_forward():
    segs = [(0.0, 0.4), (1.4, 6.0)]
    assert _merge_tiny(segs, 0.5) == [(0.0, 6.0)]


def test_merge_tiny_multiple_islands_drain():
    segs = [(0.0, 5.0), (5.2, 5.6), (6.0, 6.1), (7.0, 12.0)]
    merged = _merge_tiny(segs, 0.5)
    assert len(merged) == 2
    assert all(e - s >= 0.5 - 1e-6 for s, e in merged)
