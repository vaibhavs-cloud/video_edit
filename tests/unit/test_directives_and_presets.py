"""LLM-free directives (/editimage + /preset), per-visual modes, audio presets."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from vedit.schema import FixOp, FixPatch, Visual
from vedit.stage import audio as audio_stage
from vedit.stage import render
from vedit.stage.fix import FixError, apply_patch, parse_directive, parse_fix
from vedit.stage.visuals import ResolvedVisual


def _image_plan(edit_plan):
    """Screenshot visual addressable by the bot's image-3 naming."""
    plan = edit_plan.model_copy(deep=True)
    v = plan.visuals[0]
    v.kind = "screenshot"
    v.file = "image-3.png"
    return plan


def tp(transcript):
    return SimpleNamespace(words=transcript.words)


# ---- directive fast-path (no model) ----


def test_preset_directive(edit_plan, transcript, cfg):
    patch = parse_fix("preset punchy", _image_plan(edit_plan), transcript, cfg)
    assert patch.op == FixOp.set_preset
    assert patch.text == "punchy"


def test_set_visual_mode_directive(edit_plan, transcript, cfg):
    patch = parse_fix(
        "set_visual_mode image-3 adaptive", _image_plan(edit_plan), transcript, cfg
    )
    assert patch.op == FixOp.set_visual_mode
    assert patch.visual_id == edit_plan.visuals[0].id
    assert patch.mode == "adaptive"
    assert patch.crop_focus is None


def test_set_visual_mode_crop_directive(edit_plan, transcript, cfg):
    patch = parse_fix(
        "set_visual_mode image-3 cover crop top",
        _image_plan(edit_plan),
        transcript,
        cfg,
    )
    assert (patch.mode, patch.crop_focus) == ("cover", "top")


def test_resize_percent_directive(edit_plan, transcript, cfg):
    patch = parse_fix("resize image-3 to 60%", _image_plan(edit_plan), transcript, cfg)
    assert patch.op == FixOp.resize_visual
    assert patch.scale == 0.6


def test_resize_relative_directives(edit_plan, transcript, cfg):
    plan = _image_plan(edit_plan)
    plan.visuals[0].scale = 0.5
    bigger = parse_fix("resize image-3 bigger", plan, transcript, cfg)
    assert bigger.scale == pytest.approx(0.625)
    full = parse_fix("resize image-3 full", plan, transcript, cfg)
    assert full.scale == 1.0


def test_unknown_visual_directive_falls_through(edit_plan):
    assert parse_directive("set_visual_mode v99 cover", edit_plan) is None
    assert parse_directive("resize image-7 bigger", edit_plan) is None


# ---- apply path ----


def test_apply_set_visual_mode(edit_plan, transcript):
    plan = _image_plan(edit_plan)
    out, notes = apply_patch(
        FixPatch(
            op=FixOp.set_visual_mode,
            visual_id="image-3",
            mode="cover",
            crop_focus="bottom",
        ),
        plan,
        transcript,
    )
    got = out.visuals[0]
    assert got.mode == "cover"
    assert got.crop_focus == "bottom"
    assert any("set_visual_mode" in n for n in notes)


def test_apply_set_visual_mode_fit_normalizes(edit_plan, transcript):
    out, _ = apply_patch(
        FixPatch(op=FixOp.set_visual_mode, visual_id="image-3", mode="fit"),
        _image_plan(edit_plan),
        transcript,
    )
    assert out.visuals[0].mode == "adaptive"


def test_apply_set_visual_mode_rejects_icons(edit_plan, transcript):
    plan = edit_plan.model_copy(deep=True)
    plan.visuals[0].kind = "icon"
    with pytest.raises(FixError, match="images only"):
        apply_patch(
            FixPatch(op=FixOp.set_visual_mode, visual_id="v1", mode="cover"),
            plan,
            transcript,
        )


def test_apply_set_preset_known_and_unknown(edit_plan, transcript, cfg):
    presets = set(cfg.audio.presets or {})
    _out, notes = apply_patch(
        FixPatch(op=FixOp.set_preset, text="flat"),
        edit_plan,
        transcript,
        presets=presets,
    )
    assert any("set_preset" in n for n in notes)
    with pytest.raises(FixError, match="unknown preset"):
        apply_patch(
            FixPatch(op=FixOp.set_preset, text="nope"),
            edit_plan,
            transcript,
            presets=presets,
        )


# ---- per-visual render mode + crop focus ----


def _shot(tmp_path, name="img.png", size=(320, 200)):
    p = tmp_path / name
    Image.new("RGB", size, (30, 120, 200)).save(p)
    return p


def _resolved(visual, asset):
    return [ResolvedVisual(visual=visual, asset=asset)]


def test_mode_override_cover_crops_full_frame(tmp_path, cfg, transcript):
    cfg_va = cfg.visuals.screenshot_mode
    v = Visual(
        id="v1",
        kind="screenshot",
        file="img.png",
        from_word=transcript.words[0].i,
        to_word=transcript.words[8].i,
        mode="cover",
    )
    seg = (0.0, transcript.words[8].e + 0.5)
    plans = render._overlays_for_segment(
        seg[0], seg[1], _resolved(v, _shot(tmp_path)), tp(transcript), cfg
    )
    graph, _ = render._filter_for_segment(seg[0], seg[1], plans, False, cfg)
    assert "force_original_aspect_ratio=increase" in graph
    assert "crop=1080:1920:x='(iw-1080)/2'" in graph
    assert cfg_va  # base mode value exists; the override above wins regardless


def test_crop_focus_bottom_keeps_floor(tmp_path, cfg, transcript):
    v = Visual(
        id="v1",
        kind="screenshot",
        file="img.png",
        from_word=transcript.words[0].i,
        to_word=transcript.words[8].i,
        mode="cover",
        crop_focus="bottom",
    )
    seg = (0.0, transcript.words[8].e + 0.5)
    plans = render._overlays_for_segment(
        seg[0],
        seg[1],
        _resolved(v, _shot(tmp_path, size=(200, 400))),
        tp(transcript),
        cfg,
    )
    graph, _ = render._filter_for_segment(seg[0], seg[1], plans, False, cfg)
    assert "crop=1080:1920:x='(iw-1080)/2':y='ih-1920'" in graph


def test_default_mode_still_blur_backdrop(tmp_path, cfg, transcript):
    # no per-visual override -> the configured adaptive default holds
    v = Visual(
        id="v1",
        kind="screenshot",
        file="img.png",
        from_word=transcript.words[0].i,
        to_word=transcript.words[8].i,
    )
    seg = (0.0, transcript.words[8].e + 0.5)
    plans = render._overlays_for_segment(
        seg[0],
        seg[1],
        _resolved(v, _shot(tmp_path, size=(400, 200))),
        tp(transcript),
        cfg,
    )
    graph, _ = render._filter_for_segment(seg[0], seg[1], plans, False, cfg)
    assert "gblur=sigma=" in graph


# ---- audio recipes ----


def test_with_preset_applies_recipe(cfg):
    base = cfg.audio
    cfg2 = audio_stage.with_preset(cfg, "punchy")
    assert cfg2.audio.bass_boost_gain_db == 5
    assert cfg2.audio.comp_threshold_db == -20
    assert cfg2.audio.loudnorm_i == -14
    # noise cancelling is a hard default and can never be touched
    assert cfg2.audio.denoise_reduction_db == base.denoise_reduction_db


def test_with_preset_unknown_rejected(cfg):
    with pytest.raises(audio_stage.AudioError, match="unknown audio preset"):
        audio_stage.with_preset(cfg, "nope")


def test_with_preset_empty_name_is_noop(cfg):
    assert audio_stage.with_preset(cfg, "") is cfg
    assert audio_stage.with_preset(cfg, None) is cfg


def test_with_preset_banned_knob(cfg):
    import dataclasses

    cfg2 = dataclasses.replace(
        cfg,
        audio=dataclasses.replace(cfg.audio, presets={"sneaky": {"afftdn_nf": 2}}),
    )
    with pytest.raises(audio_stage.AudioError, match="may not change"):
        audio_stage.with_preset(cfg2, "sneaky")


def test_with_preset_unknown_knob(cfg):
    import dataclasses

    cfg2 = dataclasses.replace(
        cfg,
        audio=dataclasses.replace(cfg.audio, presets={"typo": {"comp_ratios": 4}}),
    )
    with pytest.raises(audio_stage.AudioError, match="unknown audio knob"):
        audio_stage.with_preset(cfg2, "typo")
