from __future__ import annotations

import json
from pathlib import Path

import pytest

from vedit.cli import main

ROOT = Path(__file__).resolve().parent.parent.parent
CFG = ["--config", str(ROOT / "config.yaml")]


def _icons_cfg(tmp_path: Path) -> list[str]:
    """Temp config copy with Iconify overlays enabled (icon-path coverage)."""
    import yaml

    data = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    data["visuals"]["icons_enabled"] = True
    path = tmp_path / "icons_config.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return ["--config", str(path)]


def _process(tmp_path: Path, name: str) -> list[str]:
    return ["process", "--mock", "--name", name, "--out", str(tmp_path), *CFG]


def _process_icons(tmp_path: Path, name: str) -> list[str]:
    return [
        "process",
        "--mock",
        "--name",
        name,
        "--out",
        str(tmp_path),
        *_icons_cfg(tmp_path),
    ]


def test_mock_process_passes_qc(tmp_path):
    assert main(_process(tmp_path, "e2e")) == 0

    state = tmp_path / "e2e"
    for artifact in (
        "final.mp4",
        "captions.srt",
        "captions.ass",
        "qc.json",
        "edit_report.md",
        "contact_sheet.jpg",
        "preview.mp4",
        "edit_plan.json",
        "transcript.json",
        "segments.json",
        "resolved.json",
        "meta.json",
    ):
        assert (state / artifact).exists(), artifact

    qc = json.loads((state / "qc.json").read_text(encoding="utf-8"))
    assert qc["passed"] is True
    assert all(c["ok"] for c in qc["checks"] if c["severity"] == "hard")

    # second run is a no-op thanks to stage markers
    assert main(_process(tmp_path, "e2e")) == 0
    # standalone qc subcommand agrees
    assert main(["qc", "--state", str(state), *CFG]) == 0


def test_fix_patches_without_replan(tmp_path):
    assert main(_process_icons(tmp_path, "fixrun")) == 0
    state = tmp_path / "fixrun"
    before = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True

    rc = main(
        [
            "fix",
            "--state",
            str(state),
            "--instruction",
            "remove the first visual",
            *_icons_cfg(tmp_path),
        ]
    )
    assert rc == 0
    after = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    assert after["prompt"] == before["prompt"], "fix must never re-plan"
    assert len(after["visuals"]) == len(before["visuals"]) - 1
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True
    for stage in ("plan", "visuals", "captions", "render", "qc", "report"):
        assert (state / f".done.{stage}").exists()


def test_fix_unknown_op_is_rejected_without_render(tmp_path):
    assert main(_process(tmp_path, "fixunk")) == 0
    state = tmp_path / "fixunk"
    plan_before = (state / "edit_plan.json").read_text(encoding="utf-8")
    qc_before = (state / "qc.json").read_text(encoding="utf-8")

    rc = main(["fix", "--state", str(state), "--instruction", "make it sparkle", *CFG])
    assert rc == 0
    assert (state / "edit_plan.json").read_text(encoding="utf-8") == plan_before
    assert (state / "qc.json").read_text(encoding="utf-8") == qc_before


def test_fix_replace_icon_updates_plan(tmp_path):
    assert main(_process_icons(tmp_path, "fixicon")) == 0
    state = tmp_path / "fixicon"
    before = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    rc = main(
        [
            "fix",
            "--state",
            str(state),
            "--instruction",
            "use a shield instead",
            *_icons_cfg(tmp_path),
        ]
    )
    assert rc == 0
    after = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    assert after["prompt"] == before["prompt"]
    shielded = [v for v in after["visuals"] if v.get("keyword") == "shield"]
    assert len(shielded) >= 1
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True


def test_deliver_requires_chat_id(tmp_path):
    assert main(_process(tmp_path, "dlv")) == 0
    with pytest.raises(SystemExit):
        main(["deliver", "--state", str(tmp_path / "dlv"), *CFG])


def test_screenshots_only_places_input_image(tmp_path):
    from PIL import Image

    shots = tmp_path / "shots"
    shots.mkdir()
    Image.new("RGB", (320, 200), (30, 120, 200)).save(shots / "diagram.png")

    assert (
        main(
            [
                "process",
                "--mock",
                "--name",
                "shots",
                "--out",
                str(tmp_path),
                "--attachments",
                str(shots),
                *CFG,
            ]
        )
        == 0
    )
    state = tmp_path / "shots"
    plan = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    assert plan["visuals"], "screenshot draft visual must survive assembly"
    assert all(v["kind"] == "screenshot" for v in plan["visuals"]), "no icons allowed"
    assert not any(
        str(v.get("icon") or "").startswith("mock:") for v in plan["visuals"]
    )
    resolved = json.loads((state / "resolved.json").read_text(encoding="utf-8"))
    assert len(resolved) == 1
    assert resolved[0]["asset"].endswith("diagram.png")
    assert (state / "attachments" / "diagram.png").exists()
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True


def test_fix_can_place_input_image(tmp_path):
    from PIL import Image

    shots = tmp_path / "shots2"
    shots.mkdir()
    Image.new("RGB", (320, 200), (30, 120, 200)).save(shots / "diagram.png")

    assert (
        main(
            [
                "process",
                "--mock",
                "--name",
                "shotsfix",
                "--out",
                str(tmp_path),
                "--attachments",
                str(shots),
                *CFG,
            ]
        )
        == 0
    )
    state = tmp_path / "shotsfix"
    before = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    rc = main(
        [
            "fix",
            "--state",
            str(state),
            "--instruction",
            "place the diagram at the start",
            *CFG,
        ]
    )
    assert rc == 0
    after = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    assert len(after["visuals"]) == len(before["visuals"]) + 1
    assert after["visuals"][-1]["file"] == "diagram.png"
    resolved = json.loads((state / "resolved.json").read_text(encoding="utf-8"))
    assert len(resolved) == len(after["visuals"])
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True

    # vague follow-up ("add it") reuses the staged image near the start
    rc = main(
        [
            "fix",
            "--state",
            str(state),
            "--instruction",
            "add it to this video",
            *CFG,
        ]
    )
    assert rc == 0
    vague = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    assert len(vague["visuals"]) == len(after["visuals"]) + 1
    assert vague["visuals"][-1]["file"] == "diagram.png"
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True


def test_fix_cut_and_confirm_flow(tmp_path):
    assert main(_process(tmp_path, "cutflow")) == 0
    state = tmp_path / "cutflow"
    before = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    kept_before = sum(s["end"] - s["start"] for s in before["segments"])

    # direct cut re-times the plan and re-renders through QC
    assert (
        main(["fix", "--state", str(state), "--instruction", "cut 0:05 to 0:10", *CFG])
        == 0
    )
    after = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    kept_after = sum(s["end"] - s["start"] for s in after["segments"])
    assert kept_after < kept_before
    assert after["segments"], "at least one segment must survive"
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True

    # dry-run zoom preview changes nothing but writes preview + tagged frame
    plan_bytes = (state / "edit_plan.json").read_bytes()
    assert (
        main(
            [
                "fix",
                "--state",
                str(state),
                "--instruction",
                "zoom in at 0:15",
                "--dry-run",
                *CFG,
            ]
        )
        == 0
    )
    preview = json.loads((state / "fix_preview.json").read_text(encoding="utf-8"))
    assert preview["ok"] is True and preview["op"] == "add_zoom"
    assert "input_range_s" in preview and "output_range_s" in preview
    assert "frame" in preview
    assert (state / preview["frame"]).exists()
    assert (state / "edit_plan.json").read_bytes() == plan_bytes

    # applying the preview lands the zoom and passes QC
    assert main(["fix", "--state", str(state), "--apply-preview", *CFG]) == 0
    final = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    assert len(final["zoom_at_words"]) == len(after["zoom_at_words"]) + 1
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True
