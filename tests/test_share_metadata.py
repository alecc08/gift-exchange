"""Link previews: the share card, the icons, and the rule that previews leak nothing."""

import re

from conftest import add, png_size


def meta_block(body: str) -> str:
    """Just the injected <meta og:*/twitter:*> lines, so tests can assert on the crawlable part."""
    return "\n".join(line for line in body.splitlines()
                     if '<meta property="og:' in line or '<meta name="twitter:' in line)


def test_share_meta_is_injected_with_absolute_urls(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

    block = meta_block(response.text)
    assert block, "the share meta block is missing from the shell"
    # Crawlers need absolute URLs; relative og:image is the classic silently-blank preview.
    assert '<meta property="og:image" content="http://testserver/static/og-image.png">' in block
    assert '<meta property="og:title" content="Échange de cadeaux · Père Noël secret">' in block
    assert 'Personne sait qui pige qui' in block
    assert '<meta name="twitter:card" content="summary_large_image">' in block
    assert '<meta property="og:locale" content="fr_CA">' in block


def test_marker_is_replaced_not_left_in_the_page(client):
    body = client.get("/").text
    assert "<!--share-meta-->" not in body
    assert "share-meta" not in body


def test_share_meta_follows_the_host_and_forwarded_proto(client, monkeypatch):
    """The host the family was given is whatever the request says it is — the public domain, a
    LAN override, or an IP:port — and the scheme only comes from the proxy that terminated TLS."""
    monkeypatch.setenv("TRUST_PROXY", "1")
    response = client.get("/", headers={
        "Host": "tirage-noel.alserv.org",
        "X-Forwarded-Proto": "https",
    })
    block = meta_block(response.text)
    assert '<meta property="og:image" content="https://tirage-noel.alserv.org/static/og-image.png">' in block
    assert '<meta property="og:url" content="https://tirage-noel.alserv.org/">' in block

    # without TRUST_PROXY the proxy header is ignored: a directly exposed app must not claim https
    monkeypatch.setenv("TRUST_PROXY", "0")
    block = meta_block(client.get("/", headers={"X-Forwarded-Proto": "https"}).text)
    assert '<meta property="og:image" content="http://testserver/static/og-image.png">' in block


def test_hostile_host_header_loses_the_url_tags_not_the_page(client):
    """A garbage Host must not become the preview image's host. There is nothing "clean" to fall
    back to (Starlette builds request.url from the same header), so the URL tags are dropped and
    the rest of the card survives."""
    response = client.get("/", headers={"Host": 'evil";x="1'})
    assert response.status_code == 200

    block = meta_block(response.text)
    assert "evil" not in block
    assert "og:image" not in block and "og:url" not in block
    assert '<meta property="og:title" content="Échange de cadeaux · Père Noël secret">' in block
    assert 'Personne sait qui pige qui' in block


def test_private_link_gets_the_same_generic_card(client):
    """A participant link is what actually gets shared, and a crawler reads the same page."""
    participant = add(client, "Julie", "ADULT", "Tremblay")
    token = participant["token"]

    anonymous = meta_block(client.get("/").text)
    private = meta_block(client.get(f"/?t={token}").text)

    assert private == anonymous, "the preview for a private link must be the generic card"
    assert token not in private
    assert "Julie" not in private
    assert "Tremblay" not in private


def test_share_art_is_served_and_declared_honestly(client):
    response = client.get("/static/og-image.png")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"

    payload = response.content
    assert png_size(payload) == (1200, 630), "og:image must be the size the tags declare"
    # Every major crawler accepts this size; a megabyte is the practical ceiling for the card.
    assert 10_000 < len(payload) < 1_000_000


def test_icons_and_manifest_are_served_with_their_types(client):
    for path, content_type in (
        ("/static/favicon.svg", "image/svg+xml"),
        ("/static/site.webmanifest", "application/manifest+json"),
        ("/static/apple-touch-icon.png", "image/png"),
        ("/static/icon-512.png", "image/png"),
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"] == content_type, path

    assert png_size(client.get("/static/apple-touch-icon.png").content) == (180, 180)
    assert png_size(client.get("/static/icon-512.png").content) == (512, 512)

    manifest = client.get("/static/site.webmanifest").json()
    assert manifest["start_url"] == "/"
    assert manifest["name"] == "Échange de cadeaux"
    assert manifest["lang"] == "fr-CA"


def test_shell_is_no_cache_but_static_assets_get_their_conditional_headers(client):
    """The shell differs per host, so it must not be cached; the card does not, so it keeps
    StaticFiles' ETag/Last-Modified instead — an explicit Cache-Control there would override the
    revalidation window the browser already has for a byte-identical file."""
    shell = client.get("/")
    assert shell.headers["cache-control"] == "no-cache"
    assert "etag" not in shell.headers  # no-cache means every load revalidates

    art = client.get("/static/og-image.png")
    assert "etag" in art.headers and "last-modified" in art.headers
    assert "cache-control" not in art.headers

    again = client.get("/static/og-image.png", headers={"If-None-Match": art.headers["etag"]})
    assert again.status_code == 304


def test_share_image_is_reachable_without_auth(client):
    """Preview crawlers are anonymous: an auth gate on the image is a blank card."""
    response = client.get("/static/og-image.png", headers={"Origin": "https://example.com"})
    assert response.status_code == 200
