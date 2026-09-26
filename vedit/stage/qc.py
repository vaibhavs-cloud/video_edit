"""Final-output QC gate. Hard failures block delivery; warnings go in the report."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vedit import ff
from vedit.config import Config
from vedit.schema import EditPlan
from vedit.stage.captions import CaptionLine
from vedit.stage.timeline import TimelineMap


@dataclass
class QcResult:
    checks: list[dict] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def hard_failures(self) -> list[dict]:
        return [c for c in self.checks if c["severity"] == "hard" and not c["ok"]]

    @property
    def warnings(self) -> list[dict]:
        return [c for c in self.checks if c["severity"] == "warn" and not c["ok"]]

    @property
    def passed(self) -> bool:
        return not self.hard_failures

    def as_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "stats": self.stats}


def _fps(stream: dict) -> float:
    rate = stream.get("r_frame_rate", "0/1")
    num, _, den = str(rate).partition("/")
    try:
        return float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        return 0.0


def run_qc(
    final: Path,
    plan: EditPlan,
    resolved_assets: list[Path],
    caption_lines: list[CaptionLine],
    coverage_frac: float,
    cfg: Config,
) -> QcResult:
    result = QcResult()
    timeline = TimelineMap(plan.segments)
    expected = timeline.output_dur
    result.stats = {
        "expected_output_s": round(expected, 3),
        "segments": len(plan.segments),
        "visuals_planned": len(plan.visuals),
        "visuals_resolved": len(resolved_assets),
        "caption_lines": len(caption_lines),
        "coverage": round(coverage_frac, 4),
    }

    def add(name: str, ok: bool, severity: str, detail: str) -> None:
        result.checks.append(
            {"name": name, "ok": ok, "severity": severity, "detail": detail}
        )

    if not final.exists() or final.stat().st_size == 0:
        add("final_exists", False, "hard", f"missing or empty: {final}")
        return result
    add("final_exists", True, "hard", f"{final.stat().st_size} bytes")

    try:
        info = ff.probe(final)
        v = ff.video_stream(info)
        ff.audio_stream(info)
        add("has_streams", True, "hard", "video + audio present")
    except Exception as exc:  # noqa: BLE001
        add("has_streams", False, "hard", str(exc))
        return result

    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    add(
        "resolution",
        w == cfg.video.width and h == cfg.video.height,
        "hard",
        f"{w}x{h}, expected {cfg.video.width}x{cfg.video.height}",
    )
    fps = _fps(v)
    add("fps", abs(fps - cfg.video.fps) <= 0.5, "hard", f"{fps:.2f} fps")

    dur = ff.duration_of(info)
    drift = abs(dur - expected)
    add(
        "duration",
        drift <= cfg.qc.duration_tolerance_s,
        "hard",
        f"{dur:.2f}s vs expected {expected:.2f}s (drift {drift:.2f}s)",
    )

    add(
        "caption_coverage",
        coverage_frac >= cfg.qc.min_word_coverage,
        "hard",
        f"{coverage_frac:.0%} of words, min {cfg.qc.min_word_coverage:.0%}",
    )

    add(
        "output_limit",
        dur <= cfg.limits.max_output_s,
        "warn",
        f"{dur:.1f}s vs limit {cfg.limits.max_output_s:.0f}s",
    )
    add(
        "degraded_plan",
        not plan.degraded,
        "warn",
        "plan degraded to captions-only" if plan.degraded else "full plan",
    )
    dropped = len(plan.visuals) - len(resolved_assets)
    add(
        "visuals_resolved",
        dropped == 0,
        "warn",
        f"{len(resolved_assets)}/{len(plan.visuals)} assets resolved",
    )
    return result
