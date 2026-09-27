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
from vedit.schema import EditPlan, FixOp, FixPatch, Transcript, Visual
from vedit.stage.plan import _words_inline


class FixError(RuntimeError):
    pass


SUPPORTED = (
    'supported fixes: place an input image ("place image 1 at 0:20", or just '
    '"add it to this video") · remove the visual · recaption a span '
    '("recaption 0:45: ...") · retime a visual ("move the visual at 0:20 to '
    "0:30\"). Silence cutting is automatic — a fix can't re-cut; report "
    "surviving pauses and the cut settings get tuned instead."
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


def _build_prompt(
    instruction: str,
    plan: EditPlan,
    transcript: Transcript,
    screenshots: list[str],
) -> str:
    return f"""You map ONE correction instruction to a single structured patch op.

OPS (pick exactly one):
- replace_icon: target visual changed; set visual_id + keyword (the new concept, e.g. "shield")
- remove_visual: drop a visual; set visual_id
- recaption: replace caption text; set from_word (start of the target caption) + text
- retime_visual: move a visual in time; set visual_id + from_word + to_word (source word indices)
- add_visual: place an input image; set file (exactly one of AVAILABLE SCREENSHOTS) + from_word + to_word (source word indices, inside one segment). If the instruction says to add the image without naming one, omit file and the first available screenshot is used. If it names no time or words, omit from_word/to_word and the image goes near the start (it can be retimed after).
- unknown: the instruction is not about visuals or captions at all

TARGETING: when the instruction names a time or describes a position, pick the
CLOSEST visual / caption to it — exact matches are not required. Only answer
unknown when there is genuinely nothing the instruction could refer to. For
add_visual, map a named time or quoted phrase to the closest word indices.

TIME MAP: each word is [index](start-end seconds). Times in the instruction refer to source time.

CURRENT VISUALS: {json.dumps(_visual_rows(plan, transcript), ensure_ascii=False)}
CURRENT CAPTIONS: {json.dumps(_caption_rows(plan, transcript), ensure_ascii=False)}
AVAILABLE SCREENSHOTS: {json.dumps(screenshots, ensure_ascii=False)}
WORDS: {_words_inline(transcript)}

INSTRUCTION: {instruction}

Answer with raw JSON only, matching the provided schema."""


def _mock_patch(instruction: str, plan: EditPlan, screenshots: list[str]) -> FixPatch:
    text = instruction.lower()
    first_icon = next((v for v in plan.visuals if v.kind == "icon"), None)
    if "remove" in text and plan.visuals:
        return FixPatch(op=FixOp.remove_visual, visual_id=plan.visuals[0].id)
    if ("shield" in text or "use a" in text) and first_icon:
        kw = "shield" if "shield" in text else text.split("use a ")[-1].split()[0]
        return FixPatch(op=FixOp.replace_icon, visual_id=first_icon.id, keyword=kw)
    if ("place" in text or "add" in text) and screenshots and plan.captions:
        c = plan.captions[0]
        return FixPatch(
            op=FixOp.add_visual,
            file=screenshots[0],
            from_word=c.from_word,
            to_word=c.to_word,
        )
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
    screenshots: list[str] | None = None,
) -> FixPatch:
    """Map free text to a FixPatch. Raises FixError only when the model fails."""
    shots = screenshots or []
    if mock:
        return _mock_patch(instruction, plan, shots)
    key = missing_key(cfg)
    if key:
        return FixPatch(op=FixOp.unknown, note=f"{key} not set — cannot parse fix")

    from vedit.stage.plan import _strip_fences

    prompt = _build_prompt(instruction, plan, transcript, shots)
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
    patch: FixPatch,
    plan: EditPlan,
    transcript: Transcript,
    icons_enabled: bool = True,
    screenshots: list[str] | None = None,
) -> tuple[EditPlan, list[str]]:
    """Deterministically patch the plan. Raises FixError for anything unsafe."""
    notes: list[str] = []
    n = len(transcript.words)

    if patch.op == FixOp.unknown:
        raise FixError(
            patch.note and f"unsupported fix: {patch.note}. {SUPPORTED}" or SUPPORTED
        )

    if patch.op == FixOp.replace_icon:
        if not icons_enabled:
            raise FixError(
                "icons are disabled in this project — send a screenshot and "
                "say which visual it should replace"
            )
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

    elif patch.op == FixOp.add_visual:
        shots = screenshots or []
        target_file = patch.file
        if not target_file:
            if not shots:
                raise FixError("nothing to place — send images to the bot first")
            target_file = shots[0]
        elif target_file not in shots:
            have = ", ".join(shots) or "none — send images to the bot first"
            raise FixError(f"'{target_file}' is not an input image (have: {have})")
        lo, hi = patch.from_word, patch.to_word
        if lo is None or hi is None:
            if not plan.segments:
                raise FixError("this plan has no segments to place an image in")
            seg0 = plan.segments[0]
            lo = seg0.keep_from_word
            hi = min(lo + 4, seg0.keep_to_word)
            if hi <= lo:
                raise FixError(
                    "the start is too short to place an image — "
                    "name a time instead (e.g. 'at 0:20')"
                )
            notes.append(f"fix: add_visual target defaulted to {lo}..{hi}")
        if not (0 <= lo < hi < n):
            raise FixError(f"add range {lo}..{hi} out of bounds (n={n})")
        if not _same_segment(plan, lo, hi):
            raise FixError("add range crosses a cut boundary")
        taken = {v.id for v in plan.visuals}
        num = 1
        while f"v{num}" in taken:
            num += 1
        plan.visuals.append(
            Visual(
                id=f"v{num}",
                kind="screenshot",
                file=target_file,
                from_word=lo,
                to_word=hi,
            )
        )
        notes.append(f"fix: add_visual v{num} '{target_file}' -> {lo}..{hi}")

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
