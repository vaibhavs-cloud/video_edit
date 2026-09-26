from __future__ import annotations

from vedit.schema import DraftCaption, DraftVisual, PlanDraft
from vedit.stage.plan import assemble_plan


def test_visual_crossing_cut_is_dropped_with_note(
    cfg, transcript, segments, source_dur
):
    crossing = DraftVisual(
        kind="icon",
        keyword="api",
        from_word=segments[0].keep_to_word - 2,
        to_word=segments[1].keep_from_word + 2,
        pos="top",
    )
    draft = PlanDraft(visuals=[crossing], captions=[], zoom_at_words=[])
    built, notes = assemble_plan(
        draft,
        transcript,
        segments,
        cfg,
        "",
        {"ref": "t", "sha256": "0" * 64, "w": 10, "h": 10, "dur": source_dur},
        False,
        "",
    )
    assert built.visuals == []
    assert any("crosses a cut" in n for n in notes)


def test_caption_crossing_cut_is_split(cfg, transcript, segments, source_dur):
    crossing = DraftCaption(
        from_word=segments[0].keep_to_word - 1,
        to_word=segments[1].keep_from_word + 1,
        emphasis=[],
    )
    draft = PlanDraft(visuals=[], captions=[crossing], zoom_at_words=[])
    built, _ = assemble_plan(
        draft,
        transcript,
        segments,
        cfg,
        "",
        {"ref": "t", "sha256": "0" * 64, "w": 10, "h": 10, "dur": source_dur},
        False,
        "",
    )
    spans = [c for c in built.captions if c.from_word >= segments[0].keep_to_word - 1]
    assert len(spans) == 2
    assert spans[0].to_word == segments[0].keep_to_word
    assert spans[1].from_word == segments[1].keep_from_word
    for cap in built.captions:
        assert built.segment_of_word(cap.from_word) == built.segment_of_word(
            cap.to_word
        )


def test_no_captions_degrades_to_full_coverage(cfg, transcript, segments, source_dur):
    draft = PlanDraft(visuals=[], captions=[], zoom_at_words=[])
    built, _notes = assemble_plan(
        draft,
        transcript,
        segments,
        cfg,
        "",
        {"ref": "t", "sha256": "0" * 64, "w": 10, "h": 10, "dur": source_dur},
        False,
        "",
    )
    assert built.degraded is True
    covered = {wi for c in built.captions for wi in range(c.from_word, c.to_word + 1)}
    assert covered == set(range(len(transcript.words)))


def test_zoom_words_capped_and_valid(cfg, transcript, segments, source_dur):
    draft = PlanDraft(
        visuals=[],
        captions=[],
        zoom_at_words=list(range(len(transcript.words))),
    )
    built, _ = assemble_plan(
        draft,
        transcript,
        segments,
        cfg,
        "",
        {"ref": "t", "sha256": "0" * 64, "w": 10, "h": 10, "dur": source_dur},
        False,
        "",
    )
    limit = max(1, len(segments) // cfg.video.max_zoom_segments_div)
    assert len(built.zoom_at_words) <= limit
    assert all(0 <= z < len(transcript.words) for z in built.zoom_at_words)


def test_reframe_mode_picks_crop_for_landscape_source(cfg, transcript, segments):
    draft = PlanDraft(visuals=[], captions=[], zoom_at_words=[])
    built, _ = assemble_plan(
        draft,
        transcript,
        segments,
        cfg,
        "",
        {"ref": "t", "sha256": "0" * 64, "w": 1920, "h": 1080, "dur": 100.0},
        False,
        "",
    )
    assert built.reframe.mode == "crop"
