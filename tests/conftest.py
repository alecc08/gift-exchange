"""Shared fixtures: a TestClient over an isolated SQLite file."""

import os
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Rate limiting is switched off for the functional suite (tests/test_security.py
# exercises it with its own values). Must be set before main is imported.
os.environ["LOGIN_RATE_LIMIT"] = "100000"
os.environ["API_RATE_LIMIT"] = "100000"
os.environ["DRAW_RATE_LIMIT"] = "100000"
os.environ["LOGIN_LOCKOUT_THRESHOLD"] = "0"
# Email sending stays unconfigured for the whole suite unless a test sets the key itself:
# fail-closed behaviour is the default state under test, not an accident of the dev shell.
os.environ.pop("RESEND_API_KEY", None)
os.environ.pop("RESEND_FROM", None)
# The throttle defaults pace sends for real mailboxes; the suite runs them at zero unless a
# throttle test raises them on purpose.
os.environ["EMAIL_SEND_INTERVAL_MS"] = "0"
os.environ["EMAIL_SAME_ADDRESS_GAP_S"] = "0"
os.environ["EMAIL_RETRY_AFTER_S"] = "0"

import main  # noqa: E402

ADMIN = {"X-Admin-Password": "s3cret"}
PASSWORD = "s3cret"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "exchange.db"))
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
    main.LIMITER.reset("login", "testclient")
    main.LIMITER.reset("api", "testclient")
    main.LIMITER.reset("draw", "testclient")
    main.init_db()
    with TestClient(main.app) as test_client:
        yield test_client


@pytest.fixture
def db(tmp_path):
    def open_db():
        conn = sqlite3.connect(tmp_path / "exchange.db")
        conn.row_factory = sqlite3.Row
        return conn

    return open_db


def add(client, name, role, family_group, wishlist="", group=None, email="", draw_id=None):
    """Create a participant.

    `family_group` is the display surname; `group` is the household the same-group rule works on, and
    it defaults to the surname because the older tests were written before the two were separated.
    Pass `group=""` for somebody in no group at all. `email` is optional: plenty of relatives still
    get their link handed to them in person. `draw_id` is omitted by the older tests, which is fine
    while one draw exists — that is exactly the contract they are written against.
    """
    response = client.post(
        "/api/admin/participants",
        headers=ADMIN,
        params={"draw_id": draw_id} if draw_id is not None else None,
        json={
            "name": name,
            "role": role,
            "family_group": family_group,
            "group_key": family_group if group is None else group,
            "wishlist": wishlist,
            "email": email,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["participant"]


def set_config(client, draw_id=None, **values):
    params = {"draw_id": draw_id} if draw_id is not None else None
    response = client.put("/api/admin/config", headers=ADMIN, params=params, json=values)
    assert response.status_code == 200, response.text
    return response.json()["config"]
