"""Rasterize the email art to PNG.

    python tools/build_email_art.py            # static/email-*.svg -> static/email-*.png

The mail references its art over https, so the file has to be something the mail clients can
fetch *and* display. Measured in a real Gmail inbox (2026-11-13): the SVGs served fine — 200,
`image/svg+xml`, correct bytes — and Gmail rendered nothing, because **Gmail's image proxy does
not serve SVG** (Google confirmed it has no plans to; canicemail lists SVG as "partially
supported, non-Google only"). A PNG is the boring answer that every client draws.

The SVGs stay the source of truth: they are what gets edited, they are what the site's own art
matches, and they stay vector for the browser. Only the copies the mail loads are rasterized, at
**2x the size the mail displays them**, so the art is sharp on a phone and the img's `width`/
`height` attributes match the intrinsic pixels (no resampling by the mail client).

Requires Playwright (`uv pip install playwright && playwright install chromium`); like the other
tools in this directory it is not an app dependency.
"""

import base64
import pathlib
import struct
import sys

from playwright.sync_api import sync_playwright

STATIC = pathlib.Path(__file__).resolve().parents[1] / "static"

# name, displayed size in the mail (width, height) — see `_email_assets()` in main.py. The PNG is
# written at 2x, which is the resolution a phone actually shows it at.
ASSETS = [
    ("email-flake", 56, 56),
    ("email-gift", 330, 190),
    ("email-gift-strip", 504, 36),
]
SCALE = 2


def png_size(path: pathlib.Path) -> tuple[int, int]:
    """(width, height) from the PNG header — no image library needed for eight bytes."""
    with path.open("rb") as handle:
        header = handle.read(24)
    assert header[:8] == b"\x89PNG\r\n\x1a\n", f"{path.name} is not a PNG"
    width, height = struct.unpack(">II", header[16:24])
    return width, height


def render(page, svg_path: pathlib.Path, width: int, height: int, out_path: pathlib.Path) -> None:
    """One asset at width x height CSS pixels; the device scale factor does the 2x.

    Chromium screenshots *device* pixels, so the viewport and the `<img>` are set at the size the
    mail displays (the CSS size) and `device_scale_factor=2` in `main()` is what makes the PNG come
    out twice that.
    """
    payload = base64.b64encode(svg_path.read_bytes()).decode()
    page.set_viewport_size({"width": width, "height": height})
    page.set_content(
        f'<meta charset="utf-8"><style>html,body{{margin:0;padding:0;background:transparent}}'
        f'img{{display:block;width:{width}px;height:{height}px}}</style>'
        f'<img src="data:image/svg+xml;base64,{payload}" width="{width}" height="{height}">'
    )
    page.wait_for_timeout(120)  # let the rasterizer settle; the art has no animation to wait on
    page.screenshot(path=str(out_path), omit_background=True)


def main() -> int:
    missing = [name for name, _, _ in ASSETS if not (STATIC / f"{name}.svg").exists()]
    if missing:
        print("missing source art:", ", ".join(missing), file=sys.stderr)
        return 1

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page(device_scale_factor=SCALE)
        for name, shown_width, shown_height in ASSETS:
            out = STATIC / f"{name}.png"
            render(page, STATIC / f"{name}.svg", shown_width, shown_height, out)
            wanted = (shown_width * SCALE, shown_height * SCALE)
            actual = png_size(out)
            assert actual == wanted, f"{name}.png came out {actual}, wanted {wanted}"
            print(f"  {name}.png  {actual[0]}x{actual[1]}  {out.stat().st_size // 1024} KiB "
                  f"(mail shows {shown_width}x{shown_height})")
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
