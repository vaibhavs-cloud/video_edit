"""Input acquisition: Drive link / direct URL / local file / Telegram file_id.

Everything lands at state/source.mp4 so later stages never care where the
input came from. Size and duration limits are enforced here, once.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import httpx

from vedit import ff
from vedit.config import Config

_MAX_URL_BYTES = 4 << 30  # hard stop for runaway downloads


class AcquireError(RuntimeError):
    pass


def _is_drive(url: str) -> bool:
    return "drive.google.com" in url or "docs.google.com" in url


def acquire(input_ref: str, dest: Path, cfg: Config) -> tuple[Path, str]:
    """Returns (local_path, source_kind)."""
    if not input_ref or not input_ref.strip():
        raise AcquireError(
            "no source provided: pass a Drive/direct URL, tg:<file_id>, or local path"
        )
    dest.parent.mkdir(parents=True, exist_ok=True)

    if input_ref.startswith("tg:"):
        from vedit.telegram_client import Telegram

        Telegram().download(input_ref[3:], dest, cfg.limits.telegram_download_max_bytes)
        return dest, "telegram"

    if input_ref.startswith(("http://", "https://")):
        if _is_drive(input_ref):
            import gdown

            result = gdown.download(url=input_ref, output=str(dest), quiet=True)
            if not result or not dest.exists() or dest.stat().st_size == 0:
                raise AcquireError(f"gdown could not fetch {input_ref}")
            return dest, "url"
        with httpx.stream("GET", input_ref, follow_redirects=True, timeout=120) as resp:
            resp.raise_for_status()
            written = 0
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(chunk_size=1 << 20):
                    written += len(chunk)
                    if written > _MAX_URL_BYTES:
                        raise AcquireError(f"download exceeded {_MAX_URL_BYTES} bytes")
                    fh.write(chunk)
        if dest.stat().st_size == 0:
            raise AcquireError(f"empty download from {input_ref}")
        return dest, "url"

    src = Path(input_ref)
    if not src.exists():
        raise AcquireError(f"input not found: {src}")
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest, "local"


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_media(path: Path, cfg: Config) -> dict:
    """Probe + limit checks. Returns {w, h, dur, sha256}."""
    info = ff.probe(path)
    v = ff.video_stream(info)
    ff.audio_stream(info)
    dur = ff.duration_of(info)
    if dur > cfg.limits.max_input_s:
        raise AcquireError(
            f"input too long: {dur:.0f}s > {cfg.limits.max_input_s:.0f}s"
        )
    return {
        "w": int(v.get("width", 0)),
        "h": int(v.get("height", 0)),
        "dur": dur,
        "sha256": sha256_of(path),
    }
