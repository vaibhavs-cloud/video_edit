"""Cover-mode screenshots and subtle per-visual transitions."""

from __future__ import annotations

from pathlib import Path

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


def test_cover_fills_frame_and_cycles_transitions(tmp_path, cfg, transcript):
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
        cfg, visuals=dataclasses.replace(cfg.visuals, transitions=[])
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
    ff.ffmpeg(args, timeout=120)
    assert (tmp_path / "o.mp4").stat().st_size > 0
