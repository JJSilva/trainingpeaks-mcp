"""Tests for the per-user (multi-tenant) TrainingPeaks cookie store."""

import pytest

from tp_mcp.auth import multiuser_store as store


@pytest.fixture(autouse=True)
def _temp_db(tmp_path, monkeypatch):
    """Point the store at a throwaway database per test."""
    monkeypatch.setenv("TP_MCP_DB_PATH", str(tmp_path / "store.db"))
    monkeypatch.setattr(store, "_initialized", False)
    yield


def test_store_and_get_roundtrip():
    r = store.store_user_cookie("subj-1", "cookie-value", tp_email="A@B.com", tp_athlete_id=42)
    assert r.success
    got = store.get_user_cookie("subj-1")
    assert got.success
    assert got.cookie == "cookie-value"


def test_cookie_is_encrypted_at_rest(tmp_path):
    store.store_user_cookie("subj-2", "plaintextcookie")
    # The raw DB bytes must not contain the plaintext cookie.
    from tp_mcp.auth.db import get_db_path

    raw = get_db_path().read_bytes()
    assert b"plaintextcookie" not in raw


def test_get_missing_subject():
    assert store.get_user_cookie("nope").success is False


def test_clear_cookie():
    store.store_user_cookie("subj-3", "c")
    assert store.get_user_cookie("subj-3").success
    store.clear_user_cookie("subj-3")
    assert store.get_user_cookie("subj-3").success is False


def test_upsert_updates_cookie_and_meta():
    store.store_user_cookie("subj-4", "old", tp_email="x@y.com", tp_athlete_id=1)
    store.store_user_cookie("subj-4", "new", tp_athlete_id=2)
    assert store.get_user_cookie("subj-4").cookie == "new"
    meta = store.get_user_meta("subj-4")
    assert meta["tp_athlete_id"] == 2
    assert meta["tp_email"] == "x@y.com"  # preserved via COALESCE


def test_empty_cookie_rejected():
    assert store.store_user_cookie("subj-5", "  ").success is False


def test_get_meta_missing():
    assert store.get_user_meta("ghost") is None
