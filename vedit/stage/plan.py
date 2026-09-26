"""Gemini call 2: word-indexed transcript -> PlanDraft (visuals/captions/zooms).

The model never emits cut times. Its output is JSON-Schema constrained,
validated, repaired once on failure, and finally degraded to captions-only —
this stage never crashes the pipeline.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from vedit.config import Config
from vedit.schema import (
    CaptionSpan,
    EditPlan,
    PlanDraft,
    Reframe,
    Segment,
    Transcript,
    Visual,
)

T = TypeVar("T", bound=BaseModel)


class PlanError(RuntimeError):
    pass


def _words_inline(t: Transcript) -> str:
    return " ".join(f"[{w.i}]({w.s:.2f}-{w.e:.2f}) {w.t}" for w in t.words)


def _build_prompt(
    transcript: Transcript,
    segments: list[Segment],
    screenshots: list[str],
    cfg: Config,
    user_prompt: str,
    source_dur: float,
) -> str:
    seg_lines = ", ".join(f"{s.keep_from_word}..{s.keep_to_word}" for s in segments)
    kept = sum(s.end - s.start for s in segments)
    max_zoom = max(1, len(segments) // cfg.video.max_zoom_segments_div)
    max_visuals = cfg.visuals.max_visuals
    return f"""You are the editing planner for a vertical educational Reel (1080x1920).

INPUT
- Original duration: {source_dur:.1f}s, duration after silence cuts: {kept:.1f}s
- Kept segments (inclusive word ranges): {seg_lines}
- Screenshots you may place (only these filenames): {screenshots if screenshots else "none"}
- Owner instruction: {user_prompt or "none"}

TRANSCRIPT (each word prefixed with its index)
{_words_inline(transcript)}

DECIDE
1. visuals — concepts that deserve an on-screen element. Rules:
   - max {max_visuals} visuals total, never two starting within {cfg.visuals.density_window_s:.0f}s of each other
   - kind "icon": a concrete noun/tech concept (API, token, database, lock...). keyword = the search term.
   - kind "screenshot": ONLY if a listed filename directly illustrates what is being said; set file to that filename.
   - range [from_word, to_word] inclusive, must lie inside ONE kept segment
   - pos: where it sits; zoom: true only for a strong emphasis moment
2. captions — split the talk into readable spans of 1-6 words each.
   - every word of the transcript should be covered by exactly one span
   - each span must lie inside ONE kept segment (never cross a segment boundary)
   - emphasis: indices of the 1-2 most important words of that span
3. zoom_at_words — at most {max_zoom} word indices where a subtle zoom emphasis begins.

Answer with raw JSON only, matching the provided schema."""


def generate_json(client: Any, model: str, prompt: str, schema: type[T]) -> str:
    from google.genai import types

    schema_json = schema.model_json_schema()
    attempt_configs: list[Any]
    try:
        attempt_configs = [
            {
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema_json,
                }
            },
            types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema_json
            ),
        ]
    except Exception:  # noqa: BLE001 — pragma: no cover — SDK shape differences
        attempt_configs = [
            types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema_json
            )
        ]

    last: Exception | None = None
    for cfg_item in attempt_configs:
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt, config=cfg_item
            )
            text = resp.text
            if text:
                return text
        except (TypeError, ValueError) as exc:
            last = exc
            continue
    raise PlanError(f"could not call model with structured output: {last}")


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _parse(text: str, schema: type[T]) -> T:
    return schema.model_validate_json(_strip_fences(text))


def _fallback_draft(transcript: Transcript, segments: list[Segment]) -> PlanDraft:
    """Degraded mode: captions covering every segment, no visuals, no zooms."""
    return PlanDraft(
        visuals=[],
        captions=[
            CaptionSpan(from_word=s.keep_from_word, to_word=s.keep_to_word, emphasis=[])
            for s in segments
        ],
        zoom_at_words=[],
    )


def draft_plan(
    transcript: Transcript,
    segments: list[Segment],
    screenshots: list[str],
    cfg: Config,
    user_prompt: str,
    source_dur: float,
) -> tuple[PlanDraft, bool, str]:
    """Returns (draft, degraded, note). Never raises for model/validation issues."""
    api_key = __import__("os").environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return (
            _fallback_draft(transcript, segments),
            True,
            "GEMINI_API_KEY not set -> captions-only",
        )

    from google import genai

    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(
        transcript, segments, screenshots, cfg, user_prompt, source_dur
    )
    errors: list[str] = []
    raw = ""

    for attempt in range(1, cfg.retry.llm_attempts + 1):
        try:
            effective = prompt
            if errors:
                effective += "\n\nYour previous answer was invalid:\n" + "\n".join(
                    errors[:8]
                )
                effective += "\nReturn corrected raw JSON only."
            raw = generate_json(client, cfg.models.plan, effective, PlanDraft)
            draft = _parse(raw, PlanDraft)
            return draft, False, ""
        except (ValidationError, json.JSONDecodeError) as exc:
            errors = (
                [f"- {e['loc']}: {e['msg']}" for e in exc.errors()][:8]
                if isinstance(exc, ValidationError)
                else [f"- {exc}"]
            )
            time.sleep(cfg.retry.llm_backoff_s * attempt)
        except Exception as exc:  # noqa: BLE001
            errors = [f"- {exc}"]
            time.sleep(cfg.retry.llm_backoff_s * attempt)

    return (
        _fallback_draft(transcript, segments),
        True,
        f"plan failed after retries ({errors[:1]})",
    )


def _segment_index(segments: list[Segment], wi: int) -> int | None:
    for idx, seg in enumerate(segments):
        if seg.keep_from_word <= wi <= seg.keep_to_word:
            return idx
    return None


def _clip_to_segment(a: int, b: int, seg: Segment) -> tuple[int, int] | None:
    lo, hi = max(a, seg.keep_from_word), min(b, seg.keep_to_word)
    return (lo, hi) if lo <= hi else None


def assemble_plan(
    draft: PlanDraft,
    transcript: Transcript,
    segments: list[Segment],
    cfg: Config,
    user_prompt: str,
    source_meta: dict,
    degraded: bool,
    note: str,
) -> tuple[EditPlan, list[str]]:
    """Deterministically sanitises draft output into a validated EditPlan.

    Returns (plan, notes) where notes capture every repair/drop for the report.
    """
    notes: list[str] = []
    if note:
        notes.append(note)
    visuals: list[Visual] = []
    counter = 0
    for dv in draft.visuals:
        segs = {
            _segment_index(segments, wi)
            for wi in range(
                dv.from_word, min(dv.to_word, len(transcript.words) - 1) + 1
            )
        }
        if None in segs or len(segs) != 1 or dv.from_word >= len(transcript.words):
            notes.append(
                f"dropped visual @{dv.from_word}: crosses a cut or is out of range"
            )
            continue
        counter += 1
        visuals.append(
            Visual(
                id=f"v{counter}",
                kind=dv.kind,
                keyword=dv.keyword,
                file=dv.file,
                from_word=dv.from_word,
                to_word=dv.to_word,
                pos=dv.pos,
                zoom=dv.zoom,
            )
        )

    captions: list[CaptionSpan] = []
    for dc in draft.captions:
        if dc.from_word >= len(transcript.words):
            continue
        if _segment_index(segments, dc.from_word) == _segment_index(
            segments, dc.to_word
        ):
            captions.append(
                CaptionSpan(
                    from_word=dc.from_word, to_word=dc.to_word, emphasis=dc.emphasis
                )
            )
            continue
        # split a caption that straddles a cut into per-segment spans
        for seg in segments:
            clipped = _clip_to_segment(dc.from_word, dc.to_word, seg)
            if clipped:
                lo, hi = clipped
                captions.append(
                    CaptionSpan(
                        from_word=lo,
                        to_word=hi,
                        emphasis=[e for e in dc.emphasis if lo <= e <= hi],
                    )
                )

    if not captions:
        captions = [
            CaptionSpan(from_word=s.keep_from_word, to_word=s.keep_to_word, emphasis=[])
            for s in segments
        ]
        degraded = True
        notes.append("no captions from model -> full-coverage fallback")

    zooms = [
        z
        for z in draft.zoom_at_words
        if 0 <= z < len(transcript.words) and _segment_index(segments, z) is not None
    ]
    max_zoom = max(1, len(segments) // cfg.video.max_zoom_segments_div)
    zooms = zooms[:max_zoom]

    aspect = source_meta["w"] / source_meta["h"]
    plan = EditPlan(
        source={
            "ref": source_meta["ref"],
            "sha256": source_meta["sha256"],
            "w": source_meta["w"],
            "h": source_meta["h"],
            "dur": source_meta["dur"],
        },
        reframe=Reframe(
            mode="scale" if abs(aspect - 0.5625) < 0.02 else "crop",
            aspect=0.5625,
            x_frac=cfg.video.x_frac,
        ),
        words=transcript.words,
        segments=segments,
        visuals=visuals,
        captions=captions,
        zoom_at_words=zooms,
        prompt=user_prompt,
        degraded=degraded,
    )
    return plan, notes
