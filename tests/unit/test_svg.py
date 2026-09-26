from __future__ import annotations

from vedit.stage.assets import render_svg_to_png

SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
    '<rect x="10" y="10" width="100" height="100" rx="12" fill="#ffffff"/>'
    '<circle cx="60" cy="60" r="30" fill="#000000"/></svg>'
)


def test_svg_renders_to_png(tmp_path):
    svg = tmp_path / "icon.svg"
    png = tmp_path / "icon.png"
    svg.write_text(SVG, encoding="utf-8")
    out = render_svg_to_png(svg, png, 200)
    assert out.exists()
    assert out.stat().st_size > 100

    from PIL import Image

    with Image.open(out) as img:
        assert img.size[1] == 200
        assert img.mode in ("RGBA", "RGB")
