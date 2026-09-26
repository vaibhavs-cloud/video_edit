from __future__ import annotations

import pytest

from vedit.llm import LlmError, groq_chat, missing_key, provider
from vedit.stage.transcribe import TranscribeError, parse_groq


class _Resp:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = payload if isinstance(payload, str) else str(payload)

    def json(self):
        return self._payload


def test_provider_reads_config(cfg):
    assert provider(cfg) == cfg.models.provider


def test_missing_key_reports_provider_env(monkeypatch, cfg):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # config.yaml ships provider: groq
    assert missing_key(cfg) == "GROQ_API_KEY"
    monkeypatch.setenv("GROQ_API_KEY", "x")
    assert missing_key(cfg) is None


def test_groq_chat_uses_schema_then_falls_back(monkeypatch, cfg):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    calls: list[dict] = []

    def fake_post(url, *, headers=None, json=None, timeout=None):
        calls.append(json)
        if len(calls) == 1:
            return _Resp(400, '{"error": "unsupported response_format"}')
        return _Resp(200, {"choices": [{"message": {"content": '{"ok": true}'}}]})

    import vedit.llm as llm_mod

    monkeypatch.setattr(llm_mod.httpx, "post", fake_post)
    out = groq_chat(cfg, "m", "p", {"type": "object"})
    assert out == '{"ok": true}'
    assert len(calls) == 2
    assert "response_format" in calls[0]
    assert calls[1]["response_format"]["type"] == "json_object"
    assert "JSON Schema" in calls[1]["messages"][0]["content"]


def test_groq_chat_raises_on_auth_error(monkeypatch, cfg):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    import vedit.llm as llm_mod

    monkeypatch.setattr(
        llm_mod.httpx, "post", lambda *a, **k: _Resp(401, '{"error": "bad key"}')
    )
    with pytest.raises(LlmError, match="401"):
        groq_chat(cfg, "m", "p", {"type": "object"})


def test_parse_groq_builds_words():
    t = parse_groq(
        {
            "text": "hello world",
            "words": [
                {"word": "world", "start": 0.5, "end": 0.9},
                {"word": " hello", "start": 0.1, "end": 0.4},
            ],
        }
    )
    assert [w.t for w in t.words] == ["hello", "world"]
    assert t.words[0].i == 0
    assert t.words[0].s == pytest.approx(0.1)
    assert t.words[1].e == pytest.approx(0.9)


def test_parse_groq_rejects_missing_words():
    with pytest.raises(TranscribeError):
        parse_groq({"text": "no words here"})
