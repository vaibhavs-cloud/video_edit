from __future__ import annotations

import pytest

from vedit.schema import FixOp, FixPatch
from vedit.stage.anchor import parse_time_tokens, resolve_span
from vedit.stage.captions import build_lines
from vedit.stage.fix import FixError, apply_patch, parse_fix


def test_replace_icon_clears_and_rekeywords(edit_plan, transcript):
    v0 = edit_plan.visuals[0]
    out, notes = apply_patch(
        FixPatch(op=FixOp.replace_icon, visual_id=v0.id, keyword="shield"),
        edit_plan,
        transcript,
    )
    got = next(v for v in out.visuals if v.id == v0.id)
    assert got.keyword == "shield"
    assert got.icon is None
    assert any("replace_icon" in n for n in notes)


def test_remove_visual_patches_and_revalidates(edit_plan, transcript):
    v0 = edit_plan.visuals[0]
    out, notes = apply_patch(
        FixPatch(op=FixOp.remove_visual, visual_id=v0.id), edit_plan, transcript
    )
    assert all(v.id != v0.id for v in out.visuals)
    assert any("remove_visual" in n for n in notes)


def test_retime_inside_segment_ok(edit_plan, transcript):
    anchor = edit_plan.visuals[0].from_word
    out, notes = apply_patch(
        FixPatch(
            op=FixOp.retime_visual,
            visual_id=edit_plan.visuals[0].id,
            from_word=anchor + 1,
            to_word=anchor + 2,
        ),
        edit_plan,
        transcript,
    )
    v = out.visuals[0]
    assert (v.from_word, v.to_word) == (anchor + 1, anchor + 2)
    assert notes


def test_retime_crossing_cut_rejected(edit_plan, transcript, segments):
    if len(segments) < 2:
        pytest.skip("fixture has a single segment")
    with pytest.raises(FixError, match="cut boundary"):
        apply_patch(
            FixPatch(
                op=FixOp.retime_visual,
                visual_id=edit_plan.visuals[0].id,
                from_word=segments[0].keep_to_word - 1,
                to_word=segments[1].keep_from_word + 1,
            ),
            edit_plan,
            transcript,
        )


def test_unknown_op_lists_supported(edit_plan, transcript):
    with pytest.raises(FixError, match="supported fixes"):
        apply_patch(FixPatch(op=FixOp.unknown, note=None), edit_plan, transcript)


def test_recaption_sets_override_text(edit_plan, transcript):
    cap = edit_plan.captions[0]
    out, notes = apply_patch(
        FixPatch(op=FixOp.recaption, from_word=cap.from_word, text="brand new wording"),
        edit_plan,
        transcript,
    )
    assert out.captions[0].override_text == "brand new wording"
    assert any("recaption" in n for n in notes)


def test_recaption_without_text_rejected(edit_plan, transcript):
    with pytest.raises(FixError, match="text"):
        apply_patch(
            FixPatch(op=FixOp.recaption, from_word=edit_plan.captions[0].from_word),
            edit_plan,
            transcript,
        )


def test_parse_fix_mock_heuristics(edit_plan, transcript, cfg):
    p = parse_fix("remove the first visual", edit_plan, transcript, cfg, mock=True)
    assert p.op == FixOp.remove_visual
    p = parse_fix("use a shield instead", edit_plan, transcript, cfg, mock=True)
    assert p.op == FixOp.replace_icon and p.keyword == "shield"
    p = parse_fix("make it sparkle", edit_plan, transcript, cfg, mock=True)
    assert p.op == FixOp.unknown


def test_add_visual_places_input_image(edit_plan, transcript):
    cap = edit_plan.captions[0]
    before = len(edit_plan.visuals)
    out, notes = apply_patch(
        FixPatch(
            op=FixOp.add_visual,
            file="diagram.png",
            from_word=cap.from_word,
            to_word=cap.to_word,
        ),
        edit_plan,
        transcript,
        screenshots=["diagram.png"],
    )
    assert len(out.visuals) == before + 1
    added = out.visuals[-1]
    assert added.kind == "screenshot" and added.file == "diagram.png"
    assert (added.from_word, added.to_word) == (cap.from_word, cap.to_word)
    assert any("add_visual" in n for n in notes)


def test_add_visual_unknown_file_rejected(edit_plan, transcript):
    cap = edit_plan.captions[0]
    with pytest.raises(FixError, match="not an input image"):
        apply_patch(
            FixPatch(
                op=FixOp.add_visual,
                file="nope.png",
                from_word=cap.from_word,
                to_word=cap.to_word,
            ),
            edit_plan,
            transcript,
            screenshots=["diagram.png"],
        )


def test_add_visual_without_images_rejected(edit_plan, transcript):
    cap = edit_plan.captions[0]
    with pytest.raises(FixError, match="send images"):
        apply_patch(
            FixPatch(
                op=FixOp.add_visual,
                file="diagram.png",
                from_word=cap.from_word,
                to_word=cap.to_word,
            ),
            edit_plan,
            transcript,
            screenshots=[],
        )


def test_add_visual_crossing_cut_rejected(edit_plan, transcript, segments):
    if len(segments) < 2:
        pytest.skip("fixture has a single segment")
    with pytest.raises(FixError, match="cut boundary"):
        apply_patch(
            FixPatch(
                op=FixOp.add_visual,
                file="diagram.png",
                from_word=segments[0].keep_to_word - 1,
                to_word=segments[1].keep_from_word + 1,
            ),
            edit_plan,
            transcript,
            screenshots=["diagram.png"],
        )


def test_parse_fix_mock_place_heuristic(edit_plan, transcript, cfg):
    p = parse_fix(
        "place the diagram at the start",
        edit_plan,
        transcript,
        cfg,
        mock=True,
        screenshots=["diagram.png"],
    )
    assert p.op == FixOp.add_visual and p.file == "diagram.png"


def test_add_visual_defaults_file_and_range(edit_plan, transcript):
    out, notes = apply_patch(
        FixPatch(op=FixOp.add_visual),
        edit_plan,
        transcript,
        screenshots=["image-1.jpg"],
    )
    added = out.visuals[-1]
    seg0 = edit_plan.segments[0]
    assert added.file == "image-1.jpg"
    assert added.from_word == seg0.keep_from_word
    assert added.to_word == min(seg0.keep_from_word + 4, seg0.keep_to_word)
    assert any("defaulted" in n for n in notes)


def test_add_visual_no_images_no_file_rejected(edit_plan, transcript):
    with pytest.raises(FixError, match="send images"):
        apply_patch(
            FixPatch(op=FixOp.add_visual),
            edit_plan,
            transcript,
            screenshots=[],
        )


def test_override_text_renders_single_line(edit_plan, cfg):
    edit_plan.captions[0].override_text = "custom line here"
    lines = build_lines(edit_plan, cfg)
    assert any(line.text_plain == "custom line here" for line in lines)


def _mid_segment_time(transcript, segments) -> float:
    biggest = max(segments, key=lambda s: s.keep_to_word - s.keep_from_word)
    mid = (biggest.keep_from_word + biggest.keep_to_word) // 2
    return transcript.words[mid].s


def test_parse_fix_mock_time_overrides_guess(edit_plan, transcript, segments, cfg):
    t = _mid_segment_time(transcript, segments)
    patch = parse_fix(
        f"place diagram.png at 0:{t:04.1f}",
        edit_plan,
        transcript,
        cfg,
        mock=True,
        screenshots=["diagram.png"],
    )
    assert patch.op == FixOp.add_visual
    tokens = parse_time_tokens(f"0:{t:04.1f}", source_dur=edit_plan.source.dur)
    expected = resolve_span(
        tokens[0], transcript.words, segments, cfg.visuals.placement_span_s
    )
    assert (patch.from_word, patch.to_word) == expected
    # beats the mock's default-to-first-caption guess
    assert patch.from_word != edit_plan.captions[0].from_word


def test_parse_fix_mock_add_note_echoes_input_time(
    edit_plan, transcript, segments, cfg
):
    t = _mid_segment_time(transcript, segments)
    patch = parse_fix(
        f"place diagram.png at {t:.1f} seconds",
        edit_plan,
        transcript,
        cfg,
        mock=True,
        screenshots=["diagram.png"],
    )
    out, notes = apply_patch(patch, edit_plan, transcript, screenshots=["diagram.png"])
    assert out.visuals[-1].file == "diagram.png"
    assert any("input " in n and "words " in n for n in notes)


def test_parse_fix_mock_retime_uses_last_time(edit_plan, transcript, segments, cfg):
    patch = parse_fix(
        "retime the icon: move the visual at 0:01 to 0:20",
        edit_plan,
        transcript,
        cfg,
        mock=True,
    )
    assert patch.op == FixOp.retime_visual
    expected = resolve_span(
        20.0, transcript.words, segments, cfg.visuals.placement_span_s
    )
    assert (patch.from_word, patch.to_word) == expected


def test_parse_fix_mock_recaption_anchors_time(edit_plan, transcript, segments, cfg):
    patch = parse_fix(
        "recaption 0:10: hello there",
        edit_plan,
        transcript,
        cfg,
        mock=True,
    )
    assert patch.op == FixOp.recaption
    anchor, _ = resolve_span(
        10.0, transcript.words, segments, cfg.visuals.placement_span_s
    )
    assert patch.from_word == anchor
    out, _notes = apply_patch(patch, edit_plan, transcript)
    assert any(c.override_text == "0:10: hello there" for c in out.captions)


def test_parse_fix_mock_trailing_use_a_no_crash(edit_plan, transcript, cfg):
    patch = parse_fix("please use a ", edit_plan, transcript, cfg, mock=True)
    assert patch.op == FixOp.replace_icon and patch.keyword == "icon"


def test_parse_fix_no_time_keeps_mock_guess(edit_plan, transcript, cfg):
    patch = parse_fix(
        "place the diagram at the start",
        edit_plan,
        transcript,
        cfg,
        mock=True,
        screenshots=["diagram.png"],
    )
    cap = edit_plan.captions[0]
    assert (patch.from_word, patch.to_word) == (cap.from_word, cap.to_word)


def _cuttable_range(edit_plan, transcript):
    seg = max(edit_plan.segments, key=lambda s: s.keep_to_word - s.keep_from_word)
    lo = seg.keep_from_word + 1
    hi = min(lo + 2, seg.keep_to_word - 1)
    assert hi > lo, "fixture needs a roomy segment"
    return lo, hi


def test_cut_range_splits_segment_and_repairs(edit_plan, transcript):
    lo, hi = _cuttable_range(edit_plan, transcript)
    visuals_before = len(edit_plan.visuals)
    out, notes = apply_patch(
        FixPatch(op=FixOp.cut_range, from_word=lo, to_word=hi),
        edit_plan,
        transcript,
    )
    assert all(v.to_word < lo or v.from_word > hi for v in out.visuals), (
        "no visual may touch the removed range"
    )
    assert all(c.to_word < lo or c.from_word > hi for c in out.captions), (
        "no caption may touch the removed range"
    )
    assert all(z < lo or z > hi for z in out.zoom_at_words)
    for seg in out.segments:
        assert seg.keep_to_word < lo or seg.keep_from_word > hi
    assert len(out.visuals) <= visuals_before
    assert any("cut words" in n for n in notes)


def test_cut_range_reversed_normalized(edit_plan, transcript):
    lo, hi = _cuttable_range(edit_plan, transcript)
    out, _notes = apply_patch(
        FixPatch(op=FixOp.cut_range, from_word=hi, to_word=lo),
        edit_plan,
        transcript,
    )
    for seg in out.segments:
        assert seg.keep_to_word < lo or seg.keep_from_word > hi


def test_cut_range_that_empties_everything_refused(edit_plan, transcript):
    with pytest.raises(FixError, match="whole video"):
        apply_patch(
            FixPatch(
                op=FixOp.cut_range,
                from_word=0,
                to_word=len(transcript.words) - 1,
            ),
            edit_plan,
            transcript,
        )


def test_cut_range_out_of_bounds_rejected(edit_plan, transcript):
    with pytest.raises(FixError, match="out of bounds"):
        apply_patch(
            FixPatch(
                op=FixOp.cut_range,
                from_word=len(transcript.words) + 5,
                to_word=len(transcript.words) + 9,
            ),
            edit_plan,
            transcript,
        )


def test_keep_range_cuts_outside(edit_plan, transcript):
    lo, hi = _cuttable_range(edit_plan, transcript)
    out, notes = apply_patch(
        FixPatch(op=FixOp.keep_range, from_word=lo, to_word=hi),
        edit_plan,
        transcript,
    )
    for seg in out.segments:
        assert seg.keep_from_word >= lo and seg.keep_to_word <= hi
    assert any("keep_range" in n for n in notes)


def test_add_and_remove_zoom(edit_plan, transcript):
    w = edit_plan.segments[0].keep_from_word
    out, notes = apply_patch(
        FixPatch(op=FixOp.add_zoom, from_word=w, to_word=w),
        edit_plan,
        transcript,
        max_zooms=99,
    )
    assert w in out.zoom_at_words
    assert any("add_zoom" in n for n in notes)
    out2, notes2 = apply_patch(
        FixPatch(op=FixOp.remove_zoom, from_word=w, to_word=w),
        out,
        transcript,
    )
    assert w not in out2.zoom_at_words
    assert any("remove_zoom" in n for n in notes2)


def test_add_zoom_cap_and_duplicates(edit_plan, transcript):
    w = edit_plan.segments[0].keep_from_word
    with pytest.raises(FixError, match="zoom cap"):
        apply_patch(
            FixPatch(op=FixOp.add_zoom, from_word=w, to_word=w),
            edit_plan,
            transcript,
            max_zooms=0,
        )
    out, _notes = apply_patch(
        FixPatch(op=FixOp.add_zoom, from_word=w, to_word=w),
        edit_plan,
        transcript,
        max_zooms=None,
    )
    assert w in out.zoom_at_words
    _out2, notes2 = apply_patch(
        FixPatch(op=FixOp.add_zoom, from_word=w, to_word=w),
        out,
        transcript,
        max_zooms=None,
    )
    assert any("already present" in n for n in notes2)


def test_remove_zoom_without_target_rejected(edit_plan, transcript):
    with pytest.raises(FixError, match="needs a time"):
        apply_patch(
            FixPatch(op=FixOp.remove_zoom, from_word=None, to_word=None),
            edit_plan,
            transcript,
        )


def test_invalid_patch_wrapped_as_fix_error(edit_plan, transcript, monkeypatch):
    import pydantic

    import vedit.stage.fix as fix_stage

    class _BadPlan:
        @staticmethod
        def model_validate(_dump):
            raise pydantic.ValidationError.from_exception_data(
                "EditPlan",
                [
                    {
                        "type": "value_error",
                        "loc": ("segments",),
                        "input": [],
                        "ctx": {"error": ValueError("boom")},
                    }
                ],
            )

    monkeypatch.setattr(fix_stage, "EditPlan", _BadPlan)
    with pytest.raises(FixError, match="invalid plan"):
        apply_patch(
            FixPatch(op=FixOp.remove_visual, visual_id=edit_plan.visuals[0].id),
            edit_plan,
            transcript,
        )


def test_parse_fix_mock_cut_keep_zoom_heuristics(edit_plan, transcript, cfg):
    p = parse_fix("cut 0:05 to 0:10", edit_plan, transcript, cfg, mock=True)
    assert p.op == FixOp.cut_range
    lo, hi = p.from_word, p.to_word
    assert 0 <= lo <= hi < len(transcript.words)
    p = parse_fix("keep only 0:00 to 0:20", edit_plan, transcript, cfg, mock=True)
    assert p.op == FixOp.keep_range
    p = parse_fix("zoom in at 0:15", edit_plan, transcript, cfg, mock=True)
    assert p.op == FixOp.add_zoom
    p = parse_fix("remove the zoom at 0:15", edit_plan, transcript, cfg, mock=True)
    assert p.op == FixOp.remove_zoom


def test_parse_fix_mock_cut_uses_time_points(edit_plan, transcript, segments, cfg):
    from vedit.stage.anchor import resolve_point

    p = parse_fix("cut 0:05 to 0:10", edit_plan, transcript, cfg, mock=True)
    assert (p.from_word, p.to_word) == (
        resolve_point(5.0, transcript.words, segments),
        resolve_point(10.0, transcript.words, segments),
    )


def test_parse_fix_remove_visual_still_wins(edit_plan, transcript, cfg):
    p = parse_fix("remove the first visual", edit_plan, transcript, cfg, mock=True)
    assert p.op == FixOp.remove_visual


def test_resize_visual_by_id_and_scale(edit_plan, transcript):
    v0 = edit_plan.visuals[0]
    out, notes = apply_patch(
        FixPatch(op=FixOp.resize_visual, visual_id=v0.id, scale=0.5),
        edit_plan,
        transcript,
    )
    got = next(v for v in out.visuals if v.id == v0.id)
    assert got.scale == 0.5
    assert any("resize_visual" in n and "0.50" in n for n in notes)


def test_resize_visual_out_of_range_rejected(edit_plan, transcript):
    v0 = edit_plan.visuals[0]
    with pytest.raises(FixError, match="invalid plan"):
        apply_patch(
            FixPatch(op=FixOp.resize_visual, visual_id=v0.id, scale=5.0),
            edit_plan,
            transcript,
        )


def test_resize_visual_needs_target_and_size(edit_plan, transcript):
    with pytest.raises(FixError, match="which visual"):
        apply_patch(FixPatch(op=FixOp.resize_visual, scale=0.5), edit_plan, transcript)
    v0 = edit_plan.visuals[0]
    with pytest.raises(FixError, match="how small"):
        apply_patch(
            FixPatch(op=FixOp.resize_visual, visual_id=v0.id), edit_plan, transcript
        )


def test_resize_visual_targets_covering_word(edit_plan, transcript):
    v0 = edit_plan.visuals[0]
    mid = (v0.from_word + v0.to_word) // 2
    out, _notes = apply_patch(
        FixPatch(op=FixOp.resize_visual, from_word=mid, to_word=mid, scale=0.6),
        edit_plan,
        transcript,
    )
    got = next(v for v in out.visuals if v.id == v0.id)
    assert got.scale == 0.6


def test_parse_fix_mock_resize_heuristic(edit_plan, transcript, cfg):
    p = parse_fix(
        "make the image smaller at 0:15",
        edit_plan,
        transcript,
        cfg,
        mock=True,
        screenshots=["diagram.png"],
    )
    assert p.op == FixOp.resize_visual
    assert p.scale == 0.7


def test_preview_fix_place_reports_both_timelines(edit_plan, transcript, segments, cfg):
    from vedit.stage.fix import confirmation_text, preview_fix

    t = _mid_segment_time(transcript, segments)
    preview = preview_fix(
        f"place diagram.png at {t:.1f} seconds",
        edit_plan,
        transcript,
        cfg,
        mock=True,
        screenshots=["diagram.png"],
    )
    assert preview["ok"] is True
    assert preview["op"] == "add_visual"
    assert preview["input_range_s"][0] == pytest.approx(t, abs=0.6)
    assert "output_range_s" in preview and "phrase" in preview
    assert preview["duration"]["before_s"] >= preview["duration"]["after_s"]
    text = confirmation_text(preview)
    assert "input " in text and "final video" in text and "YES" in text


def test_preview_fix_cut_reports_cascade(edit_plan, transcript, segments, cfg):
    from vedit.stage.fix import preview_fix

    lo, hi = _cuttable_range(edit_plan, transcript)
    t0 = transcript.words[lo].s
    t1 = transcript.words[hi].s
    preview = preview_fix(
        f"cut {t0:.1f}s to {t1:.1f}s",
        edit_plan,
        transcript,
        cfg,
        mock=True,
    )
    assert preview["ok"] is True
    assert preview["op"] == "cut_range"
    assert (
        preview["affected"]["segments_after"] >= preview["affected"]["segments_before"]
    )
    assert preview["duration"]["after_s"] < preview["duration"]["before_s"]


def test_preview_fix_unknown_instruction_not_ok(edit_plan, transcript, cfg):
    from vedit.stage.fix import confirmation_text, preview_fix

    preview = preview_fix("make it sparkle", edit_plan, transcript, cfg, mock=True)
    assert preview["ok"] is False and "error" in preview
    assert "can't do that" in confirmation_text(preview)


def test_plan_hash_stable(edit_plan):
    from vedit.stage.fix import plan_hash

    assert plan_hash(edit_plan) == plan_hash(edit_plan.model_copy(deep=True))


def test_cli_fix_dry_run_writes_preview_only(tmp_path, edit_plan, transcript):
    import json
    from pathlib import Path

    from vedit.cli import main
    from vedit.schema import StateMeta

    root = Path(__file__).resolve().parent.parent.parent
    state = tmp_path / "dryrun"
    state.mkdir()
    (state / "edit_plan.json").write_text(edit_plan.model_dump_json(), encoding="utf-8")
    (state / "transcript.json").write_text(
        transcript.model_dump_json(), encoding="utf-8"
    )
    (state / "segments.json").write_text(
        json.dumps([s.model_dump() for s in edit_plan.segments]), encoding="utf-8"
    )
    meta = StateMeta(
        ref="t",
        sha256="0" * 64,
        source_kind="local",
        source_ref="",
        chat_id="",
        mock=True,
    )
    (state / "meta.json").write_text(meta.model_dump_json(), encoding="utf-8")
    before_plan = (state / "edit_plan.json").read_bytes()
    before_seg = (state / "segments.json").read_bytes()

    rc = main(
        [
            "fix",
            "--state",
            str(state),
            "--instruction",
            "cut 0:05 to 0:10",
            "--dry-run",
            "--config",
            str(root / "config.yaml"),
        ]
    )
    assert rc == 0
    data = json.loads((state / "fix_preview.json").read_text(encoding="utf-8"))
    assert data["ok"] is True and data["op"] == "cut_range"
    assert (state / "edit_plan.json").read_bytes() == before_plan
    assert (state / "segments.json").read_bytes() == before_seg
    assert list(state.glob(".done.*")) == []


def test_cli_fix_apply_preview_refuses_on_drift(tmp_path, edit_plan, transcript):
    import json
    from pathlib import Path

    from vedit.cli import main
    from vedit.schema import StateMeta

    root = Path(__file__).resolve().parent.parent.parent
    state = tmp_path / "drift"
    state.mkdir()
    (state / "edit_plan.json").write_text(edit_plan.model_dump_json(), encoding="utf-8")
    (state / "transcript.json").write_text(
        transcript.model_dump_json(), encoding="utf-8"
    )
    meta = StateMeta(
        ref="t",
        sha256="0" * 64,
        source_kind="local",
        source_ref="",
        chat_id="",
        mock=True,
    )
    (state / "meta.json").write_text(meta.model_dump_json(), encoding="utf-8")
    (state / "fix_preview.json").write_text(
        json.dumps({"plan_hash": "stale", "patch": {"op": "cut_range"}}),
        encoding="utf-8",
    )
    with pytest.raises(SystemExit, match="plan changed"):
        main(
            [
                "fix",
                "--state",
                str(state),
                "--apply-preview",
                "--config",
                str(root / "config.yaml"),
            ]
        )
