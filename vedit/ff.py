"""Thin, explicit wrappers around ffmpeg/ffprobe. No magic — every call is a list."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class FFmpegError(RuntimeError):
    pass


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise FFmpegError(f"{tool} not found on PATH — install ffmpeg first")


def _tail(text: str, limit: int = 1200) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def run(args: list[str], timeout: int = 900, cwd: str | Path | None = None) -> str:
    proc = subprocess.run(
        args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
        cwd=str(cwd) if cwd else None,
    )
    if proc.returncode != 0:
        cmd = " ".join(args[:8]) + (" ..." if len(args) > 8 else "")
        raise FFmpegError(
            f"command failed ({proc.returncode}): {cmd}\n{_tail(proc.stderr)}"
        )
    return proc.stderr


def ffmpeg(args: list[str], timeout: int = 900, cwd: str | Path | None = None) -> str:
    return run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", *args],
        timeout=timeout,
        cwd=cwd,
    )


def probe(path: str | Path) -> dict:
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if proc.returncode != 0:
        raise FFmpegError(f"ffprobe failed: {_tail(proc.stderr)}")
    return json.loads(proc.stdout)


def video_stream(info: dict) -> dict:
    for s in info.get("streams", []):
        if s.get("codec_type") == "video":
            return s
    raise FFmpegError("no video stream")


def audio_stream(info: dict) -> dict:
    for s in info.get("streams", []):
        if s.get("codec_type") == "audio":
            return s
    raise FFmpegError("no audio stream")


def duration_of(info: dict) -> float:
    fmt = info.get("format", {})
    if fmt.get("duration"):
        return float(fmt["duration"])
    for s in info.get("streams", []):
        if s.get("duration"):
            return float(s["duration"])
    raise FFmpegError("could not determine duration")


def detect_silences(
    path: str | Path, noise_db: float, min_dur_s: float, merge_gap_s: float
) -> list[tuple[float, float]]:
    """Energy-based silence intervals via silencedetect (stderr, info level).

    Run this on the RAW (pre-loudnorm) track: loudness normalisation lifts
    the noise floor and hides real pauses. Nearby intervals separated by less
    than merge_gap_s are fused (breath-level dips split long pauses).
    """
    stderr = run(
        [
            "ffmpeg",
            "-hide_banner",
            "-i",
            str(path),
            "-af",
            f"silencedetect=noise={noise_db}dB:d={min_dur_s}",
            "-vn",
            "-f",
            "null",
            "-",
        ],
        timeout=900,
    )
    starts: list[float] = []
    intervals: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        line = line.strip()
        if "silence_start:" in line:
            try:
                starts.append(float(line.split("silence_start:")[1].split()[0]))
            except ValueError:
                continue
        elif "silence_end:" in line and starts:
            try:
                end = float(line.split("silence_end:")[1].split()[0])
            except ValueError:
                starts.pop()
                continue
            start = starts.pop()
            if end > start:
                intervals.append((start, end))
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start - merged[-1][1] < merge_gap_s:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(s, e) for s, e in merged]
