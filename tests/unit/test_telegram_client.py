from __future__ import annotations

import httpx
import pytest

from vedit import telegram_client as tg
from vedit.telegram_client import TelegramError


def _resp(status: int, payload=None) -> httpx.Response:
    return httpx.Response(status, json=payload if payload is not None else {"ok": True})


def test_post_retries_timeouts_then_succeeds(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs.get("json"))
        if len(calls) < 3:
            raise httpx.TimeoutException("slow")
        return _resp(200, {"ok": True, "result": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    out = tg._post("https://x", json={"a": 1}, timeout=5)
    assert out.status_code == 200
    assert len(calls) == 3


def test_post_honors_429_retry_after_then_succeeds(monkeypatch):
    sleeps = []
    seq = [
        _resp(429, {"ok": False, "parameters": {"retry_after": 0}}),
        _resp(200, {"ok": True}),
    ]

    monkeypatch.setattr(httpx, "post", lambda *a, **k: seq.pop(0))
    monkeypatch.setattr(tg.time, "sleep", lambda s: sleeps.append(s))
    out = tg._post("https://x", json={"a": 1})
    assert out.status_code == 200
    assert sleeps, "must back off on 429"


def test_post_does_not_retry_400(monkeypatch):
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(1)
        return _resp(400, {"ok": False, "description": "Bad Request: invalid file_id"})

    monkeypatch.setattr(httpx, "post", fake_post)
    out = tg._post("https://x", json={"a": 1})
    assert out.status_code == 400
    assert len(calls) == 1


def test_post_raises_after_retries_exhausted(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _resp(500, {"ok": False}))
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    with pytest.raises(TelegramError):
        tg._post("https://x", json={"a": 1})


def test_send_video_resends_bytes_on_retry(tmp_path, monkeypatch):
    bodies = []

    def fake_post(*args, **kwargs):
        bodies.append(kwargs["files"]["video"][1])
        if len(bodies) == 1:
            raise httpx.TimeoutException("slow")
        return _resp(200, {"ok": True, "result": True})

    monkeypatch.setattr(httpx, "post", fake_post)
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)
    cli = tg.Telegram(token="t")
    path = tmp_path / "v.mp4"
    path.write_bytes(b"fake-video-bytes")
    cli.send_video("1", path, "cap")
    assert bodies == [b"fake-video-bytes", b"fake-video-bytes"]
