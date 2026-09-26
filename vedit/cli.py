"""CLI entry point: process | fix | qc | report | deliver.

Every stage is marker-gated in the run state directory, so an interrupted or
rejected run resumes exactly where it stopped. Business logic lives in the
stage modules; this file only orchestrates.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from vedit import ff
from vedit import state as st
from vedit.acquire import acquire, verify_media
from vedit.config import Config, load_config
from vedit.schema import EditPlan, FixOp, PlanDraft, Segment, StateMeta, Transcript
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


def _log(msg: str) -> None:
    print(msg, flush=True)


def _notify(chat_id: str, text: str) -> None:
    """Best-effort user-facing message (fix rejections, hints). Never raises."""
    try:
        from vedit.telegram_client import Telegram

        Telegram().send_message(chat_id, text[:4000])
    except Exception as exc:  # noqa: BLE001 — a failed hint must not kill the run
        _log(f"[notify] failed: {exc}")


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
    paths = audio_stage.prepare(ctx.state / "source.mp4", ctx.work, ctx.cfg)
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
    segments = cuts.compute_segments(
        _load_transcript(ctx).words, probe["dur"], ctx.cfg.cuts
    )
    st.save_json(ctx.state / "segments.json", [s.model_dump() for s in segments])
    kept = sum(s.end - s.start for s in segments)
    _log(f"[cut] {len(segments)} segments, {kept:.1f}s kept of {probe['dur']:.1f}s")


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
    )
    st.save_model(ctx.state / "edit_plan.json", plan)
    st.save_json(ctx.state / "notes.json", notes)
    degraded_note = f" (degraded: {note})" if degraded and note else ""
    _log(
        f"[plan] {len(plan.segments)} segments, {len(plan.visuals)} visuals, "
        f"{len(plan.captions)} caption spans{degraded_note}"
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
            _log(f"[visuals] shortlist/pick failed, continuing without icons: {exc}")
        for v in plan.visuals:
            if v.id in target_ids and v.kind == "icon" and picks.get(v.id):
                v.icon = picks[v.id]
        st.save_model(ctx.state / "edit_plan.json", plan)
        resolved = visuals_stage.resolve_visuals(
            plan.visuals, transcript, ctx.cfg, ctx.icons, ctx.attachments, on_error=_log
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
    segments = render.render_segments(
        plan, resolved, base, ctx.work / "audio" / "clean_48k.wav", ctx.work, ctx.cfg
    )
    merged = render.concat(segments, ctx.work)
    render.burn_captions(merged, ctx.work, FONTS_DIR, ctx.state / "final.mp4", ctx.cfg)
    _log(f"[render] final.mp4 ({len(segments)} segments)")


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
            _log(
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


def _deliver(ctx: Ctx) -> None:
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
    size = final.stat().st_size if final.exists() else 0
    if size <= ctx.cfg.limits.telegram_send_max_bytes:
        tg.send_video(ctx.chat_id, final, caption)
        _log(f"[deliver] final.mp4 sent ({size / 1e6:.1f}MB)")
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


def cmd_attach(args: argparse.Namespace) -> int:
    """Download Telegram screenshot attachments into a run state (pre-process step)."""
    import json as _json

    cfg = load_config(args.config)
    state = Path(args.state)
    (state / "attachments").mkdir(parents=True, exist_ok=True)
    raw = args.file_ids.strip()
    file_ids = (
        _json.loads(raw)
        if raw.startswith("[")
        else [x.strip() for x in raw.split(",") if x.strip()]
    )
    if not file_ids:
        _log("[attach] no file ids")
        return 0
    from vedit.telegram_client import Telegram

    tg = Telegram()
    for i, fid in enumerate(file_ids):
        info = tg.resolve(fid)
        name = Path(str(info.get("file_path") or f"file_{i}")).name or f"file_{i}.bin"
        dest = state / "attachments" / name
        tg.download(fid, dest, cfg.limits.telegram_download_max_bytes)
        _log(f"[attach] {name} ({dest.stat().st_size} bytes)")
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
        _deliver(ctx)
    return 0 if qc_passed else 2


def cmd_fix(args: argparse.Namespace) -> int:
    """Structured correction loop (implementation.md §9): patch, never re-plan."""
    cfg = load_config(args.config)
    ff.require_tools()
    state = Path(args.state)
    plan = st.load_model(state / "edit_plan.json", EditPlan)
    ctx = _ctx_from_state(state, cfg, plan.prompt, args.chat_id or "")
    transcript = _load_transcript(ctx)
    _log(f"[fix] {args.instruction}")

    try:
        patch = fix_stage.parse_fix(
            args.instruction, plan, transcript, cfg, mock=ctx.mock
        )
        new_plan, notes = fix_stage.apply_patch(
            patch, plan, transcript, icons_enabled=ctx.cfg.visuals.icons_enabled
        )
    except fix_stage.FixError as exc:
        _log(f"[fix] {exc}")
        if ctx.chat_id:
            _notify(ctx.chat_id, str(exc))
        return 0  # handled: state untouched, run stays chainable

    st.save_model(state / "edit_plan.json", new_plan)
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
        _deliver(ctx)
    return 0 if qc_passed else 2


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
    _deliver(ctx)
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
    f.add_argument("--instruction", required=True)
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
    a.add_argument("--config", default="config.yaml")
    a.set_defaults(func=cmd_attach)
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_keys_file()
    args = build_parser().parse_args(argv)
    for attr in ("attachments",):
        if hasattr(args, attr):
            setattr(args, attr, getattr(args, attr) or "")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
