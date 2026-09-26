from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from vedit.config import load_config
from vedit.schema import PlanDraft, Transcript
from vedit.stage import cuts
from vedit.stage import plan as plan_stage

FIXTURES = ROOT / "vedit" / "fixtures"


@pytest.fixture
def cfg():
    return load_config(ROOT / "config.yaml")


@pytest.fixture
def transcript() -> Transcript:
    return Transcript.model_validate_json(
        (FIXTURES / "transcript.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def draft() -> PlanDraft:
    return PlanDraft.model_validate_json(
        (FIXTURES / "draft.json").read_text(encoding="utf-8")
    )


@pytest.fixture
def source_dur(transcript) -> float:
    return transcript.words[-1].e + 1.0


@pytest.fixture
def segments(cfg, transcript, source_dur):
    return cuts.compute_segments(transcript.words, source_dur, cfg.cuts)


@pytest.fixture
def plan(cfg, transcript, draft, segments, source_dur):
    built, notes = plan_stage.assemble_plan(
        draft,
        transcript,
        segments,
        cfg,
        "unit test",
        {"ref": "test", "sha256": "0" * 64, "w": 1280, "h": 720, "dur": source_dur},
        False,
        "",
    )
    return built, notes
