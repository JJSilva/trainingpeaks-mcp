"""End-to-end OAuth flow tests for the hosted HTTP transport.

Exercises register -> authorize -> TrainingPeaks login -> code -> token ->
authenticated /mcp, plus PKCE enforcement, refresh rotation, and per-subject
cookie binding. TrainingPeaks validation is stubbed so no real account is needed.
"""

import base64
import hashlib
import os
from urllib.parse import parse_qs, urlparse

import pytest
from starlette.testclient import TestClient

from tp_mcp.auth.validator import AuthResult, AuthStatus

REDIRECT_URI = "http://localhost:9999/cb"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("TP_MCP_PUBLIC_URL", "http://localhost:8000")
    monkeypatch.setenv("TP_MCP_TOKEN_SECRET", "test-secret")
    monkeypatch.setenv("TP_MCP_DB_PATH", str(tmp_path / "oauth.db"))
    monkeypatch.setenv("TP_MCP_ENC_KEY", base64.b64encode(os.urandom(32)).decode())

    # Reset cached store init flags so tables land in the temp DB.
    import tp_mcp.auth.multiuser_store as mstore
    import tp_mcp.oauth.store as ostore

    monkeypatch.setattr(mstore, "_initialized", False)
    monkeypatch.setattr(ostore, "_initialized", False)

    # Stub TrainingPeaks cookie validation.
    import tp_mcp.oauth.login as login

    async def fake_validate(cookie):
        return AuthResult(status=AuthStatus.VALID, athlete_id=555, user_id=999, email="rider@example.com")

    monkeypatch.setattr(login, "validate_auth", fake_validate)

    from tp_mcp.http_server import _build_app

    with TestClient(_build_app()) as c:
        yield c


def _pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _register(c):
    r = c.post(
        "/register",
        json={
            "redirect_uris": [REDIRECT_URI],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        },
    )
    assert r.status_code == 201
    return r.json()["client_id"]


def _get_code(c, client_id, challenge, cookie="FAKE"):
    auth = c.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT_URI,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": "st",
        },
        follow_redirects=False,
    )
    assert auth.status_code == 302
    login_session = parse_qs(urlparse(auth.headers["location"]).query)["login_session"][0]
    sub = c.post("/tp-login", data={"login_session": login_session, "cookie": cookie}, follow_redirects=False)
    assert sub.status_code == 302
    loc = urlparse(sub.headers["location"])
    assert f"{loc.scheme}://{loc.netloc}{loc.path}" == REDIRECT_URI
    return parse_qs(loc.query)["code"][0]


def test_metadata_endpoints(client):
    assert client.get("/.well-known/oauth-authorization-server").status_code == 200
    assert client.get("/.well-known/oauth-protected-resource").status_code == 200


def test_mcp_requires_token(client):
    r = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_full_flow(client):
    client_id = _register(client)
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)

    tok = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    )
    assert tok.status_code == 200
    body = tok.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["expires_in"] == 3600

    mcp = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": f"Bearer {body['access_token']}", "Accept": "application/json, text/event-stream"},
    )
    assert mcp.status_code == 200
    assert "tp_get_workouts" in mcp.text


def test_pkce_enforced(client):
    client_id = _register(client)
    _, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    bad = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": "WRONG-VERIFIER",
        },
    )
    assert bad.status_code == 400


def test_refresh_rotation(client):
    client_id = _register(client)
    verifier, challenge = _pkce()
    code = _get_code(client, client_id, challenge)
    tok = client.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": client_id,
            "code_verifier": verifier,
        },
    ).json()
    refresh = tok["refresh_token"]

    ok = client.post("/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
    assert ok.status_code == 200 and ok.json()["access_token"]

    # Old refresh token is single-use (rotated).
    again = client.post(
        "/token", data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id}
    )
    assert again.status_code == 400


def test_subject_binding_persisted(client):
    client_id = _register(client)
    _, challenge = _pkce()
    _get_code(client, client_id, challenge)

    from tp_mcp.auth.multiuser_store import get_user_cookie, get_user_meta
    from tp_mcp.oauth.tokens import subject_for

    subject = subject_for("999", "test-secret")
    assert get_user_cookie(subject).success
    assert get_user_meta(subject)["tp_email"] == "rider@example.com"
