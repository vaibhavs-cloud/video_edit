"""Correction loop: free-text instruction -> FixPatch -> deterministic plan patch.

The plan is never re-generated on a fix (implementation.md §9). One small
structured LLM call maps the instruction to the FixOp enum; pure code then
patches edit_plan.json, which is re-validated before anything renders.
"""

from __future__ import annotations

import json
import time

from vedit.config import Config
from vedit.llm import generate_json, make_client, missing_key
from vedit.schema import EditPlan, FixOp, FixPatch, Transcript
from vedit.stage.plan import _words_inline


class FixError(RuntimeError):
    pass


SUPPORTED = (
    'supported fixes: replace the icon ("use a shield") · remove the visual · '
    'recaption a span ("recaption 0:45: ...") · retime a visual '
    '("move the icon at 0:20 to 0:30")'
)


def _visual_rows(plan: EditPlan, transcript: Transcript) -> list[dict]:
    rows = []
    for v in plan.visuals:
        lo = min(v.from_word, len(transcript.words) - 1)
        hi = min(v.to_word, len(transcript.words) - 1)
        rows.append(
            {
                "id": v.id,
                "kind": v.kind,
                "keyword": v.keyword,
                "icon": v.icon,
                "pos": v.pos,
                "from_s": round(transcript.words[lo].s, 2),
                "to_s": round(transcript.words[hi].e, 2),
            }
        )
    return rows


def _caption_rows(plan: EditPlan, transcript: Transcript) -> list[dict]:
    rows = []
    for i, c in enumerate(plan.captions):
        lo = min(c.from_word, len(transcript.words) - 1)
        hi = min(c.to_word, len(transcript.words) - 1)
        text = c.override_text or " ".join(
            w.t for w in transcript.words[c.from_word : c.to_word + 1]
        )
        rows.append(
            {
                "index": i,
                "from_word": c.from_word,
                "to_word": c.to_word,
                "from_s": round(transcript.words[lo].s, 2),
                "to_s": round(transcript.words[hi].e, 2),
                "text": text,
            }
        )
    return rows


def _build_prompt(instruction: str, plan: EditPlan, transcript: Transcript) -> str:
    return f"""You map ONE correction instruction to a single structured patch op.

OPS (pick exactly one):
- replace_icon: target visual changed; set visual_id + keyword (the new concept, e.g. "shield")
- remove_visual: drop a visual; set visual_id
- recaption: replace caption text; set from_word (start of the target caption) + text
- retime_visual: move a visual in time; set visual_id + from_word + to_word (source word indices)
- unknown: instruction is not any of the above

TIME MAP: each word is [index](start-end seconds). Times in the instruction refer to source time.

CURRENT VISUALS: {json.dumps(_visual_rows(plan, transcript), ensure_ascii=False)}
CURRENT CAPTIONS: {json.dumps(_caption_rows(plan, transcript), ensure_ascii=False)}
WORDS: {_words_inline(transcript)}

INSTRUCTION: {instruction}

Answer with raw JSON only, matching the provided schema."""


def _mock_patch(instruction: str, plan: EditPlan) -> FixPatch:
    text = instruction.lower()
    first_icon = next((v for v in plan.visuals if v.kind == "icon"), None)
    if "remove" in text and plan.visuals:
        return FixPatch(op=FixOp.remove_visual, visual_id=plan.visuals[0].id)
    if ("shield" in text or "use a" in text) and first_icon:
        kw = "shield" if "shield" in text else text.split("use a ")[-1].split()[0]
        return FixPatch(op=FixOp.replace_icon, visual_id=first_icon.id, keyword=kw)
    if "recaption" in text and plan.captions:
        c = plan.captions[0]
        tail = text.split("recaption", 1)[1].lstrip(" :-")
        return FixPatch(
            op=FixOp.recaption,
            from_word=c.from_word,
            to_word=c.to_word,
            text=tail or "mock recaption",
        )
    if "retime" in text and first_icon:
        return FixPatch(
            op=FixOp.retime_visual,
            visual_id=first_icon.id,
            from_word=first_icon.from_word,
            to_word=first_icon.to_word,
        )
    return FixPatch(op=FixOp.unknown, note=instruction)


def parse_fix(
    instruction: str,
    plan: EditPlan,
    transcript: Transcript,
    cfg: Config,
    mock: bool = False,
) -> FixPatch:
    """Map free text to a FixPatch. Raises FixError only when the model fails."""
    if mock:
        return _mock_patch(instruction, plan)
    key = missing_key(cfg)
    if key:
        return FixPatch(op=FixOp.unknown, note=f"{key} not set — cannot parse fix")

    from vedit.stage.plan import _strip_fences

    prompt = _build_prompt(instruction, plan, transcript)
    client = make_client(cfg)
    last: Exception | None = None
    for attempt in range(1, cfg.retry.llm_attempts + 1):
        try:
            raw = generate_json(client, cfg, cfg.models.visuals, prompt, FixPatch)
            return FixPatch.model_validate_json(_strip_fences(raw))
        except Exception as exc:  # noqa: BLE001 — bounded retries then FixError
            last = exc
            time.sleep(cfg.retry.llm_backoff_s * attempt)
    raise FixError(f"could not parse the correction: {last}")


def _find_visual(plan: EditPlan, patch: FixPatch):
    for v in plan.visuals:
        if v.id == patch.visual_id:
            return v
    raise FixError(
        f"no visual '{patch.visual_id}' in this plan "
        f"(have: {', '.join(v.id for v in plan.visuals) or 'none'})"
    )


def _same_segment(plan: EditPlan, lo: int, hi: int) -> bool:
    try:
        first = plan.segment_of_word(lo)
    except KeyError:
        return False
    for wi in range(lo, hi + 1):
        try:
            if plan.segment_of_word(wi) != first:
                return False
        except KeyError:
            return False
    return True


def apply_patch(
    patch: FixPatch, plan: EditPlan, transcript: Transcript
) -> tuple[EditPlan, list[str]]:
    """Deterministically patch the plan. Raises FixError for anything unsafe."""
    notes: list[str] = []
    n = len(transcript.words)

    if patch.op == FixOp.unknown:
        raise FixError(
            patch.note and f"unsupported fix: {patch.note}. {SUPPORTED}" or SUPPORTED
        )

    if patch.op == FixOp.replace_icon:
        v = _find_visual(plan, patch)
        if patch.keyword:
            v.keyword = patch.keyword
        if v.kind != "icon":
            raise FixError(f"{v.id} is a screenshot, not an icon")
        v.icon = None  # re-picked by the visuals stage for this id only
        notes.append(f"fix: replace_icon {v.id} -> '{v.keyword}'")

    elif patch.op == FixOp.remove_visual:
        v = _find_visual(plan, patch)
        plan.visuals.remove(v)
        notes.append(f"fix: remove_visual {v.id}")

    elif patch.op == FixOp.retime_visual:
        v = _find_visual(plan, patch)
        lo, hi = patch.from_word, patch.to_word
        if lo is None or hi is None:
            raise FixError("retime needs from_word and to_word")
        if not (0 <= lo < hi < n):
            raise FixError(f"retime range {lo}..{hi} out of bounds (n={n})")
        if not _same_segment(plan, lo, hi):
            raise FixError("retime range crosses a cut boundary")
        v.from_word, v.to_word = lo, hi
        notes.append(f"fix: retime_visual {v.id} -> {lo}..{hi}")

    elif patch.op == FixOp.recaption:
        if not patch.text:
            raise FixError("recaption needs replacement text")
        if patch.from_word is None:
            raise FixError("recaption needs from_word (which caption)")
        target = next(
            (c for c in plan.captions if c.from_word <= patch.from_word <= c.to_word),
            None,
        )
        if target is None:
            raise FixError(f"no caption covers word {patch.from_word}")
        target.override_text = patch.text
        notes.append(
            f"fix: recaption {target.from_word}..{target.to_word} -> '{patch.text}'"
        )

    validated = EditPlan.model_validate(plan.model_dump())
    return validated, notes
