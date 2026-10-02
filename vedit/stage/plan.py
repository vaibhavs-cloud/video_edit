"""LLM call 2: word-indexed transcript -> PlanDraft (visuals/captions/zooms).

The model never emits cut times. Its output is JSON-Schema constrained,
validated, repaired once on failure, and finally degraded to captions-only —
this stage never crashes the pipeline.
"""

from __future__ import annotations

import json
import re
import time
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from vedit.config import Config
from vedit.llm import generate_json, make_client, missing_key
from vedit.schema import (
    CaptionSpan,
    DraftCaption,
    DraftVisual,
    EditPlan,
    PlanDraft,
    Reframe,
    Segment,
    Transcript,
    Visual,
)
from vedit.stage.anchor import AnchorError, parse_time_tokens, resolve_span

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
    if cfg.visuals.icons_enabled:
        visual_rules = (
            f"- max {max_visuals} visuals total, never two starting within "
            f"{cfg.visuals.density_window_s:.0f}s of each other\n"
            '    - kind "icon": a concrete noun/tech concept (API, token, database, lock...). keyword = the search term.\n'
            '    - kind "screenshot": ONLY if a listed filename directly illustrates what is being said; set file to that filename.'
        )
    else:
        visual_rules = (
            '- input images ONLY: kind must always be "screenshot" with file '
            'set to one of the listed filenames (never kind "icon").\n'
            "    - place a screenshot ONLY where it directly illustrates what is being said\n"
            "    - if no listed filename fits (or the list is empty), emit NO visuals at all"
        )
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
{visual_rules}
    - range [from_word, to_word] inclusive, must lie inside ONE kept segment
    - pos: where it sits; zoom: true only for a strong emphasis moment
    - Owner-specified placements in the instruction (image name + time) win over everything: use exactly that file, mapped to the closest kept words at that time (times are source seconds; never straddle a cut gap — pick the nearest kept segment). Deterministic code enforces owner placements after your draft; still emit your best mapping.
2. captions — split the talk into readable spans of 1-6 words each.
   - every word of the transcript should be covered by exactly one span
   - each span must lie inside ONE kept segment (never cross a segment boundary)
   - emphasis: indices of the 1-2 most important words of that span
3. zoom_at_words — at most {max_zoom} word indices where a subtle zoom emphasis begins.

Answer with raw JSON only, matching the provided schema."""


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
            DraftCaption(
                from_word=s.keep_from_word, to_word=s.keep_to_word, emphasis=[]
            )
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
    key = missing_key(cfg)
    if key:
        return (
            _fallback_draft(transcript, segments),
            True,
            f"{key} not set -> captions-only",
        )

    client = make_client(cfg)
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
            raw = generate_json(client, cfg, cfg.models.plan, effective, PlanDraft)
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


_OWNER_FILE = re.compile(r"[\w\-.]+?\.(?:png|jpg|jpeg|webp)", re.IGNORECASE)


def _owner_placements(
    draft: PlanDraft,
    user_prompt: str,
    transcript: Transcript,
    segments: list[Segment],
    cfg: Config,
    source_dur: float,
) -> list[str]:
    """Deterministically enforce owner-specified image+time placements.

    Repositions the draft screenshot visual whose file is named in the
    instruction (nth file pairs with nth timestamp, else the first), and
    appends an owner placement the model missed entirely. Mutates
    draft.visuals in place; the normal validation loop below still applies.
    No timestamps in the instruction means no enforcement.
    """
    notes: list[str] = []
    prompt = user_prompt or ""
    dur = source_dur if source_dur > 0 else float("inf")
    times = parse_time_tokens(prompt, source_dur=dur)
    if not times:
        return notes
    span_s = cfg.visuals.placement_span_s
    candidates = list(dict.fromkeys(m.group(0) for m in _OWNER_FILE.finditer(prompt)))
    for idx, fname in enumerate(candidates):
        t = times[min(idx, len(times) - 1)]
        try:
            lo, hi = resolve_span(t, transcript.words, segments, span_s)
        except AnchorError:
            notes.append(f"owner placement '{fname}' @ input {t:.1f}s not anchorable")
            continue
        target = next(
            (
                dv
                for dv in draft.visuals
                if dv.kind == "screenshot" and dv.file == fname
            ),
            None,
        )
        if target is None:
            draft.visuals.append(
                DraftVisual(kind="screenshot", file=fname, from_word=lo, to_word=hi)
            )
            notes.append(
                f"owner placement: '{fname}' @ input {t:.1f}s "
                f"-> words {lo}..{hi} (added)"
            )
            continue
        target.from_word, target.to_word = lo, hi
        notes.append(f"owner placement: '{fname}' @ input {t:.1f}s -> words {lo}..{hi}")
    return notes


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
    notes.extend(
        _owner_placements(
            draft,
            user_prompt,
            transcript,
            segments,
            cfg,
            float(source_meta.get("dur", 0) or 0),
        )
    )
    n_words = len(transcript.words)
    visuals: list[Visual] = []
    counter = 0
    for dv in draft.visuals:
        lo, hi = dv.from_word, dv.to_word
        if dv.kind == "icon" and not cfg.visuals.icons_enabled:
            notes.append(f"dropped visual @{lo}: icons disabled, screenshots only")
            continue
        if lo >= n_words:
            notes.append(f"dropped visual @{lo}: out of range")
            continue
        if hi <= lo:
            hi = lo + 1  # model asked for a single word: widen to survive schema
        hi = min(hi, n_words - 1)
        if hi <= lo:
            notes.append(f"dropped visual @{lo}: empty range at transcript end")
            continue
        segs = {_segment_index(segments, wi) for wi in range(lo, hi + 1)}
        if None in segs or len(segs) != 1:
            notes.append(f"dropped visual @{lo}: crosses a cut or is out of range")
            continue
        try:
            counter += 1
            visuals.append(
                Visual(
                    id=f"v{counter}",
                    kind=dv.kind,
                    keyword=dv.keyword,
                    file=dv.file,
                    from_word=lo,
                    to_word=hi,
                    pos=dv.pos,
                    zoom=dv.zoom,
                )
            )
        except ValidationError as exc:  # never let a draft quirk crash assembly
            counter -= 1
            notes.append(f"dropped visual @{lo}: {exc.errors()[0]['msg']}")

    captions: list[CaptionSpan] = []
    for dc in draft.captions:
        lo, hi = dc.from_word, min(dc.to_word, n_words - 1)
        if lo >= n_words or hi < lo:
            continue
        if _segment_index(segments, lo) == _segment_index(segments, hi):
            try:
                captions.append(
                    CaptionSpan(
                        from_word=lo,
                        to_word=hi,
                        emphasis=[e for e in dc.emphasis if lo <= e <= hi],
                    )
                )
            except ValidationError:
                captions.append(CaptionSpan(from_word=lo, to_word=hi))
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
