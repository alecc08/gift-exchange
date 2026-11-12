"""Turn the Playwright recordings + PNG stills into the README media.

    python tools/build_docs_media.py

Needs ffmpeg and Pillow. Reads docs/media/manifest.json (written by
tools/capture_docs_media.py), writes two GIFs into docs/media/, then downscales the
PNG stills to README-sized JPEGs and deletes the PNGs so the repository stays small.
"""

import json
import pathlib
import shutil
import subprocess
import sys

from PIL import Image

REPO = pathlib.Path(__file__).resolve().parents[1]
SHOTS = REPO / "docs" / "screenshots"
MEDIA = REPO / "docs" / "media"
MANIFEST = MEDIA / "manifest.json"

CLIPS = {
    # gif name: (manifest entry, seconds before the action to start, seconds to keep, width, fps)
    "reveal.gif": ("reveal", 0.35, 3.4, 480, 10),
    "organizer-flow.gif": ("flow", 0.0, 5.6, 620, 10),
}
MAX_WIDTH = 1400
MOBILE_MAX_WIDTH = 620


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


def build_gifs(manifest):
    for name, (entry, lead, duration, width, fps) in CLIPS.items():
        clip = manifest[entry]
        start = clip.get("start_at", clip.get("click_at", 0) - lead)
        start = max(0.0, start)
        filter_chain = (
            f"fps={fps},scale={width}:-1:flags=lanczos,split[a][b];"
            f"[a]palettegen=max_colors=140:stats_mode=full[p];"
            f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
        )
        ffmpeg("-ss", f"{start:.2f}", "-t", str(duration), "-i", clip["video"],
               "-vf", filter_chain, "-gifflags", "+transdiff", str(MEDIA / name))
        print(f"  gif: {name} {size_kb(MEDIA / name)}")


def size_kb(path):
    return f"{path.stat().st_size // 1024} KB"


def shrink_stills():
    for path in sorted(SHOTS.glob("*.png")):
        widest = MOBILE_MAX_WIDTH if "mobile" in path.name else MAX_WIDTH
        with Image.open(path) as image:
            image = image.convert("RGB")
            if image.width > widest:
                height = round(image.height * widest / image.width)
                image = image.resize((widest, height), Image.LANCZOS)
            out = path.with_suffix(".jpg")
            image.save(out, "JPEG", quality=84, optimize=True, progressive=True)
        path.unlink()
        print(f"  still: {out.name} {size_kb(out)}")


def main() -> int:
    if shutil.which("ffmpeg") is None:
        print("ffmpeg is required to build the GIFs", file=sys.stderr)
        return 1
    if not MANIFEST.exists():
        print(f"run tools/capture_docs_media.py first (missing {MANIFEST})", file=sys.stderr)
        return 1
    build_gifs(json.loads(MANIFEST.read_text(encoding="utf-8")))
    shrink_stills()
    total = sum(p.stat().st_size for p in list(SHOTS.glob("*")) + list(MEDIA.glob("*")))
    print(f"docs media total: {total // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
