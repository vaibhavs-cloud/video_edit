"""Deterministic rendering: reframe -> per-segment encode -> concat -> caption burn.

Every segment is encoded with *identical* parameters so the final concat can
either stream-copy (lossless, when crossfades are disabled) or blend segment
joins with a short xfade/acrossfade (when enabled). Overlay fades are computed
per segment so a visual that spans a cut never fades twice. All timeline
positions are source-time; the overlay `enable` window is converted to
segment-local time here.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from vedit import ff
from vedit.config import Config
from vedit.schema import EditPlan
from vedit.stage.visuals import ResolvedVisual

# Overlay placement margins (px in the 1080x1920 frame). Named here so the
# geometry lives in one place; promote to config.yaml if they ever need tuning.
OVERLAY_EDGE_MARGIN = 48
OVERLAY_TOP_Y = 300
OVERLAY_BOTTOM_LIFT = 420
OVERLAY_CENTER_LIFT = 240
OVERLAY_MIN_Y = 200


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
    fx: str = "fade"  # fade | rise | drift (screenshots in cover mode only)
    fx_dur: float = 0.0  # local duration the motion runs over (drift)


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
    pos: str, aw: int, ah: int, cfg: Config, margin: int = OVERLAY_EDGE_MARGIN
) -> tuple[int, int]:
    w, h = cfg.video.width, cfg.video.height
    if pos == "top-right":
        return w - aw - margin, OVERLAY_TOP_Y
    if pos == "top-left":
        return margin, OVERLAY_TOP_Y
    if pos == "top":
        return (w - aw) // 2, OVERLAY_TOP_Y
    if pos == "bottom":
        return (w - aw) // 2, h - ah - OVERLAY_BOTTOM_LIFT
    return (w - aw) // 2, max(OVERLAY_MIN_Y, h // 2 - ah // 2 - OVERLAY_CENTER_LIFT)


def _overlays_for_segment(
    seg_start: float,
    seg_end: float,
    resolved: list[ResolvedVisual],
    plan: EditPlan,
    cfg: Config,
) -> list[OverlayPlan]:
    out: list[OverlayPlan] = []
    fade = cfg.visuals.fade_s
    fx_list = cfg.visuals.transitions or ["fade"]
    for idx, rv in enumerate(resolved):
        v = rv.visual
        vis_start = plan.words[v.from_word].s
        vis_end = plan.words[v.to_word].e
        if vis_end <= seg_start or vis_start >= seg_end:
            continue
        if rv.asset is None or not rv.asset.exists():
            continue

        is_shot = v.kind == "screenshot"
        cover = is_shot and cfg.visuals.screenshot_mode == "cover"
        full_cover = cover and v.scale >= 1.0
        if full_cover:
            aw, ah = cfg.video.width, cfg.video.height
            x, y = 0, 0
        elif cover:
            # zoomed-out cover: fraction of the frame, resolution-independent
            w0 = max(2, int(cfg.video.width * v.scale) // 2 * 2)
            h0 = max(2, int(cfg.video.height * v.scale) // 2 * 2)
            aw, ah = w0 + 12, h0 + 12
            x, y = _place(v.pos, aw, ah, cfg)
            x = min(max(x, 0), cfg.video.width - aw)
            y = min(max(y, 0), cfg.video.height - ah)
        elif is_shot:
            iw, ih = _image_size(rv.asset)
            target_w = int(cfg.video.width * cfg.visuals.screenshot_width_frac)
            target_w = max(2, int(target_w * v.scale) // 2 * 2)
            target_h = max(2, int(ih * (target_w / iw)))
            aw, ah = target_w + 12, target_h + 12  # + border pad
        else:
            iw, ih = _image_size(rv.asset)
            aw = max(2, int(iw * v.scale))
            ah = max(2, int(ih * v.scale))

        if not cover:
            x, y = _place(v.pos, aw, ah, cfg)
            if x < 0 or y < 0 or x + aw > cfg.video.width or y + ah > cfg.video.height:
                raise RenderError(
                    f"overlay {v.id} does not fit the frame at pos={v.pos}"
                )

        local_start = max(vis_start, seg_start) - seg_start
        local_end = min(vis_end, seg_end) - seg_start
        if local_end - local_start < 0.15:
            continue

        in_dur = local_end - local_start
        fin = 0.0 if vis_start < seg_start + 1e-3 else min(fade, in_dur * 0.4)
        fout = 0.0 if vis_end > seg_end - 1e-3 else min(fade, in_dur * 0.4)
        fx = fx_list[idx % len(fx_list)] if full_cover else "fade"

        if full_cover:
            w, h = cfg.video.width, cfg.video.height
        else:
            w, h = aw - (12 if is_shot else 0), ah - (12 if is_shot else 0)
        out.append(
            OverlayPlan(
                path=rv.asset,
                x=x,
                y=y,
                w=w,
                h=h,
                local_start=local_start,
                local_end=local_end,
                fade_in=fin,
                fade_out=fout,
                fx=fx,
                fx_dur=in_dur,
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
    chain = [f"[0:v]trim=start={seg_start:.3f}:end={seg_end:.3f},setpts=PTS-STARTPTS"]
    if zoom:
        z = cfg.video.zoom_factor
        chain[0] += (
            f",scale=iw*{z}:-2,crop={cfg.video.width}:{cfg.video.height}:x='(iw-{cfg.video.width})/2':y='(ih-{cfg.video.height})/2'"
        )
    chain[0] += "[vbase]"

    current = "[vbase]"
    parts = list(chain)
    W, H = cfg.video.width, cfg.video.height
    for k, ov in enumerate(overlays, start=1):
        img_parts = [f"[{k + 1}:v]format=rgba"]
        if ov.w == W and ov.h == H and ov.fx in ("fade", "rise", "drift"):
            # full-frame cover: scale up, center-crop (drift pre-scales larger)
            if ov.fx == "drift":
                dw, dh = W + (W // 16 // 2 * 2), H + (H // 16 // 2 * 2)
                img_parts.append(
                    f"scale={W}:{H}:force_original_aspect_ratio=increase:"
                    f"flags=lanczos,crop={W}:{H},scale={dw}:{dh}:flags=lanczos"
                )
            else:
                img_parts.append(
                    f"scale={W}:{H}:force_original_aspect_ratio=increase:"
                    f"flags=lanczos,crop={W}:{H}"
                )
                dw, dh = W, H
        else:
            dw, dh = ov.w, ov.h
            img_parts.append(f"scale={dw}:{dh}:flags=lanczos")
        if ov.fade_in > 0:
            img_parts.append(f"fade=t=in:st=0:d={ov.fade_in:.3f}:alpha=1")
        if ov.fade_out > 0:
            img_parts.append(
                f"fade=t=out:st={max(0.0, ov.local_end - ov.fade_out):.3f}:d={ov.fade_out:.3f}:alpha=1"
            )
        parts.append(",".join(img_parts) + f"[ov{k}]")
        s, e, d = ov.local_start, ov.local_end, max(ov.fx_dur, 0.5)
        if ov.fx == "rise" and dw == W:
            x_expr, y_expr = "0", f"'max(0,{H}*(1-(t-{s:.3f})/0.35))'"
        elif ov.fx == "drift" and dw > W:
            dx, dy = dw - W, dh - H
            x_expr = f"'-{dx}+{dx}*min(1,(t-{s:.3f})/{d:.3f})'"
            y_expr = f"'-{dy}+{dy}*min(1,(t-{s:.3f})/{d:.3f})'"
        else:
            x_expr, y_expr = str(ov.x), str(ov.y)
        parts.append(
            f"{current}[ov{k}]overlay=x={x_expr}:y={y_expr}:"
            f"enable='between(t,{s:.3f},{e:.3f})'[v{k}]"
        )
        current = f"[v{k}]"
    parts.append(
        f"[1:a]atrim=start={seg_start:.3f}:end={seg_end:.3f},asetpts=PTS-STARTPTS,"
        f"afade=t=in:d=0.01,afade=t=out:st={max(0.0, dur - 0.01):.3f}:d=0.01[aout]"
    )
    return ";".join(parts), current


def _segment_fingerprint(
    seg_start: float,
    seg_end: float,
    overlays: list[OverlayPlan],
    zoom: bool,
    base: Path,
    clean_audio: Path,
    cfg: Config,
) -> str:
    """Hash of everything a segment encode depends on (fix runs skip on match)."""
    h = hashlib.sha256()
    h.update(f"{seg_start:.3f}-{seg_end:.3f}-zoom={zoom}".encode())
    for ov in overlays:
        try:
            st = ov.path.stat()
            asset_tag = f"{st.st_size}:{st.st_mtime_ns}"
        except OSError:
            asset_tag = "missing"
        h.update(
            repr(
                (
                    str(ov.path),
                    asset_tag,
                    ov.x,
                    ov.y,
                    ov.w,
                    ov.h,
                    round(ov.local_start, 3),
                    round(ov.local_end, 3),
                    round(ov.fade_in, 3),
                    round(ov.fade_out, 3),
                    ov.fx,
                    round(ov.fx_dur, 3),
                )
            ).encode()
        )
    v = cfg.video
    h.update(
        f"{v.width}x{v.height}@{v.fps}-{v.x264_preset}-crf{v.x264_crf}"
        f"-{v.audio_bitrate}-zoomx{v.zoom_factor}".encode()
    )
    for src in (base, clean_audio):
        try:
            st = src.stat()
            h.update(f"{src.name}:{st.st_size}:{st.st_mtime_ns}".encode())
        except OSError:
            h.update(f"{src.name}:missing".encode())
    h.update(cfg.hash.encode())
    return h.hexdigest()[:16]


def _encode_one_segment(
    base: Path,
    clean_audio: Path,
    seg_start: float,
    seg_end: float,
    overlays: list[OverlayPlan],
    zoom: bool,
    out: Path,
    cfg: Config,
) -> Path:
    dur = seg_end - seg_start
    graph, vlabel = _filter_for_segment(seg_start, seg_end, overlays, zoom, cfg)
    args = ["-y", "-i", str(base), "-i", str(clean_audio)]
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
    return out


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
    max_zoom = max(1, len(plan.segments) // cfg.video.max_zoom_segments_div)

    # Zoom assignment stays sequential so the cap is deterministic; only the
    # ffmpeg encodes below run in parallel.
    jobs: list[tuple[int, float, float, list[OverlayPlan], bool, Path, str]] = []
    zoomed_total = 0
    for i, seg in enumerate(plan.segments):
        out = seg_dir / f"seg_{i:03d}.mp4"
        overlays = _overlays_for_segment(seg.start, seg.end, resolved, plan, cfg)

        want_zoom = any(
            seg.keep_from_word <= z <= seg.keep_to_word for z in plan.zoom_at_words
        )
        if want_zoom and zoomed_total < max_zoom:
            zoom = True
            zoomed_total += 1
        else:
            zoom = False

        fingerprint = _segment_fingerprint(
            seg.start, seg.end, overlays, zoom, base, clean_audio, cfg
        )
        jobs.append((i, seg.start, seg.end, overlays, zoom, out, fingerprint))

    paths: list[Path | None] = [None] * len(jobs)

    def _run(job: tuple[int, float, float, list[OverlayPlan], bool, Path, str]) -> Path:
        _i, start, end, overlays, zoom, out, fingerprint = job
        sidecar = out.with_suffix(".mp4.json")
        if out.exists() and out.stat().st_size > 0 and sidecar.exists():
            try:
                if (
                    json.loads(sidecar.read_text(encoding="utf-8")).get("fp")
                    == fingerprint
                ):
                    return out
            except (OSError, ValueError):
                pass
            out.unlink(missing_ok=True)
        _encode_one_segment(base, clean_audio, start, end, overlays, zoom, out, cfg)
        sidecar.write_text(json.dumps({"fp": fingerprint}), encoding="utf-8")
        return out

    workers = max(1, min(cfg.video.encode_workers, len(jobs) or 1))
    if workers < 2:
        for job in jobs:
            paths[job[0]] = _run(job)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for idx, path in zip(
                [j[0] for j in jobs], pool.map(_run, jobs), strict=True
            ):
                paths[idx] = path
    return [p for p in paths if p is not None]


def _concat_path(path: Path) -> str:
    # Concat-demuxer quoting: single-quote wrapped, embedded quotes escaped.
    # as_posix keeps this working on Windows (backslash is an escape char).
    return str(path.resolve().as_posix()).replace("'", "'\\''")


def concat(paths: list[Path], workdir: Path) -> Path:
    if not paths:
        raise RenderError("no segments to concat")
    listing = workdir / "concat_list.txt"
    listing.write_text(
        "\n".join(f"file '{_concat_path(p)}'" for p in paths) + "\n",
        encoding="utf-8",
    )
    out = workdir / "concat.mp4"
    ff.ffmpeg(
        ["-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy", str(out)]
    )
    return out


def crossfade_overlap_s(cfg: Config) -> float:
    """Effective segment-join overlap in seconds (0 = hard cut).

    A single overlap keeps A/V in sync: when video crossfades are on, the
    video duration wins and the audio blend uses the same window; audio-only
    applies when the video knob is 0.
    """
    video_ms = cfg.cuts.crossfade_video_ms
    audio_ms = cfg.cuts.crossfade_audio_ms
    if video_ms > 0:
        return video_ms / 1000.0
    if audio_ms > 0:
        return audio_ms / 1000.0
    return 0.0


def _probe_duration(path: Path) -> float:
    return ff.duration_of(ff.probe(path))


def concat_crossfade(paths: list[Path], workdir: Path, cfg: Config) -> Path:
    """Join segments with short xfade/acrossfade blends at every boundary.

    Falls back to the lossless ``concat`` when disabled or trivial, so zero
    behavior change for users who don't opt in. Each join shortens the output
    by the overlap — QC accounts for ``(N-1) * overlap``.
    """
    overlap = crossfade_overlap_s(cfg)
    if len(paths) < 2 or overlap <= 0:
        return concat(paths, workdir)

    durs = [_probe_duration(p) for p in paths]
    shortest = min(durs)
    if overlap >= shortest:
        # Degenerate (very short segment): shrink the blend, never fail.
        overlap = max(0.01, shortest - 0.05)

    inputs: list[str] = []
    for p in paths:
        inputs += ["-i", str(p)]

    v_chain: list[str] = []
    a_chain: list[str] = []
    cumulative = durs[0]
    for i in range(1, len(paths)):
        offset = max(0.0, cumulative - overlap)
        prev_v = f"[vx{i - 1}]" if i > 1 else "[0:v]"
        v_out = "[vout]" if i == len(paths) - 1 else f"[vx{i}]"
        v_chain.append(
            f"{prev_v}[{i}:v]xfade=transition=fade:duration={overlap:.3f}:offset={offset:.3f}{v_out}"
        )
        prev_a = f"[ax{i - 1}]" if i > 1 else "[0:a]"
        a_out = "[aout]" if i == len(paths) - 1 else f"[ax{i}]"
        a_chain.append(
            f"{prev_a}[{i}:a]acrossfade=d={overlap:.3f}:c1=tri:c2=tri{a_out}"
        )
        cumulative += durs[i] - overlap

    graph = ";".join(v_chain + a_chain)
    out = workdir / "concat.mp4"
    ff.ffmpeg(
        [
            "-y",
            *inputs,
            "-filter_complex",
            graph,
            "-map",
            "[vout]",
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
            "-movflags",
            "+faststart",
            "-shortest",
            str(out),
        ]
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
            "-y",
            "-i",
            concat_mp4.name,
            "-vf",
            vf,
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
            "copy",
            "-movflags",
            "+faststart",
            str(out_path.resolve()),
        ],
        cwd=workdir,
    )
    return out_path
