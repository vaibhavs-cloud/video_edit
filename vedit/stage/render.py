"""Deterministic rendering: reframe -> per-segment encode -> concat -> caption burn.

Every segment is encoded with *identical* parameters so the final concat can
stream-copy (lossless). Overlay fades are computed per segment so a visual that
spans a cut never fades twice. All timeline positions are source-time; the
overlay `enable` window is converted to segment-local time here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from vedit import ff
from vedit.config import Config
from vedit.schema import EditPlan
from vedit.stage.visuals import ResolvedVisual


class RenderError(RuntimeError):
    pass


@dataclass(frozen=True)
class OverlayPlan:
    path: Path
    x: int
    y: int
    w: int
    h: int
    local_start: float
    local_end: float
    fade_in: float
    fade_out: float


def ensure_base(source: Path, plan: EditPlan, workdir: Path, cfg: Config) -> Path:
    workdir.mkdir(parents=True, exist_ok=True)
    base = workdir / f"base_{plan.source.sha256[:8]}.mp4"
    if base.exists() and base.stat().st_size > 0:
        return base

    w, h = cfg.video.width, cfg.video.height
    ratio = f"{w}/{h}"
    if plan.reframe.mode == "scale":
        vf = (
            f"scale={w}:{h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={w}:{h}:x='(iw-{w})/2':y='(ih-{h})/2'"
        )
    else:
        frac = plan.reframe.x_frac
        vf = (
            f"crop=w='min(iw,ih*{ratio})':h='min(ih,iw/({ratio}))':"
            f"x='(iw-min(iw,ih*{ratio}))*{frac}':y='(ih-min(ih,iw/({ratio})))*0.5',"
            f"scale={w}:{h}:flags=lanczos"
        )
    ff.ffmpeg(
        [
            "-i",
            str(source),
            "-vf",
            vf,
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            cfg.video.x264_preset,
            "-crf",
            str(cfg.video.x264_crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(cfg.video.fps),
            str(base),
        ]
    )
    return base


def _image_size(path: Path) -> tuple[int, int]:
    with Image.open(path) as img:
        return img.size


def _place(
    pos: str, aw: int, ah: int, cfg: Config, margin: int = 48
) -> tuple[int, int]:
    w, h = cfg.video.width, cfg.video.height
    if pos == "top-right":
        return w - aw - margin, 300
    if pos == "top-left":
        return margin, 300
    if pos == "top":
        return (w - aw) // 2, 300
    if pos == "bottom":
        return (w - aw) // 2, h - ah - 420
    return (w - aw) // 2, max(200, h // 2 - ah // 2 - 240)


def _overlays_for_segment(
    seg_start: float,
    seg_end: float,
    resolved: list[ResolvedVisual],
    plan: EditPlan,
    cfg: Config,
) -> list[OverlayPlan]:
    out: list[OverlayPlan] = []
    fade = cfg.visuals.fade_s
    for rv in resolved:
        v = rv.visual
        vis_start = plan.words[v.from_word].s
        vis_end = plan.words[v.to_word].e
        if vis_end <= seg_start or vis_start >= seg_end:
            continue
        if rv.asset is None or not rv.asset.exists():
            continue

        is_shot = v.kind == "screenshot"
        iw, ih = _image_size(rv.asset)
        if is_shot:
            target_w = int(cfg.video.width * cfg.visuals.screenshot_width_frac)
            target_w -= target_w % 2
            target_h = max(2, int(ih * (target_w / iw)))
            aw, ah = target_w + 12, target_h + 12  # + border pad
        else:
            aw, ah = iw, ih

        x, y = _place(v.pos, aw, ah, cfg)
        if x < 0 or y < 0 or x + aw > cfg.video.width or y + ah > cfg.video.height:
            raise RenderError(f"overlay {v.id} does not fit the frame at pos={v.pos}")

        local_start = max(vis_start, seg_start) - seg_start
        local_end = min(vis_end, seg_end) - seg_start
        if local_end - local_start < 0.15:
            continue

        in_dur = local_end - local_start
        fin = 0.0 if vis_start < seg_start + 1e-3 else min(fade, in_dur * 0.4)
        fout = 0.0 if vis_end > seg_end - 1e-3 else min(fade, in_dur * 0.4)

        out.append(
            OverlayPlan(
                path=rv.asset,
                x=x,
                y=y,
                w=aw - (12 if is_shot else 0),
                h=ah - (12 if is_shot else 0),
                local_start=local_start,
                local_end=local_end,
                fade_in=fin,
                fade_out=fout,
            )
        )
    return out


def _filter_for_segment(
    seg_start: float,
    seg_end: float,
    overlays: list[OverlayPlan],
    zoom: bool,
    cfg: Config,
) -> tuple[str, str]:
    dur = seg_end - seg_start
    vlabel = "[v0]"
    chain = [f"[0:v]trim=start={seg_start:.3f}:end={seg_end:.3f},setpts=PTS-STARTPTS"]
    if zoom:
        z = cfg.video.zoom_factor
        chain[0] += (
            f",scale=iw*{z}:-2,crop={cfg.video.width}:{cfg.video.height}:x='(iw-{cfg.video.width})/2':y='(ih-{cfg.video.height})/2'"
        )
    chain[0] += "[vbase]"

    current = "[vbase]"
    parts = list(chain)
    for k, ov in enumerate(overlays, start=1):
        img_parts = [f"[{k + 1}:v]format=rgba"]
        if ov.fade_in > 0:
            img_parts.append(f"fade=t=in:st=0:d={ov.fade_in:.3f}:alpha=1")
        if ov.fade_out > 0:
            img_parts.append(
                f"fade=t=out:st={max(0.0, ov.local_end - ov.fade_out):.3f}:d={ov.fade_out:.3f}:alpha=1"
            )
        parts.append(",".join(img_parts) + f"[ov{k}]")
        parts.append(
            f"{current}[ov{k}]overlay=x={ov.x}:y={ov.y}:"
            f"enable='between(t,{ov.local_start:.3f},{ov.local_end:.3f})'[v{k}]"
        )
        current = f"[v{k}]"
    parts.append(
        f"[1:a]atrim=start={seg_start:.3f}:end={seg_end:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=in:d=0.01,afade=t=out:st={max(0.0, dur - 0.01):.3f}:d=0.01[aout]"
    )
    _ = vlabel
    return ";".join(parts), current


def render_segments(
    plan: EditPlan,
    resolved: list[ResolvedVisual],
    base: Path,
    clean_audio: Path,
    workdir: Path,
    cfg: Config,
) -> list[Path]:
    seg_dir = workdir / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    zoomed_total = 0
    max_zoom = max(1, len(plan.segments) // cfg.video.max_zoom_segments_div)

    for i, seg in enumerate(plan.segments):
        out = seg_dir / f"seg_{i:03d}.mp4"
        dur = seg.end - seg.start
        overlays = _overlays_for_segment(seg.start, seg.end, resolved, plan, cfg)

        want_zoom = any(
            seg.keep_from_word <= z <= seg.keep_to_word for z in plan.zoom_at_words
        )
        if want_zoom and zoomed_total < max_zoom:
            zoom = True
            zoomed_total += 1
        else:
            zoom = False

        if out.exists():
            out.unlink()

        graph, vlabel = _filter_for_segment(seg.start, seg.end, overlays, zoom, cfg)
        args = ["-i", str(base), "-i", str(clean_audio)]
        for ov in overlays:
            args += [
                "-loop",
                "1",
                "-framerate",
                str(cfg.video.fps),
                "-t",
                f"{dur:.3f}",
                "-i",
                str(ov.path),
            ]
        args += [
            "-filter_complex",
            graph,
            "-map",
            vlabel,
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-preset",
            cfg.video.x264_preset,
            "-crf",
            str(cfg.video.x264_crf),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(cfg.video.fps),
            "-c:a",
            "aac",
            "-b:a",
            cfg.video.audio_bitrate,
            "-ar",
            "48000",
            "-shortest",
            str(out),
        ]
        ff.ffmpeg(args)
        paths.append(out)
    return paths


def concat(paths: list[Path], workdir: Path) -> Path:
    if not paths:
        raise RenderError("no segments to concat")
    listing = workdir / "concat_list.txt"
    listing.write_text(
        "\n".join(f"file '{p.resolve().as_posix()}'" for p in paths) + "\n",
        encoding="utf-8",
    )
    out = workdir / "concat.mp4"
    ff.ffmpeg(
        ["-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)]
    )
    return out


def burn_captions(
    concat_mp4: Path, workdir: Path, fonts_dir: Path, out_path: Path, cfg: Config
) -> Path:
    local_fonts = workdir / "fonts"
    if fonts_dir.exists() and not local_fonts.exists():
        import shutil

        shutil.copytree(fonts_dir, local_fonts)
    if not (workdir / "captions.ass").exists():
        raise RenderError("captions.ass missing — generate captions before burning")

    vf = "ass=captions.ass"
    if local_fonts.exists():
        vf += ":fontsdir=fonts"
    ff.ffmpeg(
        [
            "-i",
            concat_mp4.name,
            "-vf",
            vf,
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(out_path.resolve()),
        ],
        cwd=workdir,
    )
    return out_path
