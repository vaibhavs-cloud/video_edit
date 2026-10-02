"""Correction loop: free-text instruction -> FixPatch -> deterministic plan patch.

The plan is never re-generated on a fix (implementation.md §9). One small
structured LLM call maps the instruction to the FixOp enum; pure code then
patches edit_plan.json, which is re-validated before anything renders.

`preview_fix` + `--dry-run` run the same parse/resolve/apply on a copy and
report what WOULD happen (both input-file and final-video times) without
touching state; `fix --apply-preview` applies a saved preview without
re-calling the LLM. The Telegram worker uses this as the confirm-before-apply
step — see confirmation-contract.md.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from pydantic import ValidationError

from vedit import ff
from vedit.config import Config
from vedit.llm import generate_json, make_client, missing_key
from vedit.schema import (
    EditPlan,
    FixOp,
    FixPatch,
    Segment,
    Transcript,
    Visual,
)
from vedit.stage.anchor import (
    AnchorError,
    parse_scale,
    parse_time_tokens,
    resolve_point,
    resolve_span,
)
from vedit.stage.plan import _words_inline
from vedit.stage.timeline import TimelineMap


class FixError(RuntimeError):
    pass


# Safety floor: a cut/keep that removes less kept speech than this is almost
# certainly aimed at already-cut silence (e.g. "cut 0:50 to 0:53" landing in
# a gap). Refused with an explanatory FixError instead of a wasted render.
CUT_MIN_KEPT_S = 0.2


SUPPORTED = (
    'supported fixes: place an input image ("place image 1 at 0:20" — a timestamp '
    "is matched exactly to the input video transcript — or just "
    '"add it to this video") · remove the visual · recaption a span '
    '("recaption 0:45: ...") · retime a visual ("move the visual at 0:20 to '
    '0:30") · cut a span ("cut 0:30 to 0:45") · keep only a span '
    '("keep only 0:10 to 1:00") · add/remove a zoom ("zoom in at 0:30") · '
    'resize an overlay ("make the image smaller", "shrink it to 60%"). '
    "Silence cutting is automatic — report surviving pauses and the cut "
    "settings get tuned instead."
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
- cut_range: REMOVE a span from the video; set from_word + to_word (source word indices, normalized if reversed). E.g. "cut 0:30 to 0:45", "remove the intro", "delete where I say um". This is the ONLY op that removes footage — remove_visual only drops an overlay.
- keep_range: keep ONLY a span and cut everything else; set from_word + to_word. E.g. "keep only 0:10 to 1:00".
- add_zoom: emphasis zoom at a moment; set from_word (a named time or phrase maps here). E.g. "zoom in at 0:30".
- remove_zoom: drop zoom(s) near a time; set from_word + to_word. E.g. "remove the zoom at 0:30".
- resize_visual: shrink/grow an overlay to a fraction of the frame; set visual_id (or a time naming it) + the size, which is matched exactly by code from explicit percents/words ("60%", "half", "smaller"). E.g. "make the image at 0:58 smaller", "shrink the diagram to 60%", "zoom out the image".
- add_visual: place an input image; set file (exactly one of AVAILABLE SCREENSHOTS) + from_word + to_word (source word indices, inside one segment; an explicit time in the instruction is matched exactly to the transcript by code). If the instruction says to add the image without naming one, omit file and the first available screenshot is used. If it names no time or words, omit from_word/to_word and the image goes near the start (it can be retimed after).
- unknown: the instruction is not about visuals or captions at all

TARGETING: when the instruction names a time or describes a position, pick the
CLOSEST visual / caption to it — exact matches are not required. Only answer
unknown when there is genuinely nothing the instruction could refer to. For
add_visual, map a named time or quoted phrase to the closest word indices.

TIME MAP: each word is [index](start-end seconds). Times in the instruction refer to source time. Explicit timestamps (0:30, 30s) are resolved exactly by code — still fill from_word/to_word with your best guess.

CURRENT VISUALS: {json.dumps(_visual_rows(plan, transcript), ensure_ascii=False)}
CURRENT CAPTIONS: {json.dumps(_caption_rows(plan, transcript), ensure_ascii=False)}
AVAILABLE SCREENSHOTS: {json.dumps(screenshots, ensure_ascii=False)}
WORDS: {_words_inline(transcript)}

INSTRUCTION: {instruction}

Answer with raw JSON only, matching the provided schema."""


def _mock_patch(instruction: str, plan: EditPlan, screenshots: list[str]) -> FixPatch:
    text = instruction.lower()
    first_icon = next((v for v in plan.visuals if v.kind == "icon"), None)
    if "remove the zoom" in text or "unzoom" in text:
        c = plan.captions[0] if plan.captions else None
        return FixPatch(
            op=FixOp.remove_zoom,
            from_word=c.from_word if c else None,
            to_word=c.to_word if c else None,
        )
    if (
        text.startswith("cut ")
        or "delete " in text
        or "trim " in text
        or "remove the part" in text
    ) and plan.captions:
        c = plan.captions[0]
        return FixPatch(op=FixOp.cut_range, from_word=c.from_word, to_word=c.to_word)
    if (text.startswith("keep ") or "keep only" in text) and plan.segments:
        seg0 = plan.segments[0]
        return FixPatch(
            op=FixOp.keep_range,
            from_word=seg0.keep_from_word,
            to_word=seg0.keep_to_word,
        )
    if "zoom in" in text or "add zoom" in text:
        c = plan.captions[0] if plan.captions else None
        w = c.from_word if c else 0
        return FixPatch(op=FixOp.add_zoom, from_word=w, to_word=w)
    if (
        "resize" in text
        or "zoom out" in text
        or "smaller" in text
        or "bigger" in text
        or "shrink" in text
        or "scale" in text
        or "%" in text
    ) and plan.visuals:
        target = next(
            (v for v in plan.visuals if v.kind == "screenshot"),
            plan.visuals[0],
        )
        return FixPatch(op=FixOp.resize_visual, visual_id=target.id)
    if "remove" in text and plan.visuals:
        return FixPatch(op=FixOp.remove_visual, visual_id=plan.visuals[0].id)
    if ("shield" in text or "use a" in text) and first_icon:
        if "shield" in text:
            kw = "shield"
        else:
            tail = text.split("use a ", 1)[-1].split()
            kw = tail[0] if tail else "icon"
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


def _anchor_note(transcript: Transcript, lo: int, hi: int) -> str:
    """Human-readable placement: input-video time + covered phrase."""
    words = transcript.words
    lo = max(0, min(lo, len(words) - 1))
    hi = max(lo, min(hi, len(words) - 1))
    phrase = " ".join(w.t for w in words[lo : hi + 1])
    if len(phrase) > 60:
        phrase = phrase[:57] + "..."
    return f"input {words[lo].s:.1f}s (words {lo}..{hi} '{phrase}')"


def _apply_time_anchor(
    patch: FixPatch,
    instruction: str,
    plan: EditPlan,
    transcript: Transcript,
    span_s: float,
) -> FixPatch:
    """Deterministic override of guessed word mapping from explicit timestamps.

    The model still decides op/file/visual_id; any timestamp in the instruction
    ("at 0:30", "move 0:20 to 0:30") is resolved exactly against the transcript.
    AnchorError (degenerate plan) falls through to apply_patch's validators,
    which raise the proper FixError.
    """
    try:
        tokens = parse_time_tokens(instruction, source_dur=plan.source.dur)
    except AnchorError:
        return patch
    if not tokens or not transcript.words:
        return patch
    words = transcript.words
    try:
        if patch.op == FixOp.add_visual:
            lo, hi = resolve_span(tokens[0], words, plan.segments, span_s)
            if len(tokens) >= 2:
                _, hi2 = resolve_span(tokens[1], words, plan.segments, span_s)
                if hi2 > lo and _same_segment(plan, lo, hi2):
                    hi = hi2
            patch.from_word, patch.to_word = lo, hi
        elif patch.op == FixOp.retime_visual:
            lo, hi = resolve_span(tokens[-1], words, plan.segments, span_s)
            patch.from_word, patch.to_word = lo, hi
        elif patch.op == FixOp.recaption:
            lo, _ = resolve_span(tokens[0], words, plan.segments, span_s)
            patch.from_word = lo
        elif patch.op == FixOp.cut_range or patch.op == FixOp.keep_range:
            lo = resolve_point(tokens[0], words, plan.segments)
            hi = resolve_point(tokens[-1], words, plan.segments)
            if hi < lo:
                lo, hi = hi, lo
            patch.from_word, patch.to_word = lo, hi
        elif patch.op == FixOp.add_zoom:
            pt = resolve_point(tokens[-1], words, plan.segments)
            patch.from_word, patch.to_word = pt, pt
        elif patch.op == FixOp.remove_zoom:
            lo, hi = resolve_span(tokens[-1], words, plan.segments, span_s)
            patch.from_word, patch.to_word = lo, hi
        elif patch.op == FixOp.resize_visual:
            v = _find_resize_target(plan, patch)
            scale = parse_scale(instruction, current=v.scale if v else 1.0)
            if scale is not None:
                patch.scale = scale
            if tokens and patch.from_word is None:
                lo, hi = resolve_span(tokens[-1], words, plan.segments, span_s)
                patch.from_word, patch.to_word = lo, hi
    except AnchorError:
        pass
    return patch


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
    span_s = cfg.visuals.placement_span_s
    if mock:
        patch = _mock_patch(instruction, plan, shots)
        return _apply_time_anchor(patch, instruction, plan, transcript, span_s)
    key = missing_key(cfg)
    if key:
        return FixPatch(op=FixOp.unknown, note=f"{key} not set — cannot parse fix")

    from vedit.stage.plan import _strip_fences

    prompt = _build_prompt(instruction, plan, transcript, shots)
    client = make_client(cfg)
    last: Exception | None = None
    patch: FixPatch | None = None
    for attempt in range(1, cfg.retry.llm_attempts + 1):
        try:
            raw = generate_json(client, cfg, cfg.models.visuals, prompt, FixPatch)
            patch = FixPatch.model_validate_json(_strip_fences(raw))
            break
        except Exception as exc:  # noqa: BLE001 — bounded retries then FixError
            last = exc
            time.sleep(cfg.retry.llm_backoff_s * attempt)
    if patch is None:
        raise FixError(f"could not parse the correction: {last}")
    return _apply_time_anchor(
        patch, instruction, plan, transcript, cfg.visuals.placement_span_s
    )


def _find_resize_target(plan: EditPlan, patch: FixPatch):
    """Visual for resize_visual: explicit id wins, else the visual covering
    the named word (narrowest span wins ties). Returns None when unresolvable
    so the anchor step can still fall back to a default scale base."""
    if patch.visual_id:
        try:
            return _find_visual(plan, patch)
        except FixError:
            pass
    if patch.from_word is not None:
        cands = [v for v in plan.visuals if v.from_word <= patch.from_word <= v.to_word]
        if cands:
            return min(cands, key=lambda v: (v.to_word - v.from_word, v.from_word))
    return None


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


def _removed_kept_s(plan: EditPlan, lo: int, hi: int) -> float:
    """Kept seconds the word range [lo, hi] covers (gaps contribute nothing)."""
    start, end = plan.words[lo].s, plan.words[hi].e
    total = 0.0
    for seg in plan.segments:
        total += max(0.0, min(end, seg.end) - max(start, seg.start))
    return total


def _cut_span(plan: EditPlan, lo: int, hi: int) -> tuple[list[Segment], list[str]]:
    """Remove word range [lo, hi] from kept segments (word-bound edges).

    Overlapped segments split; new edges land exactly on neighboring word
    bounds. Raises FixError if nothing would survive.
    """
    notes: list[str] = []
    out: list[Segment] = []
    for seg in plan.segments:
        if hi < seg.keep_from_word or lo > seg.keep_to_word:
            out.append(seg)
            continue
        cut_lo = max(lo, seg.keep_from_word)
        cut_hi = min(hi, seg.keep_to_word)
        left_to = cut_lo - 1
        if left_to >= seg.keep_from_word:
            out.append(
                Segment(
                    keep_from_word=seg.keep_from_word,
                    keep_to_word=left_to,
                    start=seg.start,
                    end=plan.words[left_to].e,
                )
            )
        right_from = cut_hi + 1
        if right_from <= seg.keep_to_word:
            out.append(
                Segment(
                    keep_from_word=right_from,
                    keep_to_word=seg.keep_to_word,
                    start=plan.words[right_from].s,
                    end=seg.end,
                )
            )
        notes.append(
            f"cut words {cut_lo}..{cut_hi} "
            f"from segment {seg.keep_from_word}..{seg.keep_to_word}"
        )
    if not out:
        raise FixError("that cut would remove the whole video — refusing")
    return out, notes


def _clip_to_kept(
    plan: EditPlan, a: int, b: int, min_words: int
) -> tuple[int, int] | None:
    """Longest sub-range of [a, b] inside one kept segment (None if too short)."""
    best: tuple[int, int] | None = None
    for seg in plan.segments:
        c, d = max(a, seg.keep_from_word), min(b, seg.keep_to_word)
        if d - c + 1 >= min_words and (best is None or d - c > best[1] - best[0]):
            best = (c, d)
    return best


def _repair_cut(plan: EditPlan, lo: int, hi: int, notes: list[str]) -> None:
    """Drop/clip visuals, captions and zooms overlapping a removed word range."""
    for v in list(plan.visuals):
        if v.to_word < lo or v.from_word > hi:
            continue
        kept = _clip_to_kept(plan, v.from_word, v.to_word, min_words=2)
        if kept is None:
            plan.visuals.remove(v)
            notes.append(f"cut dropped visual {v.id} (no kept span survived)")
        else:
            v.from_word, v.to_word = kept
            notes.append(f"cut clipped visual {v.id} -> {kept[0]}..{kept[1]}")
    for c in list(plan.captions):
        if c.to_word < lo or c.from_word > hi:
            continue
        kept = _clip_to_kept(plan, c.from_word, c.to_word, min_words=1)
        if kept is None:
            plan.captions.remove(c)
            notes.append(f"cut dropped a caption over words {c.from_word}..{c.to_word}")
        else:
            if (kept[0], kept[1]) != (c.from_word, c.to_word):
                notes.append(f"cut clipped a caption -> {kept[0]}..{kept[1]}")
                if c.override_text:
                    notes.append("recaption text cleared by the clip — re-apply it")
                    c.override_text = None
            c.from_word, c.to_word = kept
            c.emphasis = [e for e in c.emphasis if kept[0] <= e <= kept[1]]
    dropped = [z for z in plan.zoom_at_words if lo <= z <= hi]
    if dropped:
        plan.zoom_at_words = [z for z in plan.zoom_at_words if not lo <= z <= hi]
        notes.append(f"cut dropped {len(dropped)} zoom(s)")


def apply_patch(
    patch: FixPatch,
    plan: EditPlan,
    transcript: Transcript,
    icons_enabled: bool = True,
    screenshots: list[str] | None = None,
    max_zooms: int | None = None,
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
        notes.append(
            f"fix: retime_visual {v.id} -> {lo}..{hi} "
            f"({_anchor_note(transcript, lo, hi)})"
        )

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
        notes.append(
            f"fix: add_visual v{num} '{target_file}' -> {lo}..{hi} "
            f"({_anchor_note(transcript, lo, hi)})"
        )

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

    elif patch.op == FixOp.cut_range:
        lo, hi = patch.from_word, patch.to_word
        if lo is None or hi is None:
            raise FixError("cut needs a time range (e.g. 'cut 0:30 to 0:45')")
        if hi < lo:
            lo, hi = hi, lo
        if not (0 <= lo < n and 0 <= hi < n):
            raise FixError(f"cut range {lo}..{hi} out of bounds (n={n})")
        removed = _removed_kept_s(plan, lo, hi)
        if removed < CUT_MIN_KEPT_S:
            raise FixError(
                f"that span holds only {removed:.1f}s of kept speech — "
                "it is already silence. Name a speaking part instead "
                "(quote the words to remove)."
            )
        plan.segments, cut_notes = _cut_span(plan, lo, hi)
        notes.extend(cut_notes)
        _repair_cut(plan, lo, hi, notes)
        notes.append(f"cut removed {removed:.1f}s of kept video")

    elif patch.op == FixOp.keep_range:
        lo, hi = patch.from_word, patch.to_word
        if lo is None or hi is None:
            raise FixError("keep needs a time range (e.g. 'keep only 0:10 to 1:00')")
        if hi < lo:
            lo, hi = hi, lo
        if not (0 <= lo < n and 0 <= hi < n):
            raise FixError(f"keep range {lo}..{hi} out of bounds (n={n})")
        if lo > 0:
            plan.segments, cut_notes = _cut_span(plan, 0, lo - 1)
            notes.extend(cut_notes)
            _repair_cut(plan, 0, lo - 1, notes)
        if hi < n - 1:
            plan.segments, cut_notes = _cut_span(plan, hi + 1, n - 1)
            notes.extend(cut_notes)
            _repair_cut(plan, hi + 1, n - 1, notes)
        notes.append(f"fix: keep_range -> words {lo}..{hi}")

    elif patch.op == FixOp.add_zoom:
        w = patch.from_word
        if w is None:
            raise FixError("add_zoom needs a time (e.g. 'zoom in at 0:30')")
        if not 0 <= w < n:
            raise FixError(f"zoom word {w} out of bounds (n={n})")
        try:
            plan.segment_of_word(w)
        except KeyError:
            w = resolve_point(transcript.words[w].s, transcript.words, plan.segments)
            notes.append(f"zoom snapped to kept word {w}")
        if w in plan.zoom_at_words:
            notes.append(f"zoom already present at word {w}")
        else:
            if max_zooms is not None and len(plan.zoom_at_words) >= max_zooms:
                raise FixError(f"zoom cap reached ({max_zooms}) — remove one first")
            plan.zoom_at_words.append(w)
            notes.append(
                f"fix: add_zoom at word {w} ({_anchor_note(transcript, w, w)})"
            )

    elif patch.op == FixOp.remove_zoom:
        lo, hi = patch.from_word, patch.to_word
        if lo is None or hi is None:
            raise FixError("remove_zoom needs a time (e.g. 'remove the zoom at 0:30')")
        inside = [z for z in plan.zoom_at_words if lo <= z <= hi]
        if not inside:
            raise FixError(f"no zoom near words {lo}..{hi}")
        plan.zoom_at_words = [z for z in plan.zoom_at_words if z not in inside]
        notes.append(f"fix: remove_zoom dropped zoom(s) at {inside}")

    elif patch.op == FixOp.resize_visual:
        v = _find_resize_target(plan, patch)
        if v is None:
            raise FixError(
                "say which visual to resize (e.g. 'make the image at 0:58 smaller')"
            )
        if patch.scale is None:
            raise FixError(
                "say how small (e.g. 'make it 60%', 'halve it', 'make it small')"
            )
        old, v.scale = v.scale, patch.scale
        notes.append(f"fix: resize_visual {v.id} scale {old:.2f} -> {v.scale:.2f}")

    try:
        validated = EditPlan.model_validate(plan.model_dump())
    except ValidationError as exc:
        raise FixError(
            f"patch produces an invalid plan: {exc.errors()[0]['msg']}"
        ) from exc
    return validated, notes


def plan_hash(plan: EditPlan) -> str:
    """Short content hash of the plan; apply-preview refuses on mismatch."""
    return hashlib.sha256(plan.model_dump_json().encode("utf-8")).hexdigest()[:16]


def preview_fix(
    instruction: str,
    plan: EditPlan,
    transcript: Transcript,
    cfg: Config,
    mock: bool = False,
    screenshots: list[str] | None = None,
    icons_enabled: bool = True,
    max_zooms: int | None = None,
) -> dict:
    """Dry-run a fix on a copy of the plan. Never mutates state.

    Returns a JSON-serializable preview: op/patch, ranges in BOTH input-file
    and final-video time, covered phrase, affected items, duration delta,
    notes — or ok False with an error. Contract for the worker in
    confirmation-contract.md.
    """
    shots = screenshots or []
    timeline = TimelineMap(plan.segments)
    before_dur = timeline.output_dur
    base = {
        "ok": False,
        "instruction": instruction,
        "op": None,
        "plan_hash": plan_hash(plan),
    }
    try:
        patch = parse_fix(
            instruction, plan, transcript, cfg, mock=mock, screenshots=shots
        )
    except FixError as exc:
        return {**base, "error": str(exc)}
    sim = plan.model_copy(deep=True)
    try:
        new_plan, notes = apply_patch(
            patch, sim, transcript, icons_enabled, shots, max_zooms
        )
    except FixError as exc:
        return {
            **base,
            "op": patch.op.value,
            "patch": patch.model_dump(),
            "error": str(exc),
        }
    preview: dict = {
        **base,
        "ok": True,
        "op": patch.op.value,
        "patch": patch.model_dump(),
        "notes": notes,
    }
    lo, hi = patch.from_word, patch.to_word
    words = transcript.words
    if lo is not None and hi is not None and 0 <= lo <= hi < len(words):
        start_in, end_in = words[lo].s, words[hi].e
        preview["input_range_s"] = [round(start_in, 2), round(end_in, 2)]
        preview["output_range_s"] = [
            round(timeline.to_output(start_in), 2),
            round(timeline.to_output(end_in), 2),
        ]
        preview["phrase"] = " ".join(w.t for w in words[lo : hi + 1])[:120]
    old_ids = {v.id for v in plan.visuals}
    new_ids = {v.id for v in new_plan.visuals}
    preview["affected"] = {
        "segments_before": len(plan.segments),
        "segments_after": len(new_plan.segments),
        "visuals_removed": sorted(old_ids - new_ids),
        "visuals_added": sorted(new_ids - old_ids),
        "captions_before": len(plan.captions),
        "captions_after": len(new_plan.captions),
        "zooms_before": len(plan.zoom_at_words),
        "zooms_after": len(new_plan.zoom_at_words),
    }
    preview["duration"] = {
        "before_s": round(before_dur, 2),
        "after_s": round(TimelineMap(new_plan.segments).output_dur, 2),
    }
    return preview


def confirmation_text(preview: dict) -> str:
    """Owner-facing confirm prompt: what happens, in BOTH timelines."""
    if not preview.get("ok"):
        return f"can't do that yet: {preview.get('error', 'unknown error')}"
    bits = [f"I'll {preview['op'].replace('_', ' ')}"]
    if "input_range_s" in preview:
        a, b = preview["input_range_s"]
        bits.append(f"input {a:.1f}s-{b:.1f}s")
        if "output_range_s" in preview:
            c, d = preview["output_range_s"]
            bits.append(f"(final video {c:.1f}s-{d:.1f}s)")
    if preview.get("phrase"):
        bits.append(f"\u2018{preview['phrase']}\u2019")
    aff = preview.get("affected", {})
    extra: list[str] = []
    if aff.get("visuals_removed"):
        extra.append(f"removing overlay(s) {', '.join(aff['visuals_removed'])}")
    if aff.get("visuals_added"):
        extra.append(f"adding overlay(s) {', '.join(aff['visuals_added'])}")
    if aff.get("captions_before") != aff.get("captions_after"):
        extra.append(
            f"captions {aff.get('captions_before')}->{aff.get('captions_after')}"
        )
    dur = preview.get("duration", {})
    if dur and dur.get("before_s") != dur.get("after_s"):
        extra.append(f"output {dur['before_s']:.0f}s->{dur['after_s']:.0f}s")
    if extra:
        bits.append(f"[{'; '.join(extra)}]")
    if preview.get("frame"):
        bits.append(f"[tagged frame: {preview['frame']}]")
    return " ".join(bits) + " Reply YES to apply."


_FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


def preview_frame(
    source: Path,
    out_path: Path,
    t: float,
    label: str,
    fonts_dir: Path | None = None,
) -> Path | None:
    """One tagged frame at input-video time ``t``. Best effort: None on failure."""
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        font: Path | None = None
        if fonts_dir:
            found = sorted(fonts_dir.glob("*.ttf")) + sorted(fonts_dir.glob("*.otf"))
            font = found[0] if found else None
        if font is None:
            for cand in _FONT_CANDIDATES:
                if Path(cand).exists():
                    font = Path(cand)
                    break
        safe = label.replace("\\", "/").replace(":", "\\:").replace("'", "")
        if font is not None:
            # Windows drive colon must be backslash-escaped even inside quotes.
            fpath = font.as_posix().replace(":", "\\:")
            vf = (
                f"drawtext=fontfile='{fpath}':text='{safe}':"
                f"fontsize=44:fontcolor=white:borderw=2:"
                f"x=(w-text_w)/2:y=h-220"
            )
        else:
            vf = "drawbox=x=0:y=0:w=iw:h=24:c=yellow@0.9:t=fill"
        ff.ffmpeg(
            [
                "-y",
                "-ss",
                f"{max(0.0, t):.2f}",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                vf,
                str(out_path),
            ],
            timeout=120,
        )
        return out_path if out_path.exists() else None
    except Exception:  # noqa: BLE001 — tagging is best effort
        return None
