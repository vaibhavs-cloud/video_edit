from __future__ import annotations

import pytest

from vedit.schema import FixOp, FixPatch, PlanDraft
from vedit.stage import fix as fix_stage
from vedit.stage.anchor import resolve_point
from vedit.stage.plan import assemble_plan
from vedit.stage.render import _segment_tokens
from vedit.stage.timeline import TimelineMap, crossfade_joins

META = {"ref": "t", "sha256": "0" * 64, "w": 10, "h": 10}


def _assemble(cfg, transcript, segments, source_dur, clips):
    draft = PlanDraft(visuals=[], captions=[], zoom_at_words=[])
    return assemble_plan(
        draft,
        transcript,
        segments,
        cfg,
        "",
        {**META, "dur": source_dur},
        False,
        "",
        clips=clips,
    )


def _clip(**kw):
    data = {"id": "clip-1", "file": "inserts/clip-1.mp4", "source_dur": 30.0}
    data.update(kw)
    return data


def _apply(cfg, plan, transcript, patch, clips):
    return fix_stage.apply_patch(
        patch,
        plan,
        transcript,
        icons_enabled=False,
        screenshots=[],
        max_zooms=1,
        presets=set(),
        clips=clips,
        insert_cfg=cfg.inserts,
    )


def test_plan_insert_uses_defaults_and_audio(cfg, transcript, segments, source_dur):
    at = transcript.words[4].s
    built, notes = _assemble(cfg, transcript, segments, source_dur, [_clip(at=at)])
    assert [i.id for i in built.inserts] == ["clip-1"]
    ins = built.inserts[0]
    assert ins.from_word == resolve_point(at, transcript.words, segments)
    assert ins.duration == cfg.inserts.default_s
    assert ins.muted is (not cfg.inserts.audio)
    assert ins.fit == cfg.inserts.fit
    assert any("insert clip-1" in n for n in notes)


def test_plan_insert_is_capped_by_source_minus_trim(
    cfg, transcript, segments, source_dur
):
    clip = _clip(trim_start=26.0, duration=50.0, source_dur=30.0)
    built, _ = _assemble(cfg, transcript, segments, source_dur, [clip])
    assert built.inserts[0].duration == pytest.approx(4.0)


def test_plan_drops_unusable_clip_with_note(cfg, transcript, segments, source_dur):
    clip = _clip(trim_start=30.0, duration=5.0, source_dur=30.0)
    built, notes = _assemble(cfg, transcript, segments, source_dur, [clip])
    assert built.inserts == []
    assert any("no usable duration" in n for n in notes)


def test_plan_anchor_time_in_a_cut_snaps_to_kept_word(
    cfg, transcript, segments, source_dur
):
    gap = (transcript.words[segments[0].keep_to_word].e + segments[1].start) / 2
    expected = resolve_point(gap, transcript.words, segments)
    built, _ = _assemble(cfg, transcript, segments, source_dur, [_clip(at=gap)])
    assert built.inserts[0].from_word == expected


def test_boundary_insert_starts_at_segment_start_and_kills_its_crossfade(
    cfg, transcript, segments, source_dur
):
    w = segments[1].keep_from_word
    built, _ = _assemble(
        cfg, transcript, segments, source_dur, [_clip(at=transcript.words[w].s)]
    )
    ins = built.inserts[0]
    assert ins.from_word == w
    assert built.insert_anchor_s(ins) == pytest.approx(segments[1].start)

    tokens = _segment_tokens(built, 1)
    assert tokens[0] == ("insert", -1.0, -1.0, 0)

    timeline = TimelineMap(built.segments, built.inserts, built.words)
    first_len = built.segments[0].end - built.segments[0].start
    assert timeline.insert_aabb()[0][1] == pytest.approx(first_len)
    assert crossfade_joins(built.segments, built.inserts, built.words) == max(
        0, len(built.segments) - 2
    )


def test_fix_insert_then_retime_trim_fit_audio_remove(cfg, edit_plan, transcript):
    clips = [_clip(source_dur=12.0)]
    w1 = edit_plan.segments[0].keep_from_word
    plan1, _ = _apply(
        cfg,
        edit_plan,
        transcript,
        FixPatch(op=FixOp.insert_video, file="clip-1", from_word=w1, duration=9),
        clips,
    )
    assert plan1.inserts[0].duration == pytest.approx(9.0)

    w2 = edit_plan.segments[-1].keep_to_word
    plan2, _ = _apply(
        cfg,
        plan1,
        transcript,
        FixPatch(op=FixOp.retime_insert, insert_id="clip-1", from_word=w2),
        clips,
    )
    assert plan2.inserts[0].from_word == w2

    plan3, _ = _apply(
        cfg,
        plan2,
        transcript,
        FixPatch(op=FixOp.trim_insert, insert_id="clip-1", trim_start=10.0),
        clips,
    )
    assert plan3.inserts[0].trim_start == pytest.approx(10.0)
    assert plan3.inserts[0].duration == pytest.approx(2.0)

    plan4, _ = _apply(
        cfg,
        plan3,
        transcript,
        FixPatch(op=FixOp.set_insert_fit, insert_id="clip-1", fit="cover"),
        clips,
    )
    assert plan4.inserts[0].fit == "cover"

    plan5, _ = _apply(
        cfg,
        plan4,
        transcript,
        FixPatch(op=FixOp.set_insert_audio, insert_id="clip-1", muted=True),
        clips,
    )
    assert plan5.inserts[0].muted is True

    plan6, _ = _apply(
        cfg,
        plan5,
        transcript,
        FixPatch(op=FixOp.remove_insert, insert_id="clip-1"),
        clips,
    )
    assert plan6.inserts == []


def test_fix_reinsert_same_clip_gets_unique_id(cfg, edit_plan, transcript):
    clips = [_clip()]
    w = edit_plan.segments[0].keep_from_word
    plan1, _ = _apply(
        cfg,
        edit_plan,
        transcript,
        FixPatch(op=FixOp.insert_video, file="clip-1", from_word=w),
        clips,
    )
    plan2, _ = _apply(
        cfg,
        plan1,
        transcript,
        FixPatch(op=FixOp.insert_video, file="clip-1", from_word=w),
        clips,
    )
    assert [i.id for i in plan2.inserts] == ["clip-1", "clip-1-2"]


def test_fix_insert_unknown_clip_is_a_clean_error(cfg, edit_plan, transcript):
    with pytest.raises(fix_stage.FixError):
        _apply(
            cfg,
            edit_plan,
            transcript,
            FixPatch(op=FixOp.insert_video, file="missing", from_word=0),
            [],
        )


def test_fix_directives_parse_without_llm(cfg, edit_plan, transcript):
    p = fix_stage.parse_fix(
        "insert_video clip-1 at 0:10 for 5s", edit_plan, transcript, cfg
    )
    assert p.op == FixOp.insert_video
    assert p.file == "clip-1"
    assert p.from_word is not None
    assert p.duration == pytest.approx(5.0)

    p = fix_stage.parse_fix("retime_insert clip-1 at 0:20", edit_plan, transcript, cfg)
    assert p.op == FixOp.retime_insert and p.from_word is not None

    p = fix_stage.parse_fix("trim_insert clip-1 to 3s", edit_plan, transcript, cfg)
    assert p.op == FixOp.trim_insert and p.duration == pytest.approx(3.0)

    p = fix_stage.parse_fix("insert_video video 1 at 0:10", edit_plan, transcript, cfg)
    assert p.op == FixOp.insert_video and p.file == "video 1"
    assert p.from_word is not None

    p = fix_stage.parse_fix("remove_insert clip-1", edit_plan, transcript, cfg)
    assert p.op == FixOp.remove_insert and p.insert_id == "clip-1"
    p = fix_stage.parse_fix("remove_insert video 1", edit_plan, transcript, cfg)
    assert p.op == FixOp.remove_insert and p.insert_id == "video 1"

    p = fix_stage.parse_fix("set_insert_fit clip-1 fill", edit_plan, transcript, cfg)
    assert p.op == FixOp.set_insert_fit and p.fit == "cover"

    p = fix_stage.parse_fix("set_insert_audio clip-1 mute", edit_plan, transcript, cfg)
    assert p.op == FixOp.set_insert_audio and p.muted is True


def test_preview_insert_reports_new_clip_and_duration(cfg, edit_plan, transcript):
    clips = [_clip(source_dur=12.0)]
    preview = fix_stage.preview_fix(
        "insert_video clip-1 at 0:10 for 6s",
        edit_plan,
        transcript,
        cfg,
        screenshots=[],
        clips=clips,
    )
    assert preview["ok"] is True
    assert preview["op"] == "insert_video"
    assert preview["duration"]["after_s"] > preview["duration"]["before_s"]
    aff = preview["affected"]
    assert aff["inserts_after"] == aff["inserts_before"] + 1
    assert "inserted videos" in fix_stage.confirmation_text(preview)


def test_parse_clips_requires_a_file_id():
    from vedit.cli import _parse_clips

    clips = _parse_clips('[{"file_id": "abc", "at": 10}, {"nope": 1}]')
    assert clips == [{"file_id": "abc", "at": 10}]
    with pytest.raises(SystemExit):
        _parse_clips("{not json")
