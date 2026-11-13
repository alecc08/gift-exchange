"""Guards over the README gallery, which is generated and read by humans and by nobody in the app.

That separation is how it went stale once: the capture tool was shooting two new screens, and the
repository had neither the JPEGs nor the README rows for them. Nothing in the test suite, the app or
CI could notice, and the gallery quietly stopped describing the product.
"""

import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]
README = REPO / "README.md"
DOCS = REPO / "docs"
SHOTS = DOCS / "screenshots"
MEDIA = DOCS / "media"
# Both ways the README embeds a file: `<img src="…">` in the gallery tables and `![…](…)` up top.
SHOWN_RE = re.compile(r"docs/(?:screenshots|media)/[\w.-]+?\.(?:jpg|png|gif)")


def gallery_references() -> set[str]:
    return set(SHOWN_RE.findall(README.read_text(encoding="utf-8")))


def published_files() -> set[str]:
    stills = {f"docs/screenshots/{p.name}" for p in SHOTS.glob("*.jpg")}
    gifs = {f"docs/media/{p.name}" for p in MEDIA.glob("*.gif")}
    return stills | gifs


def test_the_gallery_points_at_files_that_exist():
    missing = sorted(gallery_references() - published_files())
    assert not missing, f"the README shows images that are not in the repository: {missing}"


def test_no_still_is_left_out_of_the_gallery():
    """A still nobody references is a screen the README does not know about — the stale-gallery bug,
    seen from the other side."""
    unseen = sorted(published_files() - gallery_references())
    assert not unseen, f"images in docs/ that no README row shows: {unseen}"


def test_only_the_compressed_stills_are_committed():
    """tools/build_docs_media.py turns the PNG captures into README-sized JPEGs and deletes them.

    A PNG left behind is either the tool having crashed halfway or someone committing a capture by
    hand, and either way the repository pays for it: a capture is 2x scale and a tenth of the README
    page again.
    """
    strays = sorted(p.name for p in SHOTS.glob("*.png"))
    assert not strays, f"raw captures committed instead of JPEGs: {strays}"
