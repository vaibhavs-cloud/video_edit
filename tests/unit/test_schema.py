from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from vedit.schema import EditPlan, Transcript, Word


def test_word_end_before_start_rejected():
    with pytest.raises(ValidationError):
        Word(i=0, t="x", s=1.0, e=0.5)


def test_transcript_indices_must_be_contiguous():
    with pytest.raises(ValidationError):
        Transcript(
            words=[Word(i=0, t="a", s=0.0, e=0.1), Word(i=2, t="b", s=0.2, e=0.3)]
        )


def test_plan_with_no_segments_rejected(plan):
    built, _ = plan
    data = json.loads(built.model_dump_json())
    data["segments"] = []
    with pytest.raises(ValidationError):
        EditPlan.model_validate(data)


def test_caption_crossing_a_cut_rejected(plan):
    built, _ = plan
    data = json.loads(built.model_dump_json())
    data["captions"] = [{"from_word": 0, "to_word": 79, "emphasis": []}]
    with pytest.raises(ValidationError) as exc:
        EditPlan.model_validate(data)
    assert "crosses a cut" in str(exc.value)


def test_visual_crossing_a_cut_rejected(plan):
    built, _ = plan
    data = json.loads(built.model_dump_json())
    data["visuals"] = [
        {
            "id": "vx",
            "kind": "icon",
            "keyword": "api",
            "from_word": 10,
            "to_word": 30,
            "pos": "top",
        }
    ]
    with pytest.raises(ValidationError):
        EditPlan.model_validate(data)


def test_duplicate_visual_ids_rejected(plan):
    built, _ = plan
    data = json.loads(built.model_dump_json())
    data["visuals"][1]["id"] = data["visuals"][0]["id"]
    with pytest.raises(ValidationError):
        EditPlan.model_validate(data)


def test_emphasis_outside_span_rejected(plan):
    built, _ = plan
    data = json.loads(built.model_dump_json())
    data["captions"][0]["emphasis"] = [data["captions"][1]["to_word"] + 1]
    with pytest.raises(ValidationError):
        EditPlan.model_validate(data)


def test_valid_roundtrip(plan):
    built, _ = plan
    again = EditPlan.model_validate_json(built.model_dump_json())
    assert again.segments == built.segments
    assert again.segment_of_word(0) == 0
