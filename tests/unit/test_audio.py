"""Bass-boost low-shelf, denoise strength, and volume stage in the audio chain."""

from __future__ import annotations

import dataclasses

from vedit.stage import audio as audio_stage


def test_bass_boost_filter_string_included(cfg):
    filt = audio_stage._base_filters(cfg)
    a = cfg.audio
    assert f"highpass=f={a.highpass_hz}" in filt
    assert (
        f"equalizer=f={a.bass_boost_hz}:t=h:"
        f"w={a.bass_boost_width}:g={a.bass_boost_gain_db}" in filt
    )
    assert f"afftdn=nf={a.afftdn_nf}" in filt
    # order: highpass -> bass boost -> denoise -> compressor
    assert (
        filt.index("highpass")
        < filt.index("equalizer")
        < filt.index("afftdn")
        < filt.index("acompressor")
    )


def test_bass_boost_disabled_omits_equalizer(cfg):
    cfg2 = dataclasses.replace(
        cfg, audio=dataclasses.replace(cfg.audio, bass_boost_gain_db=0)
    )
    filt = audio_stage._base_filters(cfg2)
    assert "equalizer" not in filt
    assert "highpass" in filt and "afftdn" in filt


def test_denoise_reduction_knob_in_chain(cfg):
    a = cfg.audio
    assert f"afftdn=nf={a.afftdn_nf}:nr={a.denoise_reduction_db:g}" in (
        audio_stage._base_filters(cfg)
    )


def test_volume_boost_applies_with_peak_limit(cfg):
    a = cfg.audio
    assert a.volume_gain_db > 0  # default config lifts volume after loudnorm
    chain = audio_stage._enhance_filter(cfg, None)
    assert f"volume={a.volume_gain_db:g}dB" in chain
    limit = 10 ** (a.loudnorm_tp / 20.0)
    assert f"alimiter=limit={limit:.4f}:level=0:latency=1" in chain
    # loudnorm first, then boost, then peak guard
    assert chain.index("loudnorm") < chain.index("volume=") < chain.index("alimiter")


def test_volume_zero_skips_boost_and_limiter(cfg):
    cfg2 = dataclasses.replace(
        cfg, audio=dataclasses.replace(cfg.audio, volume_gain_db=0)
    )
    chain = audio_stage._enhance_filter(cfg2, None)
    assert "volume=" not in chain
    assert "alimiter" not in chain


def test_volume_negative_applies_without_limiter(cfg):
    cfg2 = dataclasses.replace(
        cfg, audio=dataclasses.replace(cfg.audio, volume_gain_db=-6)
    )
    chain = audio_stage._enhance_filter(cfg2, None)
    assert "volume=-6dB" in chain
    assert "alimiter" not in chain  # attenuation cannot clip — no peak guard needed
