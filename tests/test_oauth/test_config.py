"""Tests for OAuth configuration resolution."""

import pytest

from tp_mcp.oauth import config as cfg


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in (
        "TP_MCP_PUBLIC_URL",
        "TP_MCP_TOKEN_SECRET",
        "TP_MCP_AUTH_ENABLED",
        "TP_AUTH_COOKIE",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


def test_disabled_by_default():
    assert cfg.auth_enabled() is False


def test_enabled_when_public_url_and_no_single_user_cookie(monkeypatch):
    monkeypatch.setenv("TP_MCP_PUBLIC_URL", "https://x.example")
    assert cfg.auth_enabled() is True


def test_single_user_cookie_keeps_auth_off(monkeypatch):
    monkeypatch.setenv("TP_MCP_PUBLIC_URL", "https://x.example")
    monkeypatch.setenv("TP_AUTH_COOKIE", "cookie")
    assert cfg.auth_enabled() is False


def test_explicit_enable_overrides(monkeypatch):
    monkeypatch.setenv("TP_MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("TP_MCP_PUBLIC_URL", "https://x.example")
    monkeypatch.setenv("TP_MCP_TOKEN_SECRET", "s")
    monkeypatch.setenv("TP_AUTH_COOKIE", "cookie")
    assert cfg.auth_enabled() is True


def test_load_config_requires_secret(monkeypatch):
    monkeypatch.setenv("TP_MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("TP_MCP_PUBLIC_URL", "https://x.example")
    with pytest.raises(ValueError, match="TP_MCP_TOKEN_SECRET"):
        cfg.load_config()


def test_load_config_requires_https(monkeypatch):
    monkeypatch.setenv("TP_MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("TP_MCP_PUBLIC_URL", "http://insecure.example")
    monkeypatch.setenv("TP_MCP_TOKEN_SECRET", "s")
    with pytest.raises(ValueError, match="https"):
        cfg.load_config()


def test_load_config_allows_localhost_http(monkeypatch):
    monkeypatch.setenv("TP_MCP_AUTH_ENABLED", "true")
    monkeypatch.setenv("TP_MCP_PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setenv("TP_MCP_TOKEN_SECRET", "s")
    c = cfg.load_config()
    assert c.enabled and c.public_url == "http://localhost:8000"
