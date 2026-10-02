"""Caption generation: word spans -> .srt + .ass, in OUTPUT time.

Two rules matter here:
  * a caption line never crosses a cut boundary (jump-cut visual continuity)
  * timestamps are produced through TimelineMap, so captions follow the cuts
Emphasis words get a colour override inside the ASS markup only.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vedit.config import Config
from vedit.schema import EditPlan
from vedit.stage.timeline import TimelineMap


@dataclass(frozen=True)
class CaptionLine:
    start: float
    end: float
    text_plain: str
    text_ass: str
    word_indices: tuple[int, ...]


def _ass_time(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = round((t - int(t)) * 100)
    if cs == 100:
        cs = 0
        s += 1
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _srt_time(t: float) -> str:
    t = max(0.0, t)
    ms = round(t * 1000)
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_lines(plan: EditPlan, cfg: Config) -> list[CaptionLine]:
    timeline = TimelineMap(plan.segments)
    lines: list[CaptionLine] = []

    for span in plan.captions:
        if span.override_text:
            first, last = span.from_word, span.to_word
            out_start = timeline.to_output(plan.words[first].s)
            out_end = timeline.to_output(plan.words[last].e)
            lines.append(
                CaptionLine(
                    start=out_start,
                    end=max(out_start + 0.4, out_end),
                    text_plain=span.override_text,
                    text_ass=span.override_text,
                    word_indices=tuple(range(first, last + 1)),
                )
            )
            continue

        chunk: list[int] = []
        width = 0

        def flush(span=span) -> None:
            nonlocal chunk, width
            if not chunk:
                return
            first, last = chunk[0], chunk[-1]
            plain_words = [plan.words[i].t for i in chunk]
            plain = " ".join(plain_words)
            pieces: list[str] = []
            for i in chunk:
                token = plan.words[i].t
                if i in span.emphasis:
                    pieces.append(
                        f"{{\\b1\\1c{cfg.captions.emphasis_color}&}}{token}{{\\b0\\1c{cfg.captions.primary_color}&}}"
                    )
                else:
                    pieces.append(token)
            out_start = timeline.to_output(plan.words[first].s)
            out_end = timeline.to_output(plan.words[last].e)
            lines.append(
                CaptionLine(
                    start=out_start,
                    end=max(out_start + 0.4, out_end),
                    text_plain=plain,
                    text_ass=" ".join(pieces),
                    word_indices=tuple(chunk),
                )
            )
            chunk, width = [], 0

        for wi in range(span.from_word, span.to_word + 1):
            word = plan.words[wi]
            addition = len(word.t) + (1 if chunk else 0)
            if chunk and width + addition > cfg.captions.max_chars:
                flush()
            chunk.append(wi)
            width += addition
            # never let one line straddle a cut
            if wi < span.to_word and plan.segment_of_word(wi) != plan.segment_of_word(
                wi + 1
            ):
                flush()
        flush()

    lines.sort(key=lambda l: l.start)
    for i in range(1, len(lines)):
        if lines[i].start < lines[i - 1].end:
            lines[i] = CaptionLine(
                start=lines[i - 1].end,
                end=max(lines[i - 1].end + 0.4, lines[i].end),
                text_plain=lines[i].text_plain,
                text_ass=lines[i].text_ass,
                word_indices=lines[i].word_indices,
            )
    return lines


def coverage(plan: EditPlan) -> float:
    """Fraction of KEPT words covered by captions (cut words need none)."""
    kept = {
        wi
        for seg in plan.segments
        for wi in range(seg.keep_from_word, seg.keep_to_word + 1)
    }
    covered = {
        wi for span in plan.captions for wi in range(span.from_word, span.to_word + 1)
    }
    return len(covered & kept) / max(1, len(kept))


def write_srt(lines: list[CaptionLine], path: Path) -> Path:
    blocks = []
    for i, line in enumerate(lines, start=1):
        blocks.append(
            f"{i}\n{_srt_time(line.start)} --> {_srt_time(line.end)}\n{text_of(line)}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")
    return path


def text_of(line: CaptionLine) -> str:
    return line.text_plain


def write_ass(
    lines: list[CaptionLine], cfg: Config, path: Path, width: int, height: int
) -> Path:
    c = cfg.captions
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{c.font},{c.font_size},{c.primary_color},{c.primary_color},&H00000000,&H7F000000,0,0,0,0,100,100,0,0,1,4,0,2,60,60,{c.margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = [
        f"Dialogue: 0,{_ass_time(l.start)},{_ass_time(l.end)},Cap,,0,0,0,,{l.text_ass}"
        for l in lines
    ]
    path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
    return path


def generate(
    plan: EditPlan, cfg: Config, out_dir: Path
) -> tuple[Path, Path, list[CaptionLine]]:
    lines = build_lines(plan, cfg)
    srt = write_srt(lines, out_dir / "captions.srt")
    ass = write_ass(
        lines, cfg, out_dir / "captions.ass", cfg.video.width, cfg.video.height
    )
    return srt, ass, lines
