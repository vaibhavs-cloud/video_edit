"""Empty/missing-source guards for input acquisition."""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from vedit.acquire import AcquireError, acquire


def test_acquire_rejects_empty_source(tmp_path, cfg):
    with pytest.raises(AcquireError, match="no source provided"):
        acquire("", tmp_path / "source.mp4", cfg)
    with pytest.raises(AcquireError, match="no source provided"):
        acquire("   ", tmp_path / "source.mp4", cfg)


def test_acquire_rejects_missing_file(tmp_path, cfg):
    with pytest.raises(AcquireError, match="input not found"):
        acquire(str(tmp_path / "nope.mp4"), tmp_path / "source.mp4", cfg)


def test_acquire_drive_avoids_removed_gdown_kwargs(monkeypatch, tmp_path, cfg):
    """gdown>=5 removed fuzzy: only pass kwargs the installed version takes."""
    import inspect

    import gdown

    calls: dict = {}
    fake = types.ModuleType("gdown")

    def download(**kw):
        calls.update(kw)
        Path(str(kw["output"])).write_bytes(b"x")
        return str(kw["output"])

    fake.download = download
    monkeypatch.setitem(sys.modules, "gdown", fake)
    dest = tmp_path / "source.mp4"
    kind_path, kind = acquire(
        "https://drive.google.com/file/d/ABC123/view?usp=drivesdk", dest, cfg
    )
    assert (kind_path, kind) == (dest, "url")
    allowed = set(inspect.signature(gdown.download).parameters)
    assert set(calls) <= allowed, set(calls) - allowed
