"""Gemini call 1: audio -> word-level transcript.

Model and retries come from config.yaml. Word offsets arrive as strings like
"0.100s" (sometimes plain floats) — both are normalised to seconds here.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from vedit.config import Config
from vedit.schema import Transcript, Word


class TranscribeError(RuntimeError):
    pass


def _sec(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().removesuffix("s").strip()
        try:
            return float(text)
        except ValueError:
            return None
    return None


def _find_audio_blocks(node: Any) -> list[dict]:
    found: list[dict] = []
    if isinstance(node, dict):
        if "words" in node and isinstance(node.get("words"), list):
            found.append(node)
        for value in node.values():
            found.extend(_find_audio_blocks(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_audio_blocks(item))
    return found


def parse_response(resp: Any) -> Transcript:
    data = resp.model_dump(mode="json") if hasattr(resp, "model_dump") else resp
    words: list[Word] = []
    for block in _find_audio_blocks(data):
        for entry in block.get("words") or []:
            if not isinstance(entry, dict):
                continue
            start = _sec(entry.get("start_offset", entry.get("start")))
            end = _sec(entry.get("end_offset", entry.get("end")))
            text = str(entry.get("word", entry.get("text", ""))).strip()
            if start is None or end is None or not text:
                continue
            words.append(Word(i=0, t=text, s=start, e=max(start, end)))
    if not words:
        raise TranscribeError("no word-level timestamps in transcription response")
    words.sort(key=lambda w: (w.s, w.e))
    for i, w in enumerate(words):
        w.i = i
    return Transcript(words=words, text=" ".join(w.t for w in words))


def transcribe(wav_path: Path, cfg: Config) -> Transcript:
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        raise TranscribeError("GEMINI_API_KEY is not set")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)
    payload = wav_path.read_bytes()
    last_error: Exception | None = None

    for attempt in range(1, cfg.retry.llm_attempts + 1):
        try:
            resp = client.models.generate_content(
                model=cfg.models.transcribe,
                contents=[types.Part.from_bytes(data=payload, mime_type="audio/wav")],
                config=types.GenerateContentConfig(
                    audio_transcription_config=types.AudioTranscriptionConfig(
                        word_timestamp=True
                    )
                ),
            )
            return parse_response(resp)
        except Exception as exc:  # noqa: BLE001 — surfaced after bounded retries
            last_error = exc
            if attempt < cfg.retry.llm_attempts:
                time.sleep(cfg.retry.llm_backoff_s * attempt)

    raise TranscribeError(
        f"transcription failed after {cfg.retry.llm_attempts} attempts: {last_error}"
    )
