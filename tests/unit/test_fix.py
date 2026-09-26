from __future__ import annotations

import pytest

from vedit.schema import FixOp, FixPatch
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


def test_override_text_renders_single_line(edit_plan, cfg):
    edit_plan.captions[0].override_text = "custom line here"
    lines = build_lines(edit_plan, cfg)
    assert any(line.text_plain == "custom line here" for line in lines)
