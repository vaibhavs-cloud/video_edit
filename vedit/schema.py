"""Data contracts for the editing pipeline.

Every timeline reference in EditPlan uses WORD INDICES in *source* time.
Deterministic stages (cuts, timeline map) own all float timestamps;
the LLM only ever sees and emits word indices.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Word(BaseModel):
    i: int = Field(ge=0)
    t: str
    s: float = Field(ge=0)
    e: float = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> Word:
        if self.e < self.s:
            raise ValueError(f"word {self.i}: end {self.e} < start {self.s}")
        return self


class Transcript(BaseModel):
    words: list[Word]
    text: str = ""

    @model_validator(mode="after")
    def _check(self) -> Transcript:
        for expected, w in enumerate(self.words):
            if w.i != expected:
                raise ValueError(
                    f"word indices must be contiguous: expected {expected}, got {w.i}"
                )
        if self.words and self.words[-1].e < self.words[-1].s:
            raise ValueError("transcript ends before it starts")
        return self


class Segment(BaseModel):
    """One kept run of the source timeline (between silence cuts)."""

    keep_from_word: int = Field(ge=0)
    keep_to_word: int = Field(ge=0)
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> Segment:
        if self.end <= self.start:
            raise ValueError(f"segment empty: [{self.start}, {self.end}]")
        if self.keep_to_word < self.keep_from_word:
            raise ValueError("keep_to_word < keep_from_word")
        return self


class Visual(BaseModel):
    id: str
    kind: Literal["icon", "screenshot"]
    keyword: str | None = None
    icon: str | None = None
    file: str | None = None
    file_id: str | None = None
    from_word: int = Field(ge=0)
    to_word: int = Field(ge=0)
    pos: Literal["top", "bottom", "top-right", "top-left", "center"] = "top-right"
    zoom: bool = False
    scale: float = Field(default=1.0, ge=0.2, le=1.0)  # fraction of natural size
    # per-visual display override (None = follow visuals.screenshot_mode):
    # cover = crop to fill the reel, adaptive = blur backdrop + contained image,
    # overlay = floating box; "fit" is a legacy alias of adaptive.
    mode: Literal["cover", "adaptive", "fit", "overlay"] | None = None
    # crop anchor when cropping to fill (top/center/bottom thirds)
    crop_focus: Literal["top", "center", "bottom"] | None = None

    @model_validator(mode="after")
    def _check(self) -> Visual:
        if self.to_word <= self.from_word:
            raise ValueError(f"{self.id}: to_word must be after from_word")
        if self.kind == "icon" and not (self.keyword or self.icon):
            raise ValueError(f"{self.id}: icon visual needs keyword/icon")
        if self.kind == "screenshot" and not (self.file or self.file_id):
            raise ValueError(f"{self.id}: screenshot visual needs file")
        return self


def insert_anchor(ins: Insert, words: list[Word], segments: list[Segment]) -> float:
    """Source time where a clip starts: before its word, or at a segment edge.

    A clip anchored to a segment's first kept word starts when that segment
    starts (i.e. before its pre-roll), so the boundary has a hard cut instead of
    crossfading under the clip.
    """
    if ins.from_word < len(words):
        for seg in segments:
            if seg.keep_from_word == ins.from_word:
                return seg.start
        return words[ins.from_word].s
    return segments[-1].end


class Insert(BaseModel):
    """A clip inserted into the reel at a word boundary (source time).

    `file` is relative to the run state directory (e.g. inserts/clip-1.mp4).
    `duration` is already resolved to the clip length available after
    `trim_start`; the renderer trims and does not guess.
    """

    id: str
    file: str
    from_word: int = Field(ge=0)  # clip starts BEFORE this word (n = end)
    trim_start: float = Field(default=0.0, ge=0)
    duration: float = Field(gt=0)
    muted: bool = False
    fit: Literal["adaptive", "cover"] = "adaptive"

    @model_validator(mode="after")
    def _check(self) -> Insert:
        if not self.file:
            raise ValueError(f"{self.id}: insert needs a file")
        return self


class CaptionSpan(BaseModel):
    from_word: int = Field(ge=0)
    to_word: int = Field(ge=0)
    emphasis: list[int] = Field(default_factory=list)
    override_text: str | None = None  # set by a recaption fix (correction loop)

    @model_validator(mode="after")
    def _check(self) -> CaptionSpan:
        if self.to_word < self.from_word:
            raise ValueError("caption to_word < from_word")
        for idx in self.emphasis:
            if not (self.from_word <= idx <= self.to_word):
                raise ValueError(f"emphasis word {idx} outside span")
        return self


class PlanSource(BaseModel):
    ref: str
    sha256: str
    w: int
    h: int
    dur: float


class Reframe(BaseModel):
    mode: Literal["scale", "crop"]
    aspect: float = 0.5625
    x_frac: float = 0.5


class EditPlan(BaseModel):
    version: int = 1
    source: PlanSource
    reframe: Reframe
    words: list[Word]
    segments: list[Segment]
    inserts: list[Insert] = Field(default_factory=list)
    visuals: list[Visual] = Field(default_factory=list)
    captions: list[CaptionSpan] = Field(default_factory=list)
    zoom_at_words: list[int] = Field(default_factory=list)
    prompt: str = ""
    degraded: bool = False

    @model_validator(mode="after")
    def _check(self) -> EditPlan:
        n = len(self.words)
        if not self.segments:
            raise ValueError("plan has no segments")
        prev_end = -1.0
        word_segment: dict[int, int] = {}
        for si, seg in enumerate(self.segments):
            if seg.start < prev_end:
                raise ValueError(f"segment {si} overlaps previous")
            prev_end = seg.end
            if not (0 <= seg.keep_from_word <= seg.keep_to_word < n):
                raise ValueError(f"segment {si} word range out of bounds")
            for wi in range(seg.keep_from_word, seg.keep_to_word + 1):
                word_segment[wi] = si
                w = self.words[wi]
                if not (seg.start <= w.s and w.e <= seg.end):
                    raise ValueError(f"word {wi} not inside segment {si}")

        def require_single_segment(a: int, b: int, what: str) -> None:
            if not (0 <= a <= b < n):
                raise ValueError(f"{what}: word range out of bounds ({a}..{b}, n={n})")
            segs = {word_segment.get(wi) for wi in range(a, b + 1)}
            if len(segs) != 1 or None in segs:
                raise ValueError(f"{what}: range crosses a cut boundary")

        for cap in self.captions:
            require_single_segment(cap.from_word, cap.to_word, "caption")
        for v in self.visuals:
            require_single_segment(v.from_word, v.to_word, f"visual {v.id}")
        for ins in self.inserts:
            if not 0 <= ins.from_word <= n:
                raise ValueError(f"insert {ins.id}: from_word out of bounds")
            if ins.from_word < n and ins.from_word not in word_segment:
                raise ValueError(f"insert {ins.id}: word {ins.from_word} was cut out")
        for zi in self.zoom_at_words:
            if not 0 <= zi < n:
                raise ValueError(f"zoom word {zi} out of bounds")
        ids = [v.id for v in self.visuals]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate visual ids")
        insert_ids = [i.id for i in self.inserts]
        if len(insert_ids) != len(set(insert_ids)):
            raise ValueError("duplicate insert ids")
        return self

    def segment_of_word(self, wi: int) -> int:
        for si, seg in enumerate(self.segments):
            if seg.keep_from_word <= wi <= seg.keep_to_word:
                return si
        raise KeyError(f"word {wi} not in any segment")

    def insert_anchor_s(self, ins: Insert) -> float:
        """Source timestamp where the clip begins (before `from_word`)."""
        return insert_anchor(ins, self.words, self.segments)


# ---- LLM draft output (what gemini-3.5-flash returns) ----


class DraftVisual(BaseModel):
    kind: Literal["icon", "screenshot"]
    keyword: str | None = None
    file: str | None = None
    from_word: int = Field(ge=0)
    to_word: int = Field(ge=0)
    pos: Literal["top", "bottom", "top-right", "top-left", "center"] = "top-right"
    zoom: bool = False


class DraftCaption(BaseModel):
    from_word: int = Field(ge=0)
    to_word: int = Field(ge=0)
    emphasis: list[int] = Field(default_factory=list)


class PlanDraft(BaseModel):
    visuals: list[DraftVisual] = Field(default_factory=list)
    captions: list[DraftCaption] = Field(default_factory=list)
    zoom_at_words: list[int] = Field(default_factory=list)


class IconPick(BaseModel):
    visual_id: str
    icon: str | None = None


class IconPicks(BaseModel):
    picks: list[IconPick] = Field(default_factory=list)


# ---- Correction loop ----


class FixOp(str, Enum):
    replace_icon = "replace_icon"
    remove_visual = "remove_visual"
    recaption = "recaption"
    retime_visual = "retime_visual"
    add_visual = "add_visual"
    cut_range = "cut_range"
    keep_range = "keep_range"
    add_zoom = "add_zoom"
    remove_zoom = "remove_zoom"
    resize_visual = "resize_visual"
    set_visual_mode = "set_visual_mode"
    set_preset = "set_preset"
    insert_video = "insert_video"
    retime_insert = "retime_insert"
    trim_insert = "trim_insert"
    remove_insert = "remove_insert"
    set_insert_fit = "set_insert_fit"
    set_insert_audio = "set_insert_audio"
    unknown = "unknown"


class FixPatch(BaseModel):
    op: FixOp
    visual_id: str | None = None
    keyword: str | None = None
    from_word: int | None = None
    to_word: int | None = None
    text: str | None = None
    file: str | None = None
    scale: float | None = None
    mode: Literal["cover", "adaptive", "fit", "overlay"] | None = None
    crop_focus: Literal["top", "center", "bottom"] | None = None
    preset: str | None = None
    insert_id: str | None = None
    duration: float | None = Field(default=None, gt=0)
    trim_start: float | None = Field(default=None, ge=0)
    muted: bool | None = None
    fit: Literal["adaptive", "cover"] | None = None
    note: str | None = None


# ---- Run state ----


class StateMeta(BaseModel):
    ref: str
    sha256: str
    source_kind: Literal["url", "telegram", "local"]
    source_ref: str
    chat_id: str = ""
    config_hash: str = ""
    created_at: str = ""
    mock: bool = False
