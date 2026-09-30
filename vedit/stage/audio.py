"""Audio extraction + deterministic voice enhancement.

Fixed filter chain (versioned here, not scattered across the codebase):
    highpass -> bass_boost (low-shelf, optional) -> afftdn -> acompressor
    -> two-pass loudnorm -> volume boost (optional, peak-limited)

Everything is length-preserving, so the enhanced track stays aligned with the
source timeline and can be cut with the exact same bounds as the video.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict

from vedit import ff
from vedit.config import Config


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioPaths:
    raw_48k: Path
    clean_48k: Path
    stt_16k: Path


class LoudnormMeasured(TypedDict):
    input_i: str
    input_tp: str
    input_lra: str
    input_thresh: str
    target_offset: str


def _base_filters(cfg: Config) -> str:
    a = cfg.audio
    parts = [f"highpass=f={a.highpass_hz}"]
    if a.bass_boost_gain_db:
        parts.append(
            f"equalizer=f={a.bass_boost_hz}:t=h:w={a.bass_boost_width}:g={a.bass_boost_gain_db}"
        )
    parts.append(f"afftdn=nf={a.afftdn_nf}:nr={a.denoise_reduction_db:g}")
    parts.append(
        f"acompressor=threshold={a.comp_threshold_db}dB:ratio={a.comp_ratio}:"
        f"attack={a.comp_attack_ms}:release={a.comp_release_ms}"
    )
    return ",".join(parts)


def _measure(cfg: Config, raw: Path) -> LoudnormMeasured:
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


def _enhance_filter(cfg: Config, measured: LoudnormMeasured | None) -> str:
    a = cfg.audio
    if measured is None:
        chain = f"{_base_filters(cfg)},loudnorm=I={a.loudnorm_i}:TP={a.loudnorm_tp}:LRA={a.loudnorm_lra}"
    else:
        chain = (
            f"{_base_filters(cfg)},"
            f"loudnorm=I={a.loudnorm_i}:TP={a.loudnorm_tp}:LRA={a.loudnorm_lra}:"
            f"measured_I={measured['input_i']}:measured_TP={measured['input_tp']}:"
            f"measured_LRA={measured['input_lra']}:measured_thresh={measured['input_thresh']}:"
            f"offset={measured['target_offset']}:linear=true"
        )
    if a.volume_gain_db > 0:
        # Extra loudness after loudnorm. level=0 keeps the limiter transparent
        # (no auto-level), latency=1 compensates the lookahead delay so the
        # track stays time-aligned; both together preserve exact length.
        limit = 10 ** (a.loudnorm_tp / 20.0)
        chain += (
            f",volume={a.volume_gain_db:g}dB,"
            f"alimiter=limit={limit:.4f}:level=0:latency=1"
        )
    elif a.volume_gain_db < 0:
        chain += f",volume={a.volume_gain_db:g}dB"
    return chain


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
                str(cfg.audio.channels),
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(raw),
            ]
        )
    if not clean.exists():
        measured = _measure(cfg, raw)
        # loudnorm processes internally at 192 kHz — pin the output rate so
        # clean_48k.wav keeps its contract (and stays 4x smaller)
        ff.ffmpeg(
            [
                "-i",
                str(raw),
                "-af",
                _enhance_filter(cfg, measured),
                "-ar",
                "48000",
                str(clean),
            ]
        )
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
