"""Screenshots-only mode (visuals.icons_enabled=false): icons are dropped
before they can reach the render, and input images resolve from attachments."""

from __future__ import annotations

import pytest
from PIL import Image

from vedit.schema import DraftVisual, FixOp, FixPatch, PlanDraft, Visual
from vedit.stage import visuals as visuals_stage
from vedit.stage.fix import FixError, apply_patch
from vedit.stage.plan import _build_prompt, assemble_plan


def _meta(source_dur):
    return {"ref": "t", "sha256": "0" * 64, "w": 1280, "h": 720, "dur": source_dur}


def test_assemble_drops_icons_keeps_screenshots(cfg, transcript, segments, source_dur):
    assert cfg.visuals.icons_enabled is False
    w = segments[0].keep_from_word
    draft = PlanDraft(
        visuals=[
            DraftVisual(kind="icon", keyword="api", from_word=w, to_word=w + 2),
            DraftVisual(
                kind="screenshot", file="diagram.png", from_word=w, to_word=w + 2
            ),
        ],
        captions=[],
        zoom_at_words=[],
    )
    built, notes = assemble_plan(
        draft, transcript, segments, cfg, "", _meta(source_dur), False, ""
    )
    assert [v.kind for v in built.visuals] == ["screenshot"]
    assert any("icons disabled" in n for n in notes)


def test_assemble_keeps_icons_when_enabled(cfg_icons, transcript, segments, source_dur):
    w = segments[0].keep_from_word
    draft = PlanDraft(
        visuals=[
            DraftVisual(kind="icon", keyword="api", from_word=w, to_word=w + 2),
        ],
        captions=[],
        zoom_at_words=[],
    )
    built, _ = assemble_plan(
        draft, transcript, segments, cfg_icons, "", _meta(source_dur), False, ""
    )
    assert [v.kind for v in built.visuals] == ["icon"]


def test_plan_prompt_is_screenshots_only_when_icons_off(cfg, transcript, segments):
    prompt = _build_prompt(transcript, segments, ["diagram.png"], cfg, "", 65.0)
    assert "never kind" in prompt and "screenshot" in prompt
    assert '- kind "icon"' not in prompt


def test_plan_prompt_allows_icons_when_enabled(cfg_icons, transcript, segments):
    prompt = _build_prompt(transcript, segments, [], cfg_icons, "", 65.0)
    assert 'kind "icon"' in prompt


def test_replace_icon_rejected_when_icons_off(edit_plan, transcript):
    v0 = edit_plan.visuals[0]
    with pytest.raises(FixError, match="icons are disabled"):
        apply_patch(
            FixPatch(op=FixOp.replace_icon, visual_id=v0.id, keyword="shield"),
            edit_plan,
            transcript,
            icons_enabled=False,
        )


def test_resolve_screenshot_from_attachments(tmp_path, cfg, transcript, segments):
    Image.new("RGB", (64, 64), (10, 20, 30)).save(tmp_path / "diagram.png")
    visual = Visual(
        id="v1",
        kind="screenshot",
        file="diagram.png",
        from_word=segments[0].keep_from_word,
        to_word=segments[0].keep_from_word + 2,
    )
    resolved = visuals_stage.resolve_visuals(
        [visual], transcript, cfg, tmp_path / "icons", tmp_path
    )
    assert len(resolved) == 1
    assert resolved[0].asset == tmp_path / "diagram.png"
