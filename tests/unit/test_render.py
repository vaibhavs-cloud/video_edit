"""Cover-mode screenshots and subtle per-visual transitions."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
from PIL import Image

from vedit.schema import Visual
from vedit.stage import render
from vedit.stage.visuals import ResolvedVisual


def _shot(tmp_path: Path, name: str = "img.png") -> Path:
    p = tmp_path / name
    Image.new("RGB", (320, 200), (30, 120, 200)).save(p)
    return p


def _visuals(n: int, transcript) -> list:
    out = []
    for i in range(n):
        a = transcript.words[i * 2].i
        b = transcript.words[i * 2 + 1].i
        out.append(
            Visual(
                id=f"v{i + 1}",
                kind="screenshot",
                file="img.png",
                from_word=a,
                to_word=b,
            )
        )
    return out


def _cover_cfg(cfg):
    """Cover-mode variant: these tests pin the crop-to-fill behaviour."""
    return dataclasses.replace(
        cfg, visuals=dataclasses.replace(cfg.visuals, screenshot_mode="cover")
    )


def test_cover_fills_frame_and_cycles_transitions(tmp_path, cfg, transcript):
    cfg = _cover_cfg(cfg)
    asset = _shot(tmp_path)
    visuals = _visuals(4, transcript)
    resolved = [ResolvedVisual(visual=v, asset=asset) for v in visuals]
    seg = (0.0, transcript.words[8].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), cfg
    )
    assert len(plans) == 4
    assert [(p.x, p.y, p.w, p.h) for p in plans] == [(0, 0, 1080, 1920)] * 4
    assert [p.fx for p in plans] == ["fade", "rise", "drift", "fade"]


def transcript_plan(transcript):
    # _overlays_for_segment only needs plan.words
    from types import SimpleNamespace

    return SimpleNamespace(words=transcript.words)


def test_filter_has_cover_chain_and_motion(tmp_path, cfg, transcript):
    cfg = _cover_cfg(cfg)
    asset = _shot(tmp_path)
    resolved = [ResolvedVisual(visual=v, asset=asset) for v in _visuals(3, transcript)]
    seg = (0.0, transcript.words[8].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), cfg
    )
    graph, _ = render._filter_for_segment(seg[0], seg[1], plans, False, cfg)
    assert "force_original_aspect_ratio=increase" in graph
    assert "crop=1080:1920" in graph
    assert "max(0,1920*(1-(t-" in graph  # rise
    assert "min(1,(t-" in graph  # drift
    assert "overlay=x=0:y=0:" in graph  # fade cover at origin


def test_empty_transitions_falls_back_to_fade(tmp_path, cfg, transcript):
    import dataclasses

    cfg2 = dataclasses.replace(
        cfg,
        visuals=dataclasses.replace(
            cfg.visuals, transitions=[], screenshot_mode="cover"
        ),
    )
    asset = _shot(tmp_path)
    resolved = [ResolvedVisual(visual=_visuals(1, transcript)[0], asset=asset)]
    seg = (0.0, transcript.words[4].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), cfg2
    )
    assert [p.fx for p in plans] == ["fade"]


def test_cover_filters_execute_in_ffmpeg(tmp_path, cfg, transcript):
    """Escaping bugs in overlay expressions only surface at render time."""
    from vedit import ff

    cfg = _cover_cfg(cfg)
    asset = _shot(tmp_path)
    resolved = [ResolvedVisual(visual=v, asset=asset) for v in _visuals(3, transcript)]
    seg = (0.0, transcript.words[8].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), cfg
    )
    assert {p.fx for p in plans} == {"fade", "rise", "drift"}
    graph, vlabel = render._filter_for_segment(seg[0], seg[1], plans, False, cfg)
    base = tmp_path / "base.mp4"
    ff.ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x240:rate=10:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            str(base),
        ],
        timeout=120,
    )
    args = ["-i", str(base)]
    args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=3"]
    for ov in plans:
        args += ["-loop", "1", "-framerate", "10", "-t", "3", "-i", str(ov.path)]
    args += [
        "-filter_complex",
        graph,
        "-map",
        vlabel,
        "-map",
        "[aout]",
        "-t",
        "2",
        str(tmp_path / "o.mp4"),
    ]
    # the point of this test is graph evaluation, not encoding quality:
    # ultrafast keeps the 1080p encode inside the 300s budget on loaded CI
    args += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "30"]
    ff.ffmpeg(args, timeout=300)
    assert (tmp_path / "o.mp4").stat().st_size > 0


def _synthetic_segment(path: Path, dur: float, freq: int) -> Path:
    from vedit import ff

    ff.ffmpeg(
        [
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x240:rate=30:duration={dur}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={dur}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "28",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-shortest",
            str(path),
        ],
        timeout=120,
    )
    return path


def test_crossfade_concat_duration_accounts_overlap(tmp_path, cfg):
    """xfade/acrossfade output is shorter by (N-1) * overlap — the QC math."""
    from vedit import ff

    segs = [
        _synthetic_segment(tmp_path / f"s{i}.mp4", 2.0, 440 + i * 110) for i in range(3)
    ]
    overlap = render.crossfade_overlap_s(cfg)
    assert overlap > 0, "default config must enable crossfades"
    merged = render.concat_crossfade(segs, tmp_path, cfg)
    assert merged.stat().st_size > 0
    dur = ff.duration_of(ff.probe(merged))
    expected = sum(ff.duration_of(ff.probe(p)) for p in segs) - 2 * overlap
    assert dur == pytest.approx(expected, abs=0.35)


def test_crossfade_disabled_falls_back_to_lossless(tmp_path, cfg):
    cfg2 = dataclasses.replace(
        cfg,
        cuts=dataclasses.replace(cfg.cuts, crossfade_video_ms=0, crossfade_audio_ms=0),
    )
    assert render.crossfade_overlap_s(cfg2) == 0.0
    segs = [
        _synthetic_segment(tmp_path / f"f{i}.mp4", 1.5, 440 + i * 110) for i in range(2)
    ]
    merged = render.concat_crossfade(segs, tmp_path, cfg2)
    assert merged.stat().st_size > 0
    from vedit import ff

    dur = ff.duration_of(ff.probe(merged))
    expected = sum(ff.duration_of(ff.probe(p)) for p in segs)
    assert dur == pytest.approx(expected, abs=0.35)


def test_segment_fingerprint_stable_and_sensitive(tmp_path, cfg):
    asset = _shot(tmp_path)
    base = tmp_path / "base.mp4"
    base.write_bytes(b"fake-base")
    clean = tmp_path / "clean.wav"
    clean.write_bytes(b"fake-clean")
    fp1 = render._segment_fingerprint(0.0, 2.5, [], False, base, clean, cfg)
    fp2 = render._segment_fingerprint(0.0, 2.5, [], False, base, clean, cfg)
    assert fp1 == fp2
    assert render._segment_fingerprint(0.0, 2.5, [], True, base, clean, cfg) != fp1
    assert render._segment_fingerprint(0.0, 3.0, [], False, base, clean, cfg) != fp1
    assert asset.exists()


def _ass_text(text: str) -> str:
    return (
        "[Script Info]\nScriptType: v4.00+\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Cap,Arial,20,&H00FFFFFF,&H00FFFFFF,&H00000000,&H7F000000,"
        "0,0,0,0,100,100,0,0,1,1,0,2,10,10,10,1\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
        f"Dialogue: 0,0:00:00.00,0:00:01.00,Cap,,0,0,0,,{text}\n"
    )


def test_burn_captions_overwrites_existing_final(tmp_path, cfg):
    """Regression: every fix re-render must replace final.mp4 (was kept stale)."""
    from vedit import ff

    out = tmp_path / "final.mp4"
    first_concat = _synthetic_segment(tmp_path / "concat.mp4", 2.0, 440)
    (tmp_path / "captions.ass").write_text(_ass_text("first"), encoding="utf-8")
    render.burn_captions(first_concat, tmp_path, tmp_path / "fonts", out, cfg)
    first_bytes = out.read_bytes()
    assert ff.duration_of(ff.probe(out)) == pytest.approx(2.0, abs=0.3)

    second_concat = _synthetic_segment(tmp_path / "concat2.mp4", 1.0, 660)
    (tmp_path / "captions.ass").write_text(_ass_text("second"), encoding="utf-8")
    render.burn_captions(second_concat, tmp_path, tmp_path / "fonts", out, cfg)
    assert out.read_bytes() != first_bytes
    assert ff.duration_of(ff.probe(out)) == pytest.approx(1.0, abs=0.3)


def test_scaled_cover_centers_inside_frame(tmp_path, cfg, transcript):
    """resize_visual scale shrinks a cover to a centered, in-frame box."""
    v = Visual(
        id="v1",
        kind="screenshot",
        file="img.png",
        from_word=10,
        to_word=19,
        pos="top-right",
        scale=0.5,
    )
    asset = _shot(tmp_path)
    resolved = [ResolvedVisual(visual=v, asset=asset)]
    seg = (0.0, transcript.words[19].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), cfg
    )
    assert len(plans) == 1
    p = plans[0]
    assert (p.w, p.h) == (540, 960)
    assert (p.x, p.y) == (1080 - 540 - 12 - 48, 300)
    assert 0 <= p.x and p.x + p.w + 12 <= 1080
    assert 0 <= p.y and p.y + p.h + 12 <= 1920


def test_full_cover_unchanged(tmp_path, cfg, transcript):
    cfg = _cover_cfg(cfg)
    v = Visual(
        id="v1",
        kind="screenshot",
        file="img.png",
        from_word=10,
        to_word=19,
        scale=1.0,
    )
    asset = _shot(tmp_path)
    resolved = [ResolvedVisual(visual=v, asset=asset)]
    seg = (0.0, transcript.words[19].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), cfg
    )
    assert [(p.x, p.y, p.w, p.h) for p in plans] == [(0, 0, 1080, 1920)]


def _adaptive_cfg(cfg):
    return dataclasses.replace(
        cfg, visuals=dataclasses.replace(cfg.visuals, screenshot_mode="adaptive")
    )


def test_adaptive_wide_image_uses_blur_fit(tmp_path, cfg, transcript):
    from PIL import Image

    wide = tmp_path / "wide.png"
    Image.new("RGB", (800, 200), (10, 60, 120)).save(wide)  # 4:1 landscape
    v = Visual(id="v1", kind="screenshot", file="wide.png", from_word=10, to_word=19)
    resolved = [ResolvedVisual(visual=v, asset=wide)]
    seg = (0.0, transcript.words[19].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), _adaptive_cfg(cfg)
    )
    assert len(plans) == 1
    assert plans[0].fit_blur is True
    assert (plans[0].w, plans[0].h) == (1080, 1920)


def test_adaptive_reel_shaped_image_uses_cover(tmp_path, cfg, transcript):
    from PIL import Image

    tall = tmp_path / "tall.png"
    Image.new("RGB", (540, 960), (10, 60, 120)).save(tall)  # exactly 9:16
    v = Visual(id="v1", kind="screenshot", file="tall.png", from_word=10, to_word=19)
    resolved = [ResolvedVisual(visual=v, asset=tall)]
    seg = (0.0, transcript.words[19].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), _adaptive_cfg(cfg)
    )
    assert plans[0].fit_blur is False
    assert (plans[0].x, plans[0].y, plans[0].w, plans[0].h) == (0, 0, 1080, 1920)


def test_fit_blur_filter_graph_composites(tmp_path, cfg, transcript):
    from PIL import Image

    wide = tmp_path / "wide.png"
    Image.new("RGB", (800, 200), (10, 60, 120)).save(wide)
    v = Visual(id="v1", kind="screenshot", file="wide.png", from_word=10, to_word=19)
    resolved = [ResolvedVisual(visual=v, asset=wide)]
    seg = (0.0, transcript.words[19].e + 1.0)
    plans = render._overlays_for_segment(
        seg[0], seg[1], resolved, transcript_plan(transcript), _adaptive_cfg(cfg)
    )
    graph, _vlabel = render._filter_for_segment(
        seg[0], seg[1], plans, False, _adaptive_cfg(cfg)
    )
    assert "gblur=sigma=" in graph
    assert "force_original_aspect_ratio=decrease" in graph
    assert "overlay=x=(W-w)/2:y=(H-h)/2" in graph
    assert "overlay=x=0:y=0:" in graph  # composite placed full-frame
