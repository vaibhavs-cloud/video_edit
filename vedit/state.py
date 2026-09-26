"""Run state on disk: resumable stages + artifacts for fix/report/deliver."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

STAGES = (
    "acquire",
    "audio",
    "transcribe",
    "cut",
    "plan",
    "visuals",
    "captions",
    "render",
    "qc",
    "report",
)


class StateError(RuntimeError):
    pass


def marker(state: Path, stage: str) -> Path:
    if stage not in STAGES:
        raise StateError(f"unknown stage: {stage}")
    return state / f".done.{stage}"


def is_done(state: Path, stage: str) -> bool:
    return marker(state, stage).exists()


def mark_done(state: Path, stage: str) -> None:
    marker(state, stage).write_text("ok", encoding="utf-8")


def clear_from(state: Path, stage: str) -> None:
    if stage not in STAGES:
        raise StateError(f"unknown stage: {stage}")
    start = STAGES.index(stage)
    for name in STAGES[start:]:
        p = marker(state, name)
        if p.exists():
            p.unlink()


def run_stage(state: Path, stage: str, fn: Callable[[], Any]) -> Any:
    """Run fn unless the stage marker exists; always mark on success."""
    if is_done(state, stage):
        return None
    out = fn()
    mark_done(state, stage)
    return out


def ensure_state(out_root: Path, name: str) -> Path:
    state = out_root / name
    state.mkdir(parents=True, exist_ok=True)
    (state / "work").mkdir(exist_ok=True)
    (state / "icons").mkdir(exist_ok=True)
    return state


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_model(path: Path, model: BaseModel) -> None:
    save_json(path, json.loads(model.model_dump_json()))


def load_model(path: Path, cls: type[T]) -> T:
    if not path.exists():
        raise StateError(f"missing state file: {path}")
    return cls.model_validate_json(path.read_text(encoding="utf-8"))


def copy_artifact(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest
