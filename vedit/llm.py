"""Provider-agnostic LLM access: Groq (OpenAI-compatible) or Gemini.

Provider and model IDs live in config.yaml — never hardcoded. Structured
output is requested per-provider; downstream pydantic validation plus the
stage-level repair loop are the real contract either way.
"""

from __future__ import annotations

import json
import os
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from vedit.config import Config

GROQ_BASE = "https://api.groq.com/openai/v1"

T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    pass


def provider(cfg: Config) -> str:
    return (cfg.models.provider or "gemini").lower()


def missing_key(cfg: Config) -> str | None:
    """Name of the env var the configured provider needs, if unset."""
    name = "GROQ_API_KEY" if provider(cfg) == "groq" else "GEMINI_API_KEY"
    return name if not os.environ.get(name) else None


def make_client(cfg: Config) -> Any:
    if provider(cfg) == "groq":
        return None  # stateless HTTP, no client object needed
    from google import genai

    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def groq_chat(cfg: Config, model: str, prompt: str, schema: dict) -> str:
    """One chat completion with JSON-schema structured output.

    Falls back to json_object + schema-in-prompt when the model rejects
    response_format; pydantic validation downstream enforces the contract.
    """
    key = os.environ["GROQ_API_KEY"]
    schema_note = (
        "Answer with raw JSON only, matching this JSON Schema:\n" + json.dumps(schema)
    )
    bodies = [
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 8192,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "output", "strict": True, "schema": schema},
            },
        },
        {
            "model": model,
            "messages": [{"role": "user", "content": prompt + "\n\n" + schema_note}],
            "temperature": 0.2,
            "max_tokens": 8192,
            "response_format": {"type": "json_object"},
        },
    ]
    last: Exception = LlmError("no request attempted")
    for body in bodies:
        resp = httpx.post(
            f"{GROQ_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
            timeout=90.0,
        )
        if resp.status_code == 200:
            text = resp.json()["choices"][0]["message"].get("content")
            if text:
                return text
            last = LlmError("empty completion")
            continue
        last = LlmError(f"groq chat {resp.status_code}: {resp.text[:300]}")
        if resp.status_code in (400, 422):
            continue  # try the next request shape
        break  # auth/rate-limit/server — stage retry loop decides
    raise LlmError(str(last))


def _gemini_generate_json(client: Any, model: str, prompt: str, schema: type[T]) -> str:
    from google.genai import types

    schema_json = schema.model_json_schema()
    attempt_configs: list[Any]
    try:
        attempt_configs = [
            {
                "response_format": {
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema_json,
                }
            },
            types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema_json
            ),
        ]
    except Exception:  # noqa: BLE001 — pragma: no cover — SDK shape differences
        attempt_configs = [
            types.GenerateContentConfig(
                response_mime_type="application/json", response_schema=schema_json
            )
        ]

    last: Exception | None = None
    for cfg_item in attempt_configs:
        try:
            resp = client.models.generate_content(
                model=model, contents=prompt, config=cfg_item
            )
            text = resp.text
            if text:
                return text
        except (TypeError, ValueError) as exc:
            last = exc
            continue
    raise LlmError(f"could not call model with structured output: {last}")


def generate_json(
    client: Any, cfg: Config, model: str, prompt: str, schema: type[T]
) -> str:
    if provider(cfg) == "groq":
        return groq_chat(cfg, model, prompt, schema.model_json_schema())
    return _gemini_generate_json(client, model, prompt, schema)
