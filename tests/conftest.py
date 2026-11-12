"""Shared fixtures: a TestClient over an isolated SQLite file."""

import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import main  # noqa: E402

ADMIN = {"X-Admin-Password": "s3cret"}
PASSWORD = "s3cret"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "exchange.db"))
    monkeypatch.setenv("ADMIN_PASSWORD", PASSWORD)
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


def add(client, name, role, family_group, wishlist=""):
    response = client.post(
        "/api/admin/participants",
        headers=ADMIN,
        json={"name": name, "role": role, "family_group": family_group, "wishlist": wishlist},
    )
    assert response.status_code == 201, response.text
    return response.json()["participant"]


def set_config(client, **values):
    response = client.put("/api/admin/config", headers=ADMIN, json=values)
    assert response.status_code == 200, response.text
    return response.json()["config"]
