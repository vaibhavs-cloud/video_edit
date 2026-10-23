"""CLI entry point: process | fix | qc | report | deliver.

Every stage is marker-gated in the run state directory, so an interrupted or
rejected run resumes exactly where it stopped. Business logic lives in the
stage modules; this file only orchestrates.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

from vedit import ff
from vedit import state as st
from vedit.acquire import acquire, verify_media
from vedit.config import Config, load_config
from vedit.schema import (
    EditPlan,
    FixOp,
    FixPatch,
    PlanDraft,
    Segment,
    StateMeta,
    Transcript,
)
from vedit.stage import audio as audio_stage
from vedit.stage import captions as captions_stage
from vedit.stage import cuts, render, transcribe
from vedit.stage import fix as fix_stage
from vedit.stage import plan as plan_stage
from vedit.stage import qc as qc_stage
from vedit.stage import report as report_stage
from vedit.stage import visuals as visuals_stage
from vedit.stage.visuals import ResolvedVisual

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FONTS_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_LOG = logging.getLogger("vedit")


def _setup_logging() -> None:
    """Levelled logging: VEDIT_LOG_LEVEL=DEBUG|INFO|WARNING filters CI output."""
    level = os.environ.get("VEDIT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(levelname)s %(message)s",
        stream=sys.stdout,
    )
    _LOG.setLevel(level)  # keep our records flowing even if root was configured


def _log(msg: str) -> None:
    _LOG.info(msg)


def _warn(msg: str) -> None:
    _LOG.warning(msg)


def _notify(chat_id: str, text: str) -> None:
    """Best-effort user-facing message (fix rejections, hints). Never raises."""
    try:
        from vedit.telegram_client import Telegram

        Telegram().send_message(chat_id, text[:4000])
    except Exception as exc:  # noqa: BLE001 — a failed hint must not kill the run
        _warn(f"[notify] failed: {exc}")


@dataclass
class Ctx:
    state: Path
    cfg: Config
    mock: bool
    prompt: str
    input_ref: str
    chat_id: str
    repick: list[str] | None = None  # visual ids to re-pick on a fix run

    @property
    def work(self) -> Path:
        return self.state / "work"

    @property
    def icons(self) -> Path:
        return self.state / "icons"

    @property
    def attachments(self) -> Path | None:
        d = self.state / "attachments"
        return d if d.exists() else None


# ---------- loaders ----------


def _load_transcript(ctx: Ctx) -> Transcript:
    return st.load_model(ctx.state / "transcript.json", Transcript)


def _load_segments(ctx: Ctx) -> list[Segment]:
    return [
        Segment.model_validate(s) for s in st.load_json(ctx.state / "segments.json")
    ]


def _load_plan(ctx: Ctx) -> EditPlan:
    return st.load_model(ctx.state / "edit_plan.json", EditPlan)


def _load_resolved(ctx: Ctx) -> list[ResolvedVisual]:
    plan = _load_plan(ctx)
    by_id = {v.id: v for v in plan.visuals}
    out: list[ResolvedVisual] = []
    for entry in st.load_json(ctx.state / "resolved.json"):
        visual = by_id.get(entry["id"])
        if visual is None:
            continue
        asset = ctx.state / entry["asset"]
        if asset.exists():
            out.append(ResolvedVisual(visual=visual, asset=asset))
    return out


def _qc_result(ctx: Ctx) -> qc_stage.QcResult:
    data = st.load_json(ctx.state / "qc.json")
    return qc_stage.QcResult(checks=data["checks"], stats=data.get("stats", {}))


# ---------- stages ----------


def _make_mock_source(dest: Path, cfg: Config) -> None:
    if dest.exists():
        return
    ff.ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30:duration=31",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=31",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-shortest",
            str(dest),
        ],
        timeout=300,
    )


def _mock_icon(dest_dir: Path, visual_id: str, keyword: str, cfg: Config) -> Path:
    from PIL import Image, ImageDraw

    size = cfg.visuals.icon_size
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([8, 8, size - 8, size - 8], outline=(255, 255, 255, 255), width=6)
    draw.text(
        (size // 4, size // 2 - 6), (keyword or "icon")[:8], fill=(255, 255, 255, 255)
    )
    path = dest_dir / f"mock-{visual_id}.png"
    img.save(path)
    return path


def s_acquire(ctx: Ctx) -> None:
    dest = ctx.state / "source.mp4"
    if ctx.mock and not ctx.input_ref:
        _make_mock_source(dest, ctx.cfg)
        kind, ref = "local", "mock://synthetic"
    else:
        _, kind = acquire(ctx.input_ref, dest, ctx.cfg)
        ref = ctx.input_ref
    probe = verify_media(dest, ctx.cfg)
    st.save_json(ctx.state / "probe.json", probe)
    st.save_model(
        ctx.state / "meta.json",
        StateMeta(
            ref=probe["sha256"][:12],
            sha256=probe["sha256"],
            source_kind=kind,
            source_ref=ref,
            chat_id=ctx.chat_id,
            config_hash=ctx.cfg.hash,
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            mock=ctx.mock,
        ),
    )
    _log(f"[acquire] {kind} source {probe['w']}x{probe['h']} {probe['dur']:.1f}s")


def s_audio(ctx: Ctx) -> None:
    cfg = ctx.cfg
    preset_file = ctx.state / "preset.json"
    if preset_file.exists():
        try:
            cfg = audio_stage.with_preset(
                cfg, str(st.load_json(preset_file).get("preset") or "")
            )
        except audio_stage.AudioError as exc:
            _warn(f"[audio] preset ignored: {exc}")
            cfg = audio_stage.with_preset(cfg, None)
    paths = audio_stage.prepare(ctx.state / "source.mp4", ctx.work, cfg)
    _log(f"[audio] clean track ready ({paths.clean_48k.name})")


def s_transcribe(ctx: Ctx) -> None:
    if ctx.mock:
        transcript = Transcript.model_validate_json(
            (FIXTURES / "transcript.json").read_text(encoding="utf-8")
        )
        _log(f"[transcribe] mock fixture, {len(transcript.words)} words")
    else:
        transcript = transcribe.transcribe(ctx.work / "audio" / "stt_16k.wav", ctx.cfg)
        _log(f"[transcribe] {len(transcript.words)} words")
    st.save_model(ctx.state / "transcript.json", transcript)


def s_cut(ctx: Ctx) -> None:
    probe = st.load_json(ctx.state / "probe.json")
    transcript = _load_transcript(ctx)
    words = transcript.words
    if not ctx.mock:
        # whisper stretches word timings across real pauses: cross-check the
        # raw track and shrink phantom spans before the gap rules run
        raw_audio = ctx.work / "audio" / "raw_48k.wav"
        if raw_audio.exists():
            silences = ff.detect_silences(
                raw_audio,
                ctx.cfg.cuts.silence_noise_db,
                ctx.cfg.cuts.silence_min_dur_ms / 1000.0,
                ctx.cfg.cuts.silence_merge_gap_ms / 1000.0,
            )
            words, stats = cuts.correct_words(words, silences, ctx.cfg.cuts)
            transcript = Transcript(words=words)
            st.save_model(ctx.state / "transcript.json", transcript)
            _log(
                f"[cut] silence assist: {len(silences)} pauses, "
                f"{stats['shrunk']} words shrunk, {stats['dropped']} dropped"
            )
    segments = cuts.compute_segments(words, probe["dur"], ctx.cfg.cuts)
    st.save_json(ctx.state / "segments.json", [s.model_dump() for s in segments])
    kept = sum(s.end - s.start for s in segments)
    _log(f"[cut] {len(segments)} segments, {kept:.1f}s kept of {probe['dur']:.1f}s")


def _load_clips(ctx: Ctx) -> list[dict]:
    """Staged inserted-clip metadata (inserts.json), with local durations filled."""
    path = ctx.state / "inserts.json"
    if not path.exists():
        return []
    clips = st.load_json(path)
    if not isinstance(clips, list):
        return []
    out: list[dict] = []
    for clip in clips:
        if not isinstance(clip, dict):
            continue
        clip = dict(clip)
        if not clip.get("source_dur"):
            local = ctx.state / str(clip.get("file") or "")
            if clip.get("file") and local.exists():
                try:
                    clip["source_dur"] = ff.probe(local)["dur"]
                except Exception as exc:  # noqa: BLE001 - best-effort metadata
                    _LOG.debug("could not probe %s: %s", local, exc)
        out.append(clip)
    return out


def s_plan(ctx: Ctx) -> None:
    probe = st.load_json(ctx.state / "probe.json")
    meta = st.load_model(ctx.state / "meta.json", StateMeta)
    transcript = _load_transcript(ctx)
    segments = _load_segments(ctx)

    if ctx.mock:
        draft = PlanDraft.model_validate_json(
            (FIXTURES / "draft.json").read_text(encoding="utf-8")
        )
        degraded, note = False, "mock run: plan from fixtures"
    else:
        screenshots = (
            sorted(p.name for p in (ctx.attachments or Path(".")).glob("*"))
            if ctx.attachments
            else []
        )
        draft, degraded, note = plan_stage.draft_plan(
            transcript, segments, screenshots, ctx.cfg, ctx.prompt, probe["dur"]
        )
    plan, notes = plan_stage.assemble_plan(
        draft,
        transcript,
        segments,
        ctx.cfg,
        ctx.prompt,
        {
            "ref": meta.ref,
            "sha256": probe["sha256"],
            "w": probe["w"],
            "h": probe["h"],
            "dur": probe["dur"],
        },
        degraded,
        note,
        clips=_load_clips(ctx),
    )
    st.save_model(ctx.state / "edit_plan.json", plan)
    st.save_json(ctx.state / "notes.json", notes)
    degraded_note = f" (degraded: {note})" if degraded and note else ""
    _log(
        f"[plan] {len(plan.segments)} segments, {len(plan.visuals)} visuals, "
        f"{len(plan.captions)} caption spans, {len(plan.inserts)} inserts"
        f"{degraded_note}"
    )


def s_visuals(ctx: Ctx) -> None:
    plan = _load_plan(ctx)
    transcript = _load_transcript(ctx)
    resolved: list[ResolvedVisual] = []

    if ctx.mock:
        for v in plan.visuals:
            if v.kind == "icon" and ctx.cfg.visuals.icons_enabled:
                v.icon = f"mock:{v.keyword or v.id}"
                resolved.append(
                    ResolvedVisual(
                        visual=v,
                        asset=_mock_icon(ctx.icons, v.id, v.keyword or "", ctx.cfg),
                    )
                )
            elif ctx.attachments:
                from vedit.stage.assets import resolve_screenshot

                path = resolve_screenshot(v.file or "", ctx.attachments)
                if path:
                    resolved.append(ResolvedVisual(visual=v, asset=path))
        st.save_model(ctx.state / "edit_plan.json", plan)
    else:
        repick = None if ctx.repick is None else set(ctx.repick)
        targets = [
            v
            for v in plan.visuals
            if v.kind == "icon" and (repick is None or v.id in repick)
        ]
        if not ctx.cfg.visuals.icons_enabled and targets:
            # legacy plans may still carry icons; never pick new ones when off
            _log("[visuals] icons disabled — skipping icon pick")
            targets = []
        target_ids = {v.id for v in targets}
        picks: dict[str, str | None] = {}
        try:
            shortlists = visuals_stage.shortlists(targets, ctx.cfg)
            picks = visuals_stage.pick_icons(shortlists, ctx.cfg)
            if repick is not None:
                # explicit user request: never silently drop the replacement
                for vid, candidates in shortlists.items():
                    if not picks.get(vid) and candidates:
                        picks[vid] = candidates[0]
                        _log(
                            f"[visuals] pick model returned none for {vid} "
                            f"-> top candidate {candidates[0]}"
                        )
        except Exception as exc:  # noqa: BLE001 — iconify outage degrades visuals, not the run
            _warn(f"[visuals] shortlist/pick failed, continuing without icons: {exc}")
        for v in plan.visuals:
            if v.id in target_ids and v.kind == "icon" and picks.get(v.id):
                v.icon = picks[v.id]
        st.save_model(ctx.state / "edit_plan.json", plan)
        resolved = visuals_stage.resolve_visuals(
            plan.visuals,
            transcript,
            ctx.cfg,
            ctx.icons,
            ctx.attachments,
            on_error=_warn,
        )

    manifest = [
        {"id": r.visual.id, "asset": r.asset.relative_to(ctx.state).as_posix()}
        for r in resolved
        if r.asset is not None and r.asset.is_relative_to(ctx.state)
    ]
    st.save_json(ctx.state / "resolved.json", manifest)
    _log(f"[visuals] {len(manifest)}/{len(plan.visuals)} assets resolved")


def s_captions(ctx: Ctx) -> None:
    plan = _load_plan(ctx)
    srt, ass, lines = captions_stage.generate(plan, ctx.cfg, ctx.work)
    st.copy_artifact(srt, ctx.state)
    st.copy_artifact(ass, ctx.state)
    _log(f"[captions] {len(lines)} lines, coverage {captions_stage.coverage(plan):.0%}")


def s_render(ctx: Ctx) -> None:
    plan = _load_plan(ctx)
    resolved = _load_resolved(ctx)
    base = render.ensure_base(ctx.state / "source.mp4", plan, ctx.work, ctx.cfg)
    clean_audio = ctx.work / "audio" / "clean_48k.wav"
    if plan.inserts:
        pieces = render.render_pieces(
            plan, resolved, base, clean_audio, ctx.state, ctx.work, ctx.cfg
        )
        merged = render.assemble_pieces(pieces, ctx.work, ctx.cfg)
        mains = sum(1 for p in pieces if p.kind == "main")
        _log(
            f"[render] final.mp4 ({mains} main pieces + "
            f"{len(plan.inserts)} inserted clips)"
        )
    else:
        segments = render.render_segments(
            plan, resolved, base, clean_audio, ctx.work, ctx.cfg
        )
        merged = render.concat_crossfade(segments, ctx.work, ctx.cfg)
        _log(f"[render] final.mp4 ({len(segments)} segments)")
    render.burn_captions(merged, ctx.work, FONTS_DIR, ctx.state / "final.mp4", ctx.cfg)


def s_qc(ctx: Ctx) -> None:
    plan = _load_plan(ctx)
    resolved = _load_resolved(ctx)
    lines = captions_stage.build_lines(plan, ctx.cfg)
    result = qc_stage.run_qc(
        ctx.state / "final.mp4",
        plan,
        [r.asset for r in resolved if r.asset],
        lines,
        captions_stage.coverage(plan),
        ctx.cfg,
    )
    st.save_json(ctx.state / "qc.json", result.as_dict())
    for check in result.checks:
        if not check["ok"]:
            _warn(
                f"[qc] {'FAIL' if check['severity'] == 'hard' else 'WARN'} {check['name']}: {check['detail']}"
            )
    _log(f"[qc] {'PASS' if result.passed else 'FAIL'}")


def s_report(ctx: Ctx) -> None:
    plan = _load_plan(ctx)
    meta = st.load_model(ctx.state / "meta.json", StateMeta)
    notes = st.load_json(ctx.state / "notes.json")
    qc_result = _qc_result(ctx)
    dur = qc_result.stats.get("expected_output_s", 0.0)
    report_stage.write_report(
        plan, qc_result, ctx.cfg, ctx.state / "edit_report.md", notes, meta.source_ref
    )
    report_stage.contact_sheet(
        ctx.state / "final.mp4", ctx.state / "contact_sheet.jpg", ctx.cfg, dur
    )
    report_stage.preview(ctx.state / "final.mp4", ctx.state / "preview.mp4", ctx.cfg)
    _log("[report] edit_report.md + contact_sheet.jpg + preview.mp4")


# ---------- commands ----------

ALL_STAGES = [
    ("acquire", s_acquire),
    ("audio", s_audio),
    ("transcribe", s_transcribe),
    ("cut", s_cut),
    ("plan", s_plan),
    ("visuals", s_visuals),
    ("captions", s_captions),
    ("render", s_render),
    ("qc", s_qc),
    ("report", s_report),
]


def _run_stages(ctx: Ctx, start: str = "acquire") -> None:
    started = False
    for name, fn in ALL_STAGES:
        if name == start:
            started = True
        if started:
            st.run_stage(ctx.state, name, lambda fn=fn: fn(ctx))


def _budget_report(ctx: Ctx, tg) -> None:
    """Post-delivery storage budget pass: nudge at notify_gb, evict at evict_gb."""
    from vedit.stage.export import maintenance

    s3 = ctx.cfg.export.s3
    if not s3.enabled:
        return
    try:
        total, deleted = maintenance(ctx.cfg)
    except Exception as exc:  # noqa: BLE001 — budget hygiene must not break delivery
        _log(f"[deliver] budget check failed: {exc}")
        return
    used = total / (1 << 30)
    if deleted:
        keys = ", ".join(deleted[-3:]) + (" …" if len(deleted) > 3 else "")
        tg.send_message(
            ctx.chat_id,
            f"storage was over {s3.evict_gb}GB — trimmed to {used:.1f}GB "
            f"(deleted {len(deleted)} oldest exports: {keys})",
        )
        _log(f"[deliver] budget evicted {len(deleted)} files, total now {total}B")
    elif used >= s3.notify_gb:
        tg.send_message(
            ctx.chat_id,
            f"storage at {used:.1f}GB / 10GB free tier — oldest exports get "
            f"deleted automatically above {s3.evict_gb}GB",
        )
        _log(f"[deliver] budget notice at {total}B")


def _deliver(ctx: Ctx) -> None:
    from vedit.stage.export import upload_and_presign
    from vedit.telegram_client import Telegram

    final = ctx.state / "final.mp4"
    qc_result = _qc_result(ctx)
    meta = st.load_model(ctx.state / "meta.json", StateMeta)
    source_dur = st.load_json(ctx.state / "probe.json")["dur"]
    state_ref = meta.ref[:8]  # reply to this message to target this run in a fix
    caption = (
        f"edit ready · {qc_result.stats.get('expected_output_s', 0):.0f}s from {source_dur:.0f}s · "
        f"QC {'PASS' if qc_result.passed else 'FAIL'} · state {state_ref}"
    )
    tg = Telegram()
    if not final.exists():
        raise FileNotFoundError(str(final))
    size = final.stat().st_size

    # Full quality lives on R2 as a time-limited link; the Telegram copy is a
    # teaser (Telegram recompresses sendVideo and caps uploads at 50MB).
    link = None
    if size > 0 and ctx.cfg.export.s3.enabled:
        try:
            link = upload_and_presign(ctx.cfg, ctx.state, state_ref)
        except Exception as exc:  # noqa: BLE001 — deliver must not die on R2 hiccups
            _log(f"[deliver] r2 upload failed, falling back to chat delivery: {exc}")

    fits_telegram = 0 < size <= ctx.cfg.limits.telegram_send_max_bytes
    teaser = ctx.cfg.export.s3.teaser
    if link is not None:
        if fits_telegram and teaser == "document":
            tg.send_document(ctx.chat_id, final, caption + " · bit-exact copy")
            _log(f"[deliver] document sent ({size / 1e6:.1f}MB, bit-exact)")
        elif fits_telegram and teaser == "video":
            tg.send_video(ctx.chat_id, final, caption + " · full quality at link")
            _log(f"[deliver] teaser sent ({size / 1e6:.1f}MB) + r2 link")
        tg.send_message(ctx.chat_id, f"full quality (no re-encode): {link}")
        _log(f"[deliver] link sent ({size / 1e6:.1f}MB)")
        _budget_report(ctx, tg)
        return
    if fits_telegram:
        tg.send_video(ctx.chat_id, final, caption)
        _log(f"[deliver] final.mp4 sent ({size / 1e6:.1f}MB)")
        _budget_report(ctx, tg)
        return
    preview = ctx.state / "preview.mp4"
    if preview.exists():
        tg.send_video(
            ctx.chat_id, preview, caption + " (preview — full file exceeds 50MB)"
        )
    sheet = ctx.state / "contact_sheet.jpg"
    if sheet.exists():
        tg.send_photo(ctx.chat_id, sheet)
    tg.send_message(
        ctx.chat_id,
        f"full video is {size / 1e6:.0f}MB (Telegram cap 50MB) — state {state_ref}",
    )
    _log("[deliver] oversized: sent preview + contact sheet")
    _budget_report(ctx, tg)


def _parse_file_ids(raw: str) -> list[str]:
    """Attach file ids from a JSON array or CSV string.

    Accepts plain id strings and worker-style {"id": ..., "note": ...}
    objects (the relay stages images with notes); anything else is skipped.
    Raises SystemExit on malformed JSON instead of a traceback.
    """
    import json as _json

    text = (raw or "").strip()
    if not text:
        return []
    if text.startswith("{"):
        raise SystemExit("bad --file-ids JSON: expected an array")
    if text.startswith("["):
        try:
            items = _json.loads(text)
        except ValueError as exc:
            raise SystemExit(f"bad --file-ids JSON: {exc}") from exc
        if not isinstance(items, list):
            raise SystemExit("bad --file-ids JSON: expected an array")
        out: list[str] = []
        for item in items:
            if isinstance(item, dict):
                fid = item.get("id") or item.get("file_id")
                if fid:
                    out.append(str(fid))
            elif isinstance(item, str) and item.strip():
                out.append(item.strip())
        return out
    return [x.strip() for x in text.split(",") if x.strip()]


def _parse_clips(raw: str) -> list[dict]:
    """Inserted-clip specs from the worker (JSON array of objects)."""
    import json as _json

    text = (raw or "").strip()
    if not text:
        return []
    try:
        items = _json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"bad --clips JSON: {exc}") from exc
    if not isinstance(items, list):
        raise SystemExit("bad --clips JSON: expected an array")
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        fid = item.get("file_id") or item.get("id")
        if fid:
            out.append({**item, "file_id": str(fid)})
    return out


def _attach_clips(state: Path, clips: list[dict], cfg: Config) -> None:
    """Download inserted clips and merge their metadata into inserts.json."""
    from vedit.telegram_client import Telegram

    ins_dir = state / "inserts"
    ins_dir.mkdir(parents=True, exist_ok=True)
    meta_path = state / "inserts.json"
    existing = st.load_json(meta_path) if meta_path.exists() else []
    max_n = 0
    for m in existing:
        name = str(m.get("id") or "")
        if name.startswith("clip-") and name[5:].isdigit():
            max_n = max(max_n, int(name[5:]))
    tg = Telegram()
    for spec in clips:
        explicit = str(spec.get("name") or "").strip()
        if explicit.startswith("clip-") and explicit[5:].isdigit():
            name = explicit
        else:
            max_n += 1
            name = f"clip-{max_n}"
        info = tg.resolve(spec["file_id"])
        ext = Path(str(info.get("file_path") or "")).suffix.lower() or ".mp4"
        fname = f"{name}{ext}"
        dest = ins_dir / fname
        tg.download(spec["file_id"], dest, cfg.limits.telegram_download_max_bytes)
        entry = {
            "id": name,
            "file": f"inserts/{fname}",
            "file_id": spec["file_id"],
            "at": spec.get("at"),
            "duration": spec.get("duration"),
            "trim_start": spec.get("trim_start") or 0.0,
            "muted": bool(spec.get("muted", not cfg.inserts.audio)),
            "fit": spec.get("fit") or cfg.inserts.fit,
            "label": spec.get("label") or "",
            "source_dur": spec.get("source_dur"),
        }
        existing = [m for m in existing if m.get("id") != name]
        existing.append(entry)
        _log(f"[attach] {name} ({dest.stat().st_size} bytes)")


def cmd_attach(args: argparse.Namespace) -> int:
    """Download Telegram attachments/clips into a run state (pre-process step)."""
    cfg = load_config(args.config)
    state = Path(args.state)
    (state / "attachments").mkdir(parents=True, exist_ok=True)
    file_ids = _parse_file_ids(args.file_ids)
    clips = _parse_clips(getattr(args, "clips", ""))
    if not file_ids and not clips:
        _log("[attach] no file ids")
        return 0
    if file_ids:
        from vedit.telegram_client import Telegram

        tg = Telegram()
        # Numbered names match the bot's "saved image N" confirmations, so the
        # owner can say "place image 1 at 0:20". Continue past existing ones.
        start = 0
        for p in (state / "attachments").iterdir():
            num = p.name.removeprefix("image-").split(".")[0]
            if num.isdigit():
                start = max(start, int(num))
        for k, fid in enumerate(file_ids, start=start + 1):
            info = tg.resolve(fid)
            ext = Path(str(info.get("file_path") or "")).suffix or ".jpg"
            name = f"image-{k}{ext}"
            dest = state / "attachments" / name
            tg.download(fid, dest, cfg.limits.telegram_download_max_bytes)
            _log(f"[attach] {name} ({dest.stat().st_size} bytes)")
    if clips:
        _attach_clips(state, clips, cfg)
    return 0


def _ctx_from_state(state: Path, cfg: Config, prompt: str, chat_id: str) -> Ctx:
    meta = st.load_model(state / "meta.json", StateMeta)
    st.ensure_dirs(state)  # downloaded CI state lacks empty dirs (icons/, work/)
    return Ctx(
        state=state,
        cfg=cfg,
        mock=meta.mock,
        prompt=prompt,
        input_ref=meta.source_ref,
        chat_id=chat_id or meta.chat_id,
    )


def cmd_process(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    ff.require_tools()
    state = st.ensure_state(Path(args.out), args.name or "run")
    if args.from_stage:
        st.clear_from(state, args.from_stage)
    if args.attachments:
        import shutil

        target = state / "attachments"
        if not target.exists():
            shutil.copytree(args.attachments, target)

    ctx = Ctx(
        state=state,
        cfg=cfg,
        mock=args.mock,
        prompt=args.prompt or "",
        input_ref=args.input or "",
        chat_id=args.chat_id or "",
    )
    _run_stages(ctx, "acquire")

    qc_passed = _qc_result(ctx).passed if (state / "qc.json").exists() else False
    if args.chat_id and qc_passed:
        _deliver_guarded(ctx)
    return 0 if qc_passed else 2


def cmd_fix(args: argparse.Namespace) -> int:
    """Structured correction loop (implementation.md §9): patch, never re-plan."""
    cfg = load_config(args.config)
    ff.require_tools()
    state = Path(args.state)
    if not args.apply_preview and not (args.instruction or "").strip():
        raise SystemExit("fix needs --instruction (or --apply-preview)")
    plan = st.load_model(state / "edit_plan.json", EditPlan)
    ctx = _ctx_from_state(state, cfg, plan.prompt, args.chat_id or "")
    transcript = _load_transcript(ctx)
    zoom_cap = max(1, len(plan.segments) // ctx.cfg.video.max_zoom_segments_div)
    _log(f"[fix] {args.instruction}")
    shots = (
        sorted(p.name for p in ctx.attachments.iterdir() if p.is_file())
        if ctx.attachments
        else []
    )
    clips = _load_clips(ctx)

    if args.dry_run:
        preview = fix_stage.preview_fix(
            args.instruction,
            plan,
            transcript,
            cfg,
            mock=ctx.mock,
            screenshots=shots,
            icons_enabled=ctx.cfg.visuals.icons_enabled,
            max_zooms=zoom_cap,
            clips=clips,
        )
        frame: Path | None = None
        if preview.get("ok") and "input_range_s" in preview:
            existing = sorted((state / "preview").glob("fix_preview_*.jpg"))
            out = state / "preview" / f"fix_preview_{len(existing) + 1}.jpg"
            frame = fix_stage.preview_frame(
                state / "source.mp4",
                out,
                preview["input_range_s"][0],
                f"input {preview['input_range_s'][0]:.1f}s - {preview['op']}",
                FONTS_DIR if FONTS_DIR.exists() else None,
            )
            if frame is not None:
                preview["frame"] = frame.relative_to(state).as_posix()
        st.save_json(state / "fix_preview.json", preview)
        _log(f"[fix:preview] {fix_stage.confirmation_text(preview)}")
        return 0 if preview.get("ok") else 2

    try:
        notes_prefix: list[str] = []
        if args.apply_preview:
            preview_file = state / "fix_preview.json"
            if not preview_file.exists():
                raise SystemExit("no fix_preview.json — run fix --dry-run first")
            data = st.load_json(preview_file)
            if data.get("plan_hash") != fix_stage.plan_hash(plan):
                # The YES authorized the instruction, not the byte-plan: the plan
                # moved on (usually the owner's own later edit), so re-resolve
                # the same instruction deterministically instead of refusing.
                instruction = (data.get("instruction") or "").strip()
                if not instruction:
                    raise SystemExit(
                        "plan changed since preview and it carries no "
                        "instruction — re-run fix --dry-run"
                    )
                _log("[fix] preview stale (plan moved on) — re-resolving instruction")
                patch = fix_stage.parse_fix(
                    instruction,
                    plan,
                    transcript,
                    cfg,
                    mock=ctx.mock,
                    screenshots=shots,
                    clips=clips,
                )
                notes_prefix.append(
                    "re-resolved after plan drift (preview was for an older plan)"
                )
            else:
                patch = FixPatch.model_validate(data["patch"])
                _log(f"[fix] applying previewed {patch.op.value} (no re-parse)")
        else:
            patch = fix_stage.parse_fix(
                args.instruction,
                plan,
                transcript,
                cfg,
                mock=ctx.mock,
                screenshots=shots,
                clips=clips,
            )
        new_plan, notes = fix_stage.apply_patch(
            patch,
            plan,
            transcript,
            icons_enabled=ctx.cfg.visuals.icons_enabled,
            screenshots=shots,
            max_zooms=zoom_cap,
            presets=set(ctx.cfg.audio.presets or {}),
            clips=clips,
            insert_cfg=cfg.inserts,
        )
        notes = notes_prefix + notes

        if patch.op == FixOp.set_preset:
            name = (patch.text or "").strip()
            try:
                audio_stage.with_preset(ctx.cfg, name)
            except audio_stage.AudioError as exc:
                raise fix_stage.FixError(str(exc)) from exc
            st.save_json(state / "preset.json", {"preset": name})
            work_audio = state / "work" / "audio"
            for f in ("clean_48k.wav", "stt_16k.wav"):
                stale = work_audio / f
                if stale.exists():
                    stale.unlink()
            # re-prep audio only; the transcript marker stays valid
            st.clear_stage(state, "audio")
            _log(f"[fix] audio recipe set to '{name}' — rebuilding the clean track")
    except fix_stage.FixError as exc:
        _log(f"[fix] {exc}")
        if ctx.chat_id:
            _notify(ctx.chat_id, str(exc))
        return 0  # handled: state untouched, run stays chainable

    st.save_model(state / "edit_plan.json", new_plan)
    st.save_json(
        state / "segments.json", [s.model_dump() for s in new_plan.segments]
    )  # cut/keep fixes rewrite segments too
    notes_file = state / "notes.json"
    existing = st.load_json(notes_file) if notes_file.exists() else []
    st.save_json(notes_file, existing + notes)
    st.mark_done(state, "plan")  # never let a missing marker re-plan over the patch
    st.clear_from(state, "visuals")  # icons/manifest/captions/render must rebuild
    ctx.repick = (
        [patch.visual_id] if patch.op == FixOp.replace_icon and patch.visual_id else []
    )
    _log(f"[fix] {patch.op.value} applied — re-rendering")

    _run_stages(ctx, "acquire")  # transcribe/cut/plan skipped by markers

    qc_passed = _qc_result(ctx).passed
    if ctx.chat_id and qc_passed:
        _deliver_guarded(ctx)
    return 0 if qc_passed else 2


def _deliver_guarded(ctx: Ctx) -> None:
    """Deliver, persisting a human-readable error for the workflow failure step.

    A rendered, QC-passed video must never die silently at the send call:
    the message tells the owner the video is ready and how to retry.
    """
    try:
        _deliver(ctx)
    except Exception as exc:
        try:
            (ctx.state / "last_error.txt").write_text(
                f"video ready but delivery failed: {exc}\n"
                "reply 'resend' and I'll try again",
                encoding="utf-8",
            )
        except OSError:
            pass
        raise


def cmd_qc(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    ff.require_tools()
    state = Path(args.state)
    st.clear_from(state, "qc")
    ctx = _ctx_from_state(state, cfg, "", "")
    _run_stages(ctx, "qc")
    return 0 if _qc_result(ctx).passed else 2


def cmd_report(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    ff.require_tools()
    state = Path(args.state)
    st.clear_from(state, "report")
    ctx = _ctx_from_state(state, cfg, "", "")
    _run_stages(ctx, "report")
    return 0


def cmd_deliver(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    ff.require_tools()
    state = Path(args.state)
    ctx = _ctx_from_state(state, cfg, "", args.chat_id or "")
    if not ctx.chat_id:
        raise SystemExit("no chat_id: pass --chat-id or set it at process time")
    if not _qc_result(ctx).passed:
        raise SystemExit("QC has not passed — delivery is blocked")
    _deliver_guarded(ctx)
    return 0


def _load_keys_file() -> None:
    keys = PROJECT_ROOT / "keys.txt"
    if not keys.exists():
        return
    for line in keys.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vedit", description="AI video editing agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("process", help="run the full pipeline")
    p.add_argument(
        "--input", default="", help="Drive link, URL, local path, or tg:<file_id>"
    )
    p.add_argument("--out", default="out", help="output root directory")
    p.add_argument("--name", default="", help="run/state name (default: run)")
    p.add_argument("--prompt", default="", help="owner instruction for the edit")
    p.add_argument(
        "--attachments", default="", help="directory of screenshot attachments"
    )
    p.add_argument("--config", default="config.yaml")
    p.add_argument(
        "--mock", action="store_true", help="offline fixtures, no LLM/network for plan"
    )
    p.add_argument("--from-stage", default="", choices=[s for s, _ in ALL_STAGES])
    p.add_argument(
        "--chat-id", default="", help="deliver the result to this Telegram chat"
    )
    p.set_defaults(func=cmd_process)

    f = sub.add_parser(
        "fix", help="apply a correction and re-render from the plan stage"
    )
    f.add_argument("--state", required=True)
    f.add_argument("--instruction", default="")
    f.add_argument(
        "--dry-run",
        action="store_true",
        help="preview only: write state/fix_preview.json + tagged frame, change nothing",
    )
    f.add_argument(
        "--apply-preview",
        action="store_true",
        help="apply state/fix_preview.json without re-parsing (refuses on plan drift)",
    )
    f.add_argument("--config", default="config.yaml")
    f.add_argument("--chat-id", default="")
    f.set_defaults(func=cmd_fix)

    q = sub.add_parser("qc", help="re-run the QC gate")
    q.add_argument("--state", required=True)
    q.add_argument("--config", default="config.yaml")
    q.set_defaults(func=cmd_qc)

    r = sub.add_parser("report", help="re-generate the report artifacts")
    r.add_argument("--state", required=True)
    r.add_argument("--config", default="config.yaml")
    r.set_defaults(func=cmd_report)

    d = sub.add_parser("deliver", help="send final.mp4 to Telegram")
    d.add_argument("--state", required=True)
    d.add_argument("--chat-id", default="")
    d.add_argument("--config", default="config.yaml")
    d.set_defaults(func=cmd_deliver)

    a = sub.add_parser("attach", help="download Telegram attachments into a run state")
    a.add_argument("--state", required=True)
    a.add_argument(
        "--file-ids", required=True, help="JSON array or comma-separated file_ids"
    )
    a.add_argument("--clips", default="", help="JSON array of inserted-clip specs")
    a.add_argument("--config", default="config.yaml")
    a.set_defaults(func=cmd_attach)
    return parser


def _error_state_dir(args: argparse.Namespace) -> Path:
    state = getattr(args, "state", "") or ""
    if state:
        return Path(state)
    return Path(getattr(args, "out", "") or "out") / (
        getattr(args, "name", "") or "run"
    )


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    _load_keys_file()
    args = build_parser().parse_args(argv)
    for attr in ("attachments",):
        if hasattr(args, attr):
            setattr(args, attr, getattr(args, attr) or "")
    try:
        return args.func(args)
    except Exception:
        # The workflow's failure report sends last_error.txt to the chat; without
        # this, crashes mid-stage surfaced only as "no video produced".
        try:
            target = _error_state_dir(args)
            target.mkdir(parents=True, exist_ok=True)
            error_file = target / "last_error.txt"
            existing = (
                error_file.read_text(encoding="utf-8") if error_file.exists() else ""
            )
            if "delivery failed" not in existing:
                error_file.write_text(traceback.format_exc()[-6000:], encoding="utf-8")
        except OSError as exc:
            _LOG.warning("could not write last_error.txt: %s", exc)
        raise


if __name__ == "__main__":
    sys.exit(main())
