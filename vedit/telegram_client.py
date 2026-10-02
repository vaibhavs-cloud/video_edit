"""Minimal Telegram Bot API client (downloads + delivery only).

Interaction design lives in the Worker; this client only pulls a source file
and pushes results, enforcing the bot API size limits from config.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import httpx


class TelegramError(RuntimeError):
    pass


def _retry_after_s(resp: httpx.Response) -> int:
    try:
        return max(0, int(resp.json().get("parameters", {}).get("retry_after", 2)))
    except Exception:  # noqa: BLE001 — best-effort parse
        return 2


def _post(url: str, *, data=None, json=None, files=None, timeout=60) -> httpx.Response:
    """POST with retries for transient failures (timeouts, 5xx, 429 flood waits).

    4xx other than 429 are permanent (bad request, bad file_id) and fail fast.
    File payloads must be bytes (not open handles) so attempts can re-send.
    """
    last: Exception | None = None
    for attempt in range(3):
        try:
            resp = httpx.post(url, data=data, json=json, files=files, timeout=timeout)
        except (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.RemoteProtocolError,
        ) as exc:
            last = exc
        else:
            if resp.status_code == 429:
                wait = _retry_after_s(resp)
                time.sleep(min(wait + 1, 60))
                last = TelegramError(f"telegram 429, retry after {wait}s")
                continue
            if resp.status_code >= 500:
                last = TelegramError(f"telegram {resp.status_code}")
                time.sleep(min(2**attempt, 8))
                continue
            return resp
        if attempt < 2:
            time.sleep(min(2**attempt, 8))
    assert last is not None
    raise last


class Telegram:
    def __init__(self, token: str | None = None, timeout: float = 300.0):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not self.token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is not set")
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _ok(self, resp: httpx.Response) -> Any:
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(
                f"telegram {resp.url.path}: {data.get('description', resp.text[:200])}"
            )
        return data["result"]

    def resolve(self, file_id: str) -> dict:
        return self._ok(
            httpx.get(self._url("getFile"), params={"file_id": file_id}, timeout=60)
        )

    def download(self, file_id: str, dest: Path, max_bytes: int) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        info = self.resolve(file_id)
        size = int(info.get("file_size", 0))
        if size > max_bytes:
            raise TelegramError(f"file too large: {size} > {max_bytes}")
        file_url = f"https://api.telegram.org/file/bot{self.token}/{info['file_path']}"
        with self._client.stream("GET", file_url) as resp:
            resp.raise_for_status()
            written = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 16):
                    written += len(chunk)
                    if written > max_bytes:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        raise TelegramError(f"stream exceeded {max_bytes} bytes")
                    fh.write(chunk)
        return dest

    def send_message(self, chat_id: str, text: str) -> None:
        self._ok(
            _post(
                self._url("sendMessage"),
                json={"chat_id": chat_id, "text": text[:4000]},
                timeout=60,
            )
        )

    def send_video(self, chat_id: str, path: Path, caption: str = "") -> None:
        payload = path.read_bytes()
        self._ok(
            _post(
                self._url("sendVideo"),
                data={
                    "chat_id": chat_id,
                    "caption": caption[:1000],
                    "supports_streaming": "true",
                },
                files={"video": (path.name, payload, "video/mp4")},
                timeout=600,
            )
        )

    def send_photo(self, chat_id: str, path: Path, caption: str = "") -> None:
        payload = path.read_bytes()
        self._ok(
            _post(
                self._url("sendPhoto"),
                data={"chat_id": chat_id, "caption": caption[:1000]},
                files={"photo": (path.name, payload, "image/jpeg")},
                timeout=120,
            )
        )

    def close(self) -> None:
        self._client.close()
