"""Visual resolution: Iconify shortlist -> one batched LLM pick -> PNG assets.

Deterministic post-checks (density cap, max visuals) run after the pick, so a
confident-but-wrong model answer still cannot overcrowd the video.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from vedit.config import Config
from vedit.schema import IconPicks, Transcript, Visual
from vedit.stage.assets import render_svg_to_png, resolve_screenshot
from vedit.stage.plan import generate_json


class VisualsError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedVisual:
    visual: Visual
    asset: Path | None


def _get(url: str, cfg: Config, params: dict | None = None) -> httpx.Response:
    last: Exception | None = None
    for attempt in range(1, cfg.retry.http_attempts + 1):
        try:
            with httpx.Client(timeout=20.0, follow_redirects=True) as client:
                resp = client.get(url, params=params)
                resp.raise_for_status()
                return resp
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(cfg.retry.http_backoff_s * attempt)
    raise VisualsError(f"HTTP failed for {url}: {last}")


def shortlists(visuals: list[Visual], cfg: Config) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for v in visuals:
        if v.kind != "icon" or not v.keyword:
            continue
        resp = _get(
            "https://api.iconify.design/search",
            cfg,
            params={
                "query": v.keyword,
                "prefixes": cfg.visuals.prefixes,
                "limit": cfg.visuals.search_limit,
            },
        )
        icons = [str(name) for name in resp.json().get("icons", [])][
            : cfg.visuals.shortlist
        ]
        out[v.id] = icons
    return out


def pick_icons(
    shortlists_map: dict[str, list[str]], cfg: Config, mock: dict | None = None
) -> dict[str, str | None]:
    if mock is not None:
        return {vid: mock.get(vid) for vid in shortlists_map}
    if not shortlists_map:
        return {}
    import os

    if not os.environ.get("GEMINI_API_KEY"):
        return {vid: None for vid in shortlists_map}

    from google import genai

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    prompt = (
        "You pick the single best icon for each concept from a shortlist.\n"
        'Return JSON: {"picks": [{"visual_id": str, "icon": "prefix:name" | null}]}.\n'
        "Use null when no candidate actually illustrates the concept.\n\n"
        + json.dumps(shortlists_map, ensure_ascii=False)
    )
    last: Exception | None = None
    for attempt in range(1, cfg.retry.llm_attempts + 1):
        try:
            raw = generate_json(client, cfg.models.visuals, prompt, IconPicks)
            picks = IconPicks.model_validate_json(
                raw.strip().removeprefix("```json").removesuffix("```").strip()
            )
            found = {p.visual_id: p.icon for p in picks.picks}
            return {vid: found.get(vid) for vid in shortlists_map}
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(cfg.retry.llm_backoff_s * attempt)
    raise VisualsError(f"icon pick failed after retries: {last}")


def apply_limits(
    visuals: list[Visual], transcript: Transcript, cfg: Config
) -> list[Visual]:
    """Density + quantity caps, in source time. Order is stable."""
    kept: list[Visual] = []
    for v in sorted(visuals, key=lambda x: (x.from_word, x.id)):
        if len(kept) >= cfg.visuals.max_visuals:
            break
        start = transcript.words[v.from_word].s
        if any(
            abs(transcript.words[k.from_word].s - start) < cfg.visuals.density_window_s
            for k in kept
        ):
            continue
        kept.append(v)
    return [v for v in kept if v.to_word < len(transcript.words)]


def fetch_icon(icon_id: str, cfg: Config, cache_dir: Path) -> Path:
    prefix, _, name = icon_id.partition(":")
    if not name:
        raise VisualsError(f"malformed icon id: {icon_id}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    svg_path = cache_dir / f"{prefix}-{name}.svg"
    png_path = cache_dir / f"{prefix}-{name}-{cfg.visuals.icon_size}.png"

    if png_path.exists() and png_path.stat().st_size > 0:
        return png_path
    if not svg_path.exists():
        color = quote(cfg.visuals.icon_color, safe="")
        url = f"https://api.iconify.design/{prefix}/{name}.svg?color={color}&height={cfg.visuals.icon_size}"
        svg_path.write_bytes(_get(url, cfg).content)
    return render_svg_to_png(svg_path, png_path, cfg.visuals.icon_size)


def resolve_visuals(
    visuals: list[Visual],
    transcript: Transcript,
    cfg: Config,
    icons_dir: Path,
    attachments_dir: Path | None,
    on_error: Any = None,
) -> list[ResolvedVisual]:
    """Fetch/convert every asset. Unresolvable visuals are dropped, never fatal."""
    limited = apply_limits(visuals, transcript, cfg)
    resolved: list[ResolvedVisual] = []
    for v in limited:
        try:
            if v.kind == "icon":
                icon = v.icon or ""
                if not icon:
                    if on_error:
                        on_error(f"[visuals] {v.id}: no icon picked for '{v.keyword}'")
                    continue
                resolved.append(
                    ResolvedVisual(visual=v, asset=fetch_icon(icon, cfg, icons_dir))
                )
            else:
                path = resolve_screenshot(v.file or "", attachments_dir)
                if path is None:
                    if on_error:
                        on_error(f"[visuals] {v.id}: screenshot '{v.file}' not found")
                    continue
                resolved.append(ResolvedVisual(visual=v, asset=path))
        except Exception as exc:  # noqa: BLE001 — one bad asset must not kill the render
            if on_error:
                on_error(f"[visuals] {v.id} ({v.kind}) failed: {exc!r}")
    return resolved
