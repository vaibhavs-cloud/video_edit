"""Audio extraction + deterministic voice enhancement.

Fixed filter chain (versioned here, not scattered across the codebase):
    highpass -> afftdn -> acompressor -> two-pass loudnorm

Everything is length-preserving, so the enhanced track stays aligned with the
source timeline and can be cut with the exact same bounds as the video.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from vedit import ff
from vedit.config import Config


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioPaths:
    raw_48k: Path
    clean_48k: Path
    stt_16k: Path


def _base_filters(cfg: Config) -> str:
    a = cfg.audio
    return (
        f"highpass=f={a.highpass_hz},"
        f"afftdn=nf={a.afftdn_nf},"
        f"acompressor=threshold={a.comp_threshold_db}dB:ratio={a.comp_ratio}:"
        f"attack={a.comp_attack_ms}:release={a.comp_release_ms}"
    )


def _measure(cfg: Config, raw: Path) -> dict:
    a = cfg.audio
    filt = (
        f"{_base_filters(cfg)},"
        f"loudnorm=I={a.loudnorm_i}:TP={a.loudnorm_tp}:LRA={a.loudnorm_lra}:print_format=json"
    )
    # loudnorm's JSON report is printed at info level — do not force -loglevel error
    stderr = ff.run(
        ["ffmpeg", "-hide_banner", "-i", str(raw), "-af", filt, "-f", "null", "-"],
        timeout=600,
    )
    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.DOTALL)
    if not matches:
        raise AudioError(f"loudnorm measure pass produced no JSON:\n{stderr[-800:]}")
    return json.loads(matches[-1])


def _enhance_filter(cfg: Config, measured: dict | None) -> str:
    a = cfg.audio
    if measured is None:
        return f"{_base_filters(cfg)},loudnorm=I={a.loudnorm_i}:TP={a.loudnorm_tp}:LRA={a.loudnorm_lra}"
    return (
        f"{_base_filters(cfg)},"
        f"loudnorm=I={a.loudnorm_i}:TP={a.loudnorm_tp}:LRA={a.loudnorm_lra}:"
        f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
        f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
        f"offset={measured['target_offset']}:linear=true"
    )


def prepare(source: Path, workdir: Path, cfg: Config) -> AudioPaths:
    work = workdir / "audio"
    work.mkdir(parents=True, exist_ok=True)
    raw = work / "raw_48k.wav"
    clean = work / "clean_48k.wav"
    stt = work / "stt_16k.wav"

    if not raw.exists():
        ff.ffmpeg(
            [
                "-i",
                str(source),
                "-vn",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(raw),
            ]
        )
    if not clean.exists():
        measured = _measure(cfg, raw)
        ff.ffmpeg(["-i", str(raw), "-af", _enhance_filter(cfg, measured), str(clean)])
    if not stt.exists():
        ff.ffmpeg(
            [
                "-i",
                str(clean),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "pcm_s16le",
                str(stt),
            ]
        )

    return AudioPaths(raw_48k=raw, clean_48k=clean, stt_16k=stt)
