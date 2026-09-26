from __future__ import annotations

from vedit.stage import visuals
from vedit.stage.visuals import fetch_icon


class _Resp:
    content = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10">'
        b'<rect width="10" height="10"/></svg>'
    )


def test_fetch_icon_creates_missing_cache_dir(monkeypatch, tmp_path, cfg):
    """CI fix runs restore state without empty dirs — fetch must self-heal."""
    monkeypatch.setattr(visuals, "_get", lambda *a, **k: _Resp())
    cache = tmp_path / "state" / "icons"
    assert not cache.exists()
    png = fetch_icon("lucide:test-icon", cfg, cache)
    assert png.exists() and png.stat().st_size > 0
    assert (cache / "lucide-test-icon.svg").exists()
