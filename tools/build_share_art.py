#!/usr/bin/env python3
"""Render the share card and the home-screen icons from the HTML sources in tools/.

    python3 tools/build_share_art.py            # writes static/og-image.png, icon-512.png,
                                               #         apple-touch-icon.png

These three files are the only binary art in the repo, and they are generated, never hand-edited:
the sources are `tools/share-card.html` and `tools/app-icon.html`, which use the same palette, the
same snowflake geometry and the same Google Fonts as the app itself, so the preview a crawler
renders matches what the family sees when they open the link.

Chromium is NOT an app dependency — nothing at runtime needs it. If it is missing, install
Playwright's browser (`uv pip install playwright && playwright install chromium`) or point
CHROME_BIN at any Chrome/Chromium.

Why 1200x630: it is the size every crawler expects for `og:image` / `twitter:card`
summary_large_image, and the wide card is what Facebook, WhatsApp, Messenger, iMessage, Discord,
Slack and Telegram crop from.
"""

from __future__ import annotations

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "static"
TOOLS = ROOT / "tools"

# (html source, output, width, height) — plus the downscales derived from the 512 render.
JOBS = [
    ("share-card.html", "og-image.png", 1200, 630),
    ("app-icon.html", "icon-512.png", 512, 512),
]
DERIVED = [("icon-512.png", "apple-touch-icon.png", 180)]


def find_chromium() -> str:
    """$CHROME_BIN, then the usual system names, then a Playwright-managed Chromium."""
    candidates = [os.environ.get("CHROME_BIN", "")]
    candidates += [shutil.which(n) or "" for n in ("chromium", "chromium-browser", "google-chrome", "chrome")]
    cache = Path.home() / ".cache" / "ms-playwright"
    candidates += sorted(glob.glob(str(cache / "chromium-*" / "chrome-linux*" / "chrome")), reverse=True)
    candidates += sorted(glob.glob(str(cache / "chromium_headless_shell-*" / "*" / "headless_shell")), reverse=True)
    for path in candidates:
        if path and Path(path).is_file() and os.access(path, os.X_OK):
            return path
    sys.exit(
        "no Chromium found. Install one, or set CHROME_BIN=/path/to/chrome.\n"
        "  uv pip install playwright && playwright install chromium"
    )


def render(chrome: str, source: Path, out: Path, width: int, height: int) -> None:
    """One Chromium invocation per asset. --virtual-time-budget is what waits for the web fonts:
    without it the screenshot races the font download and the card comes back in Georgia."""
    with tempfile.TemporaryDirectory() as profile:
        cmd = [
            chrome, "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
            "--force-color-profile=srgb", "--font-render-hinting=none",
            f"--user-data-dir={profile}", f"--window-size={width},{height}",
            "--virtual-time-budget=15000", "--run-all-compositor-stages-before-draw",
            f"--screenshot={out}", source.as_uri(),
        ]
        done = subprocess.run(cmd, capture_output=True, text=True)
    if not out.is_file():
        sys.exit(f"chromium produced no screenshot for {source.name}:\n{done.stderr.strip()[:800]}")


def check_card(path: Path, band: int, allow_wide: bool) -> None:
    """Fail the build if the headline runs outside the square mobile clients crop to.

    Facebook, WhatsApp and iMessage show the 1.91:1 card in a feed but crop the centre square for
    link previews on phones, so the one design rule that is easy to break by editing type sizes is
    "every headline line must live inside the middle `band` px". The headline is the only near-white
    ivory in the card (#faf7f2), so thresholding at 240 isolates it from the gold frame, the snow
    dots and the pine-100 body copy. Runs of ivory on each row are then merged into components, and
    the tall components (>= half the tallest) are the headline lines: a whole line, not one glyph's
    ascender, which is what a naive widest-row measure gets wrong.
    """
    try:
        from PIL import Image
    except ImportError:
        print("  (Pillow missing: skipping the crop-safe check)")
        return

    with Image.open(path) as im:
        width, height = im.size
        pixels = im.convert("L").point(lambda v: 1 if v >= 240 else 0).tobytes()

    runs: list[tuple[int, int, int]] = []          # (row, x_start, x_end_inclusive)
    for y in range(height):
        row = pixels[y * width:(y + 1) * width]
        x = 0
        while x < width:
            if row[x]:
                start = x
                while x < width and row[x]:
                    x += 1
                runs.append((y, start, x - 1))
            else:
                x += 1

    # Segmentation is by rows, not glyphs: centred text lines are separated vertically, while their
    # letters are separated horizontally (and letters of one word at this size have gaps wider than
    # any line gap, which is what a glyph-level merge keeps getting wrong).
    occupied = sorted({y for y, _, _ in runs})
    bands: list[list[tuple[int, int, int]]] = []
    for y in occupied:
        if bands and y - bands[-1][-1][0] <= 3:
            bands[-1].extend(r for r in runs if r[0] == y)
        else:
            bands.append([r for r in runs if r[0] == y])

    boxes = []
    for group in bands:
        ys = [g[0] for g in group]
        boxes.append((min(g[1] for g in group), max(g[2] for g in group), min(ys), max(ys)))

    if not boxes:
        print("  (no ivory headline found in the card — check that the fonts loaded)")
        return

    tallest = max(top - bottom + 1 for _, _, bottom, top in boxes)
    headline = [c for c in boxes if c[3] - c[2] + 1 >= tallest * 0.6]
    for left, right, bottom, top in sorted(boxes, key=lambda c: c[2])[:8]:
        tag = "headline" if (left, right, bottom, top) in headline else "text"
        print(f"    {tag:>8}: x={left:>4}..{right:<4} y={bottom:>3}..{top:<3} "
              f"{right - left + 1:>4} x {top - bottom + 1:>3} px")

    widest = max(headline, key=lambda c: c[1] - c[0])
    left, right, bottom, top = widest
    span = right - left + 1
    low, high = (width - band) // 2, (width + band) // 2

    print(f"  crop-safe: {len(headline)} headline line(s); widest {span} px at x={left}..{right} "
          f"(mobile crops the middle {band} px: x={low}..{high})")
    if (left < low or right > high) and not allow_wide:
        sys.exit(
            f"  a headline line runs outside the centre {band} px band, so a phone preview would cut "
            f"it mid-word.\n  Shrink or wrap h1 in {TOOLS / 'share-card.html'} "
            f"(--allow-wide-title to ship anyway)."
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--width", type=int, help="override the card width (default 1200)")
    parser.add_argument("--allow-wide-title", action="store_true",
                        help="skip the crop-safe failure (the measurement is still printed)")
    args = parser.parse_args()

    chrome = find_chromium()
    print(f"chromium: {chrome}")
    STATIC.mkdir(exist_ok=True)

    for name, out_name, width, height in JOBS:
        if args.width and out_name == "og-image.png":
            width, height = args.width, round(args.width * 630 / 1200)
        render(chrome, TOOLS / name, STATIC / out_name, width, height)
        print(f"  {out_name}: {width}x{height}  {(STATIC / out_name).stat().st_size // 1024} KB")
        if out_name == "og-image.png":
            check_card(STATIC / out_name, height, args.allow_wide_title)

    for src_name, out_name, size in DERIVED:
        try:
            from PIL import Image
        except ImportError:
            sys.exit("Pillow is needed to derive %s (pip install pillow)" % out_name)
        with Image.open(STATIC / src_name) as im:
            im.convert("RGB").resize((size, size), Image.LANCZOS).save(STATIC / out_name, optimize=True)
        print(f"  {out_name}: {size}x{size}  {(STATIC / out_name).stat().st_size // 1024} KB (from {src_name})")


if __name__ == "__main__":
    main()
