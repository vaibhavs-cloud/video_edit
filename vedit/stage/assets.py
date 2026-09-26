"""Asset preparation: SVG -> PNG (the one non-trivial dependency) and screenshot resolution."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class AssetError(RuntimeError):
    pass


def render_svg_to_png(svg_path: Path, png_path: Path, height: int) -> Path:
    """Primary: rsvg-convert (CI, no Python C deps). Fallback: svglib renderPM."""
    png_path.parent.mkdir(parents=True, exist_ok=True)

    rsvg = shutil.which("rsvg-convert")
    if rsvg:
        proc = subprocess.run(
            [rsvg, "-h", str(height), "-o", str(png_path), str(svg_path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode == 0 and png_path.exists() and png_path.stat().st_size > 0:
            return png_path
        raise AssetError(f"rsvg-convert failed: {proc.stderr.strip()}")

    try:
        from reportlab.graphics import renderPM
        from svglib.svglib import svg2rlg
    except ImportError as exc:  # pragma: no cover
        raise AssetError(
            "no SVG renderer: install librsvg2-bin or `pip install svglib[bitmaps]`"
        ) from exc

    drawing = svg2rlg(str(svg_path))
    if drawing is None:
        raise AssetError(f"svglib could not parse {svg_path}")
    scale = height / max(drawing.height, 1.0)
    drawing.scale(scale, scale)
    drawing.width *= scale
    drawing.height *= scale
    renderPM.drawToFile(drawing, str(png_path), fmt="PNG")
    if not png_path.exists() or png_path.stat().st_size == 0:
        raise AssetError(f"renderPM produced no output for {svg_path}")
    return png_path


def resolve_screenshot(filename: str, attachments_dir: Path | None) -> Path | None:
    if not attachments_dir:
        return None
    direct = attachments_dir / filename
    if direct.exists():
        return direct
    matches = [
        p
        for p in attachments_dir.iterdir()
        if p.name == filename or p.stem == Path(filename).stem
    ]
    return matches[0] if matches else None
