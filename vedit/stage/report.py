"""Human-readable artifacts: edit_report.md, contact sheet, compressed preview."""

from __future__ import annotations

from pathlib import Path

from vedit import ff
from vedit.config import Config
from vedit.schema import EditPlan
from vedit.stage.qc import QcResult
from vedit.stage.timeline import TimelineMap


def contact_sheet(final: Path, out: Path, cfg: Config, dur: float) -> Path | None:
    step = max(1, round(dur / 6))
    try:
        ff.ffmpeg(
            [
                "-i",
                str(final),
                "-vf",
                f"fps=1/{step},scale=270:480,tile=3x2",
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(out),
            ],
            timeout=300,
        )
        return out if out.exists() else None
    except Exception:  # noqa: BLE001 — cosmetic artifact, never blocks
        return None


def preview(final: Path, out: Path, cfg: Config) -> Path | None:
    try:
        ff.ffmpeg(
            [
                "-i",
                str(final),
                "-vf",
                f"scale={cfg.video.width // 2}:{cfg.video.height // 2}",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "30",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                str(out),
            ],
            timeout=600,
        )
        return out if out.exists() else None
    except Exception:  # noqa: BLE001
        return None


def write_report(
    plan: EditPlan,
    qc: QcResult,
    cfg: Config,
    out: Path,
    notes: list[str],
    input_ref: str,
) -> Path:
    timeline = TimelineMap(plan.segments)
    lines = [
        "# Edit report",
        "",
        f"- Input: `{input_ref}`",
        f"- Source: {plan.source.w}x{plan.source.h}, {plan.source.dur:.1f}s, sha256 `{plan.source.sha256[:12]}`",
        f"- Reframe: {plan.reframe.mode} (x_frac={plan.reframe.x_frac})",
        (
            f"- Kept: {len(plan.segments)} segments, {timeline.output_dur:.1f}s "
            f"of {plan.source.dur:.1f}s ({timeline.output_dur / max(plan.source.dur, 1e-6):.0%})"
        ),
        (
            f"- Captions: {qc.stats.get('caption_lines', 0)} lines, "
            f"coverage {qc.stats.get('coverage', 0):.0%}"
        ),
        f"- Visuals: {qc.stats.get('visuals_resolved', 0)}/{qc.stats.get('visuals_planned', 0)} resolved",
        f"- QC: {'PASS' if qc.passed else 'FAIL'}",
        "",
        "## Checks",
        "",
    ]
    for c in qc.checks:
        mark = "PASS" if c["ok"] else ("FAIL" if c["severity"] == "hard" else "WARN")
        lines.append(f"- [{mark}] {c['name']}: {c['detail']}")
    if notes:
        lines += ["", "## Notes", ""] + [f"- {n}" for n in notes]
    lines += [
        "",
        "## Reproduce",
        "",
        f"```bash\nvedit process --input {input_ref} --prompt {plan.prompt or '""'}\n```",
        "",
        f"Config hash: `{cfg.hash}`",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    return out
