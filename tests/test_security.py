"""Hardening: rate limits, lockouts, session tokens, payload caps, headers."""

import base64
import hashlib
import hmac
import json
import time

from conftest import ADMIN, PASSWORD, add

import main

ROUTE_HEADERS = {"Origin": "https://evil.example"}


def test_blank_admin_password_disables_the_organizer_console(client, monkeypatch):
    """A `${MAYBE_UNSET_SECRET}` that expands to "" must never authenticate `""`.

    Regression test: with ADMIN_PASSWORD empty, `compare_digest("", "")` used to return
    True, so the organizer console was open to the world.
    """
    monkeypatch.setenv("ADMIN_PASSWORD", "")
    monkeypatch.delenv("SESSION_SECRET", raising=False)

    assert client.post("/api/admin/login", json={"password": ""}).status_code == 503
    assert client.get("/api/admin/state", headers={"X-Admin-Password": ""}).status_code == 503
    assert client.get("/api/admin/state", headers=ADMIN).status_code == 503   # even the right one
    assert client.get("/api/limits").json()["default_password"] is True

    # With a blank password the derived signing key is predictable (PBKDF2 over "" salted
    # with the DB path), so the whole console is shut rather than checking signatures:
    # a bearer token whose signature verifies against those public defaults still fails.
    salt = str(main.db_path()).encode()
    key = hashlib.pbkdf2_hmac("sha256", b"", salt, 50_000)
    body = base64.urlsafe_b64encode(
        json.dumps({"i": "x", "e": int(time.time()) + 300}, separators=(",", ":")).encode()
    ).decode()
    forged = f"{body}.{hmac.new(key, body.encode(), hashlib.sha256).hexdigest()}"
    assert client.get("/api/admin/state",
                      headers={"Authorization": f"Bearer {forged}"}).status_code == 503


def test_unset_admin_password_still_uses_the_flagged_default(client, monkeypatch):
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    assert main.admin_password() == main.DEFAULT_ADMIN_PASSWORD
    assert client.get("/api/limits").json()["default_password"] is True
    assert client.get("/api/admin/state", headers={"X-Admin-Password": "admin"}).status_code == 200


def test_login_rate_limit_and_lockout(client, monkeypatch):
    mod = __import__("main")
    monkeypatch.setattr(mod, "LOGIN_LIMIT", 10)
    monkeypatch.setattr(mod, "LOGIN_LOCKOUT_THRESHOLD", 3)
    monkeypatch.setattr(mod, "LOGIN_LOCKOUT_SECONDS", 120)
    mod.LIMITER.reset("login", "testclient")
    mod.LIMITER.reset("api", "testclient")

    codes = [client.post("/api/admin/login", json={"password": "nope"}).status_code for _ in range(3)]
    assert codes == [401, 401, 429]        # the third failure trips the lockout

    limited = client.post("/api/admin/login", json={"password": "nope"})
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 60

    # even the correct password is refused while the lockout is running
    blocked = client.post("/api/admin/login", json={"password": PASSWORD})
    assert blocked.status_code == 429


def test_login_lockout_is_scoped_per_client(client):
    mod = __import__("main")
    mod.LIMITER.reset("login", "10.0.0.9")
    mod.LIMITER.block("login", "10.0.0.9", 60)
    assert client.post("/api/admin/login", json={"password": PASSWORD}).status_code == 200
    mod.LIMITER.reset("login", "10.0.0.9")


def test_forged_xff_cannot_escape_the_login_block_without_trusted_proxy(client, monkeypatch):
    mod = __import__("main")
    monkeypatch.setattr(mod, "LOGIN_LIMIT", 2)
    monkeypatch.setattr(mod, "LOGIN_LOCKOUT_THRESHOLD", 0)
    mod.LIMITER.reset("login", "testclient")
    mod.LIMITER.reset("api", "testclient")

    client.post("/api/admin/login", json={"password": "a"}, headers={"X-Forwarded-For": "9.9.9.9"})
    client.post("/api/admin/login", json={"password": "b"}, headers={"X-Forwarded-For": "8.8.8.8"})
    spoofed = client.post("/api/admin/login", json={"password": PASSWORD},
                          headers={"X-Forwarded-For": "7.7.7.7"})
    assert spoofed.status_code == 429      # XFF ignored: same socket, same budget

    # behind a trusted proxy the same header is honoured, so each client keeps its own budget
    monkeypatch.setenv("TRUST_PROXY", "1")
    fresh = client.post("/api/admin/login", json={"password": PASSWORD},
                        headers={"X-Forwarded-For": "6.6.6.6"})
    assert fresh.status_code == 200


def test_draw_endpoint_has_its_own_budget(client, monkeypatch):
    mod = __import__("main")
    monkeypatch.setattr(mod, "DRAW_LIMIT", 4)
    mod.LIMITER.reset("draw", "testclient")
    person = add(client, "A", "ADULT", "Martin")

    for _ in range(4):
        assert client.get(f"/api/draw/{person['token']}").status_code == 200
    limited = client.get(f"/api/draw/{person['token']}")
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) > 0
    # the public budget cannot be drained by admin traffic
    assert client.get("/api/admin/state", headers=ADMIN).status_code == 200


def test_session_token_flow(client):
    login = client.post("/api/admin/login", json={"password": PASSWORD})
    assert login.status_code == 200
    token = login.json()["token"]
    assert login.json()["expires_in"] > 0
    assert PASSWORD not in token

    assert client.get("/api/admin/state", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.post("/api/admin/reset", headers={"Authorization": f"Bearer {token}"}).status_code == 200

    tampered = token[:-3] + ("abc" if not token.endswith("abc") else "xyz")
    assert client.get("/api/admin/state", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401
    assert client.get("/api/admin/state", headers={"Authorization": "Bearer nonsense"}).status_code == 401
    assert client.get("/api/admin/state", headers={"Authorization": f"{token}"}).status_code == 401

    assert client.post("/api/admin/logout", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert client.get("/api/admin/state", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_session_tokens_are_signed_per_secret(client, monkeypatch):
    login = client.post("/api/admin/login", json={"password": PASSWORD})
    token = login.json()["token"]

    monkeypatch.setenv("SESSION_SECRET", "a-completely-different-secret")
    assert client.get("/api/admin/state", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_expired_session_is_rejected(client, monkeypatch):
    login = client.post("/api/admin/login", json={"password": PASSWORD})
    token = login.json()["token"]
    mod = __import__("main")
    with mod._sessions_lock:
        mod._sessions[token] = int(time.time()) - 1
    assert client.get("/api/admin/state", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_password_still_works_as_admin_credential(client):
    """The spec's X-Admin-Password header keeps working unchanged."""
    assert client.get("/api/admin/state", headers=ADMIN).status_code == 200


def test_oversized_bodies_are_rejected(client, monkeypatch):
    mod = __import__("main")
    monkeypatch.setattr(mod, "MAX_BODY_BYTES", 512)
    big = {"name": "x" * 2000, "role": "ADULT", "family_group": "Martin"}
    assert client.post("/api/admin/participants", headers=ADMIN, json=big).status_code == 413
    assert client.put("/api/admin/config", headers=ADMIN,
                      json={"budget_note": "y" * 2000}).status_code == 413


def test_field_length_caps(client):
    assert client.post("/api/admin/participants", headers=ADMIN,
                       json={"name": "n" * 120, "role": "ADULT", "family_group": "Martin"}).status_code == 422
    assert client.post("/api/admin/participants", headers=ADMIN,
                       json={"name": "Ok", "role": "ADULT",
                             "family_group": "g" * 80}).status_code == 422
    assert client.post("/api/admin/participants", headers=ADMIN,
                       json={"name": "Ok", "role": "ADULT", "family_group": "Martin",
                             "wishlist": "w" * 1200}).status_code == 422
    assert client.put("/api/admin/config", headers=ADMIN,
                      json={"event_title": "t" * 200}).status_code == 422
    assert client.put("/api/admin/config", headers=ADMIN,
                      json={"budget_note": "b" * 600}).status_code == 422
    token = add(client, "Ok", "ADULT", "Martin")["token"]
    assert client.put(f"/api/draw/{token}/wishlist", json={"wishlist": "w" * 1200}).status_code == 422


def test_security_headers_present(client):
    for path in ("/", "/api/health", "/api/admin/state"):
        response = client.get(path, headers=ADMIN)
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        csp = response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in csp
        assert "base-uri 'self'" in csp
        assert "https://cdn.jsdelivr.net" in csp


def test_unauth_responses_do_not_leak_internals(client):
    response = client.get("/api/admin/state", headers={"X-Admin-Password": "wrong"})
    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
    assert client.get("/api/draw/nope").json() == {"detail": "Invalid link"}


def test_redraw_on_locked_event_changes_nothing(client, db):
    from conftest import set_config

    set_config(client, adult_draws_adult=1, adult_draws_child=0,
               child_draws_adult=0, child_draws_child=0)
    add(client, "Solo", "ADULT", "Martin")
    add(client, "Other", "ADULT", "Dubois")
    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 200
    conn = db()
    before = sorted((row["giver_id"], row["receiver_id"])
                    for row in conn.execute("SELECT giver_id, receiver_id FROM assignments"))
    conn.close()

    assert client.post("/api/admin/draw", headers=ADMIN).status_code == 409

    conn = db()
    after = sorted((row["giver_id"], row["receiver_id"])
                   for row in conn.execute("SELECT giver_id, receiver_id FROM assignments"))
    conn.close()
    assert before == after and len(before) == 2


def test_participant_tokens_never_reach_the_log(client, monkeypatch):
    """`GET /api/draw/<uuid>` is a route whose path *is* the credential.

    The container log on a deployed host is the journal: readable by more people and sometimes by
    more systems than the database directory. A line per page load that repeats the link would put
    every family member's whole credential there.
    """
    from conftest import set_config

    set_config(client, adult_draws_adult=1, adult_draws_child=0,
               child_draws_adult=0, child_draws_child=0)
    add(client, "Mario", "ADULT", "Tremblay")
    add(client, "Lise", "ADULT", "Gagnon")
    token = next(p["token"] for p in client.get("/api/admin/state", headers=ADMIN)
                 .json()["participants"] if p["name"] == "Mario")

    lines = []
    monkeypatch.setattr(main.LOGGER, "info", lambda message, *args: lines.append(message % args))

    assert client.get(f"/api/draw/{token}").status_code == 200
    assert client.put(f"/api/draw/{token}/wishlist", json={"wishlist": "des chaussettes"}).status_code == 200
    client.get("/api/admin/state", headers=ADMIN)

    joined = "\n".join(lines)
    assert token not in joined, "a participant link was written to the log"
    assert "GET path=/api/draw/<token> status=200" in joined
    assert "PUT path=/api/draw/<token>/wishlist status=200" in joined
    # the routes worth debugging are still logged in full
    assert "path=/api/admin/state" in joined


def test_redaction_only_replaces_token_shaped_segments():
    """A `<token>` placeholder everywhere would hide nothing but also tell you nothing."""
    from fastapi import Request

    def fake(path):
        scope = {"type": "http", "path": path, "headers": [], "method": "GET",
                 "query_string": b"", "root_path": "", "scheme": "http", "server": ("t", 80),
                 "client": ("1.2.3.4", 1)}
        return main.redacted_path(Request(scope))

    assert fake("/api/draw/6f1d2c3a-0b0b-4c1a-9d2e-7f8a9b0c1d2e") == "/api/draw/<token>"
    assert fake("/api/draw/6F1D2C3A-0B0B-4C1A-9D2E-7F8A9B0C1D2E/wishlist") == "/api/draw/<token>/wishlist"
    assert fake("/api/admin/participants/12") == "/api/admin/participants/12"
    assert fake("/api/health") == "/api/health"
    # a name that happens to look partly like a uuid stays visible
    assert fake("/api/draw/not-a-uuid") == "/api/draw/not-a-uuid"
