"""Empty/missing-source guards for input acquisition."""

from __future__ import annotations

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
