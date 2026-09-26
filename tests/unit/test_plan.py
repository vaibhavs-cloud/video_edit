from __future__ import annotations

import time

from vedit.schema import DraftCaption, DraftVisual, PlanDraft
from vedit.stage import plan as plan_stage
from vedit.stage.plan import _fallback_draft, assemble_plan, draft_plan


def test_visual_crossing_cut_is_dropped_with_note(
    cfg_icons, transcript, segments, source_dur
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
        cfg_icons,
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


def test_fallback_draft_validates(transcript, segments):
    draft = _fallback_draft(transcript, segments)
    assert isinstance(draft, PlanDraft)
    assert len(draft.captions) == len(segments)
    assert draft.visuals == []
    assert draft.zoom_at_words == []
    assert all(isinstance(c, DraftCaption) for c in draft.captions), (
        "fallback must emit PlanDraft's own caption type"
    )


def test_draft_plan_degrades_instead_of_crashing(
    monkeypatch, cfg, transcript, segments
):
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setattr(
        plan_stage,
        "generate_json",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("quota")),
    )
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    draft, degraded, note = draft_plan(transcript, segments, [], cfg, "", 65.0)
    assert degraded is True
    assert isinstance(draft, PlanDraft)
    assert len(draft.captions) == len(segments)
    assert "plan failed" in note


def _meta(source_dur):
    return {"ref": "t", "sha256": "0" * 64, "w": 10, "h": 10, "dur": source_dur}


def test_single_word_visual_is_widened_not_crashed(
    cfg_icons, transcript, segments, source_dur
):
    w = segments[0].keep_from_word + 1
    draft = PlanDraft(
        visuals=[DraftVisual(kind="icon", keyword="focus", from_word=w, to_word=w)],
        captions=[],
        zoom_at_words=[],
    )
    built, _ = assemble_plan(
        draft, transcript, segments, cfg_icons, "", _meta(source_dur), False, ""
    )
    assert len(built.visuals) == 1
    v = built.visuals[0]
    assert v.from_word == w and v.to_word == w + 1


def test_reversed_and_out_of_range_drafts_never_crash(
    cfg_icons, transcript, segments, source_dur
):
    w = segments[0].keep_from_word + 2
    draft = PlanDraft(
        visuals=[
            DraftVisual(kind="icon", keyword="x", from_word=w, to_word=1),
            DraftVisual(
                kind="icon",
                keyword="y",
                from_word=len(transcript.words) + 5,
                to_word=len(transcript.words) + 9,
            ),
        ],
        captions=[
            DraftCaption(from_word=9, to_word=3, emphasis=[99]),
            DraftCaption(
                from_word=segments[0].keep_from_word,
                to_word=segments[0].keep_from_word + 2,
                emphasis=[5000],
            ),
        ],
        zoom_at_words=[99999],
    )
    built, _ = assemble_plan(
        draft, transcript, segments, cfg_icons, "", _meta(source_dur), False, ""
    )
    assert all(v.to_word > v.from_word for v in built.visuals)
    assert all(c.from_word <= c.to_word for c in built.captions)
    assert all(
        c.from_word <= e <= c.to_word for c in built.captions for e in c.emphasis
    )
    assert all(0 <= z < len(transcript.words) for z in built.zoom_at_words)
