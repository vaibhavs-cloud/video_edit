from __future__ import annotations

import itertools

from vedit.stage import captions
from vedit.stage.timeline import TimelineMap


def test_lines_never_cross_a_cut(plan, cfg):
    built, _ = plan
    lines = captions.build_lines(built, cfg)
    assert lines
    for line in lines:
        seg = {built.segment_of_word(wi) for wi in line.word_indices}
        assert len(seg) == 1
        assert line.word_indices == tuple(
            range(line.word_indices[0], line.word_indices[-1] + 1)
        )


def test_lines_are_monotonic_and_non_overlapping(plan, cfg):
    built, _ = plan
    lines = captions.build_lines(built, cfg)
    for prev, cur in itertools.pairwise(lines):
        assert cur.start >= prev.end
        assert cur.end > cur.start


def test_output_times_match_timeline(plan, cfg):
    built, _ = plan
    timeline = TimelineMap(built.segments)
    lines = captions.build_lines(built, cfg)
    first = lines[0]
    assert first.start == timeline.to_output(built.words[first.word_indices[0]].s)
    assert first.end >= timeline.to_output(built.words[first.word_indices[-1]].e)


def test_coverage_is_full_for_full_spans(plan):
    built, _ = plan
    assert captions.coverage(built) == 1.0


def test_emphasis_gets_ass_markup(plan, cfg):
    built, _ = plan
    lines = captions.build_lines(built, cfg)
    emphasized = [
        l
        for l in lines
        if any(
            i in {e for c in built.captions for e in c.emphasis} for i in l.word_indices
        )
    ]
    assert emphasized, "fixture has emphasis words"
    assert any(cfg.captions.emphasis_color in l.text_ass for l in emphasized)


def test_write_srt_and_ass(plan, cfg, tmp_path):
    built, _ = plan
    srt, ass, lines = captions.generate(built, cfg, tmp_path)
    srt_text = srt.read_text(encoding="utf-8")
    ass_text = ass.read_text(encoding="utf-8")
    assert srt_text.count("-->") == len(lines)
    assert ass_text.count("Dialogue:") == len(lines)
    assert f"PlayResX: {cfg.video.width}" in ass_text
    assert "Style: Cap," in ass_text
    assert "-->" in srt_text and "," in srt_text
