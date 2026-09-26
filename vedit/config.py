"""Typed access to config.yaml. Every tuning knob lives there, never in code."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class ModelsCfg:
    transcribe: str
    plan: str
    visuals: str


@dataclass(frozen=True)
class CutsCfg:
    cut_gap_ms: int
    keep_gap_ms: int
    pad_ms: int
    min_kept_segment_ms: int
    min_output_s: float
    adaptive_multiplier: float
    pre_roll_ms: int
    post_roll_ms: int


@dataclass(frozen=True)
class AudioCfg:
    highpass_hz: int
    afftdn_nf: int
    comp_threshold_db: int
    comp_ratio: int
    comp_attack_ms: int
    comp_release_ms: int
    loudnorm_i: float
    loudnorm_tp: float
    loudnorm_lra: float


@dataclass(frozen=True)
class VideoCfg:
    width: int
    height: int
    fps: int
    x_frac: float
    zoom_factor: float
    max_zoom_segments_div: int
    x264_preset: str
    x264_crf: int
    audio_bitrate: str


@dataclass(frozen=True)
class VisualsCfg:
    prefixes: str
    search_limit: int
    shortlist: int
    density_window_s: float
    max_visuals: int
    icon_color: str
    icon_size: int
    fade_s: float
    screenshot_width_frac: float


@dataclass(frozen=True)
class CaptionsCfg:
    font: str
    font_size: int
    max_chars: int
    margin_v: int
    primary_color: str
    emphasis_color: str


@dataclass(frozen=True)
class QcCfg:
    duration_tolerance_s: float
    min_word_coverage: float


@dataclass(frozen=True)
class LimitsCfg:
    max_output_s: float
    max_input_s: float
    telegram_download_max_bytes: int
    telegram_send_max_bytes: int


@dataclass(frozen=True)
class RetryCfg:
    llm_attempts: int
    llm_backoff_s: float
    http_attempts: int
    http_backoff_s: float


@dataclass(frozen=True)
class Config:
    models: ModelsCfg
    cuts: CutsCfg
    audio: AudioCfg
    video: VideoCfg
    visuals: VisualsCfg
    captions: CaptionsCfg
    qc: QcCfg
    limits: LimitsCfg
    retry: RetryCfg
    hash: str


def load_config(path: str | Path) -> Config:
    p = Path(path)
    raw = p.read_bytes()
    data = yaml.safe_load(raw) or {}
    return Config(
        models=ModelsCfg(**data["models"]),
        cuts=CutsCfg(**data["cuts"]),
        audio=AudioCfg(**data["audio"]),
        video=VideoCfg(**data["video"]),
        visuals=VisualsCfg(**data["visuals"]),
        captions=CaptionsCfg(**data["captions"]),
        qc=QcCfg(**data["qc"]),
        limits=LimitsCfg(**data["limits"]),
        retry=RetryCfg(**data["retry"]),
        hash=hashlib.sha256(raw).hexdigest()[:16],
    )
