from __future__ import annotations

import json
from pathlib import Path

import pytest

from vedit.cli import main

ROOT = Path(__file__).resolve().parent.parent.parent
CFG = ["--config", str(ROOT / "config.yaml")]


def _process(tmp_path: Path, name: str) -> list[str]:
    return ["process", "--mock", "--name", name, "--out", str(tmp_path), *CFG]


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


def test_fix_reruns_from_plan(tmp_path):
    assert main(_process(tmp_path, "fixrun")) == 0
    state = tmp_path / "fixrun"
    before = json.loads((state / "qc.json").read_text(encoding="utf-8"))
    assert before["passed"] is True

    rc = main(["fix", "--state", str(state), "--instruction", "fewer visuals", *CFG])
    assert rc == 0
    plan = json.loads((state / "edit_plan.json").read_text(encoding="utf-8"))
    assert plan["prompt"].endswith("CORRECTION: fewer visuals")
    assert json.loads((state / "qc.json").read_text(encoding="utf-8"))["passed"] is True
    for stage in ("plan", "visuals", "captions", "render", "qc", "report"):
        assert (state / f".done.{stage}").exists()


def test_deliver_requires_chat_id(tmp_path):
    assert main(_process(tmp_path, "dlv")) == 0
    with pytest.raises(SystemExit):
        main(["deliver", "--state", str(tmp_path / "dlv"), *CFG])
