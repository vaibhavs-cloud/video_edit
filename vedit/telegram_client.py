"""Minimal Telegram Bot API client (downloads + delivery only).

Interaction design lives in the Worker; this client only pulls a source file
and pushes results, enforcing the bot API size limits from config.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx


class TelegramError(RuntimeError):
    pass


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
            httpx.post(
                self._url("sendMessage"),
                json={"chat_id": chat_id, "text": text[:4000]},
                timeout=60,
            )
        )

    def send_video(self, chat_id: str, path: Path, caption: str = "") -> None:
        with path.open("rb") as fh:
            self._ok(
                httpx.post(
                    self._url("sendVideo"),
                    data={
                        "chat_id": chat_id,
                        "caption": caption[:1000],
                        "supports_streaming": "true",
                    },
                    files={"video": (path.name, fh, "video/mp4")},
                    timeout=600,
                )
            )

    def send_photo(self, chat_id: str, path: Path, caption: str = "") -> None:
        with path.open("rb") as fh:
            self._ok(
                httpx.post(
                    self._url("sendPhoto"),
                    data={"chat_id": chat_id, "caption": caption[:1000]},
                    files={"photo": (path.name, fh, "image/jpeg")},
                    timeout=120,
                )
            )

    def close(self) -> None:
        self._client.close()
