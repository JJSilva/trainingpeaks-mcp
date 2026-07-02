"""SQLite-backed persistence for OAuth clients, tokens, codes, and login state.

Shares the database file with :mod:`tp_mcp.auth.multiuser_store` (see
:mod:`tp_mcp.auth.db`). All records are small JSON blobs of the corresponding
``mcp`` models; secret token values are the primary keys and are never logged.
"""

import json
import time

from mcp.server.auth.provider import AccessToken, AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from tp_mcp.auth.db import connect

_SCHEMA = """
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id   TEXT PRIMARY KEY,
    client_json TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_auth_codes (
    code       TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    code_json  TEXT NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token      TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    token_json TEXT NOT NULL,
    expires_at INTEGER
);
CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token      TEXT PRIMARY KEY,
    client_id  TEXT NOT NULL,
    token_json TEXT NOT NULL,
    expires_at INTEGER
);
CREATE TABLE IF NOT EXISTS oauth_pending (
    login_session TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL,
    params_json   TEXT NOT NULL,
    expires_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_mfa (
    login_session TEXT PRIMARY KEY,
    mfa_json      TEXT NOT NULL,
    expires_at    INTEGER NOT NULL
);
"""

_initialized = False


def init() -> None:
    """Create OAuth tables if needed (idempotent)."""
    global _initialized
    if _initialized:
        return
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()
    _initialized = True


# --- Clients ---------------------------------------------------------------

def save_client(client: OAuthClientInformationFull) -> None:
    init()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, client_json, created_at) VALUES (?, ?, ?)",
            (client.client_id, client.model_dump_json(), int(time.time())),
        )
    finally:
        conn.close()


def get_client(client_id: str) -> OAuthClientInformationFull | None:
    init()
    conn = connect()
    try:
        row = conn.execute("SELECT client_json FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return OAuthClientInformationFull.model_validate_json(row["client_json"])


# --- Authorization codes ---------------------------------------------------

def save_auth_code(code: AuthorizationCode) -> None:
    init()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_auth_codes (code, client_id, code_json, expires_at) VALUES (?, ?, ?, ?)",
            (code.code, code.client_id, code.model_dump_json(), int(code.expires_at)),
        )
    finally:
        conn.close()


def get_auth_code(code: str) -> AuthorizationCode | None:
    init()
    conn = connect()
    try:
        row = conn.execute("SELECT code_json, expires_at FROM oauth_auth_codes WHERE code=?", (code,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row["expires_at"] < time.time():
        delete_auth_code(code)
        return None
    return AuthorizationCode.model_validate_json(row["code_json"])


def delete_auth_code(code: str) -> None:
    init()
    conn = connect()
    try:
        conn.execute("DELETE FROM oauth_auth_codes WHERE code=?", (code,))
    finally:
        conn.close()


# --- Access tokens ---------------------------------------------------------

def save_access_token(token: AccessToken) -> None:
    init()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_access_tokens (token, client_id, token_json, expires_at) VALUES (?, ?, ?, ?)",
            (token.token, token.client_id, token.model_dump_json(), token.expires_at),
        )
    finally:
        conn.close()


def get_access_token(token: str) -> AccessToken | None:
    init()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT token_json, expires_at FROM oauth_access_tokens WHERE token=?", (token,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row["expires_at"] is not None and row["expires_at"] < time.time():
        delete_access_token(token)
        return None
    return AccessToken.model_validate_json(row["token_json"])


def delete_access_token(token: str) -> None:
    init()
    conn = connect()
    try:
        conn.execute("DELETE FROM oauth_access_tokens WHERE token=?", (token,))
    finally:
        conn.close()


# --- Refresh tokens --------------------------------------------------------

def save_refresh_token(token: RefreshToken) -> None:
    init()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_refresh_tokens "
            "(token, client_id, token_json, expires_at) VALUES (?, ?, ?, ?)",
            (token.token, token.client_id, token.model_dump_json(), token.expires_at),
        )
    finally:
        conn.close()


def get_refresh_token(token: str) -> RefreshToken | None:
    init()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT token_json, expires_at FROM oauth_refresh_tokens WHERE token=?", (token,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row["expires_at"] is not None and row["expires_at"] < time.time():
        delete_refresh_token(token)
        return None
    return RefreshToken.model_validate_json(row["token_json"])


def delete_refresh_token(token: str) -> None:
    init()
    conn = connect()
    try:
        conn.execute("DELETE FROM oauth_refresh_tokens WHERE token=?", (token,))
    finally:
        conn.close()


# --- Pending authorizations (login sessions) -------------------------------

def save_pending(login_session: str, client_id: str, params_json: str, ttl: int) -> None:
    init()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_pending "
            "(login_session, client_id, params_json, expires_at) VALUES (?, ?, ?, ?)",
            (login_session, client_id, params_json, int(time.time()) + ttl),
        )
    finally:
        conn.close()


def get_pending(login_session: str) -> dict | None:
    """Return ``{"client_id": ..., "params_json": ...}`` or None if missing/expired."""
    init()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT client_id, params_json, expires_at FROM oauth_pending WHERE login_session=?",
            (login_session,),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row["expires_at"] < time.time():
        delete_pending(login_session)
        return None
    return {"client_id": row["client_id"], "params_json": row["params_json"]}


def delete_pending(login_session: str) -> None:
    init()
    conn = connect()
    try:
        conn.execute("DELETE FROM oauth_pending WHERE login_session=?", (login_session,))
    finally:
        conn.close()


# --- MFA continuation state ------------------------------------------------

def save_mfa(login_session: str, state: dict, ttl: int) -> None:
    init()
    conn = connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_mfa (login_session, mfa_json, expires_at) VALUES (?, ?, ?)",
            (login_session, json.dumps(state), int(time.time()) + ttl),
        )
    finally:
        conn.close()


def get_mfa(login_session: str) -> dict | None:
    init()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT mfa_json, expires_at FROM oauth_mfa WHERE login_session=?", (login_session,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    if row["expires_at"] < time.time():
        delete_mfa(login_session)
        return None
    return json.loads(row["mfa_json"])


def delete_mfa(login_session: str) -> None:
    init()
    conn = connect()
    try:
        conn.execute("DELETE FROM oauth_mfa WHERE login_session=?", (login_session,))
    finally:
        conn.close()
