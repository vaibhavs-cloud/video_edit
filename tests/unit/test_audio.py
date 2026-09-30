"""Bass-boost low-shelf stage in the deterministic audio filter chain."""

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
