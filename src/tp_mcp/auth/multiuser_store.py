"""Per-user (multi-tenant) TrainingPeaks cookie storage.

Maps an opaque OAuth ``subject`` to that user's encrypted ``Production_tpAuth``
cookie. Used by the hosted server when a request is authenticated as a specific
subject; the single-user/local path (``subject is None``) continues to use
``tp_mcp.auth.storage`` (env / keyring / encrypted file).

The cookie is encrypted at rest with AES-256-GCM via
:func:`tp_mcp.auth.encrypted.encrypt_blob` (key from ``TP_MCP_ENC_KEY`` or the
machine-derived default). Cookie values are never logged.
"""

import time

from tp_mcp.auth.db import connect
from tp_mcp.auth.encrypted import decrypt_blob, encrypt_blob
from tp_mcp.auth.keyring import CredentialResult

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    subject       TEXT PRIMARY KEY,
    tp_email      TEXT,
    tp_athlete_id INTEGER,
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS tp_cookies (
    subject     TEXT PRIMARY KEY REFERENCES users(subject) ON DELETE CASCADE,
    enc_cookie  BLOB NOT NULL,
    expires_at  INTEGER,
    updated_at  INTEGER NOT NULL
);
"""

_initialized = False


def _init() -> None:
    global _initialized
    if _initialized:
        return
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
    finally:
        conn.close()
    _initialized = True


def store_user_cookie(
    subject: str,
    cookie: str,
    *,
    tp_email: str | None = None,
    tp_athlete_id: int | None = None,
    expires_at: int | None = None,
) -> CredentialResult:
    """Encrypt and persist a subject's TrainingPeaks cookie (upsert)."""
    if not cookie or not cookie.strip():
        return CredentialResult(success=False, message="Cookie value cannot be empty")
    _init()
    now = int(time.time())
    enc = encrypt_blob(cookie.strip())
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO users (subject, tp_email, tp_athlete_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(subject) DO UPDATE SET
                tp_email=COALESCE(excluded.tp_email, users.tp_email),
                tp_athlete_id=COALESCE(excluded.tp_athlete_id, users.tp_athlete_id),
                updated_at=excluded.updated_at
            """,
            (subject, (tp_email or "").lower() or None, tp_athlete_id, now, now),
        )
        conn.execute(
            """
            INSERT INTO tp_cookies (subject, enc_cookie, expires_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subject) DO UPDATE SET
                enc_cookie=excluded.enc_cookie,
                expires_at=excluded.expires_at,
                updated_at=excluded.updated_at
            """,
            (subject, enc, expires_at, now),
        )
        return CredentialResult(success=True, message="Cookie stored for subject")
    except Exception as e:
        return CredentialResult(success=False, message=f"Store error ({type(e).__name__})")
    finally:
        conn.close()


def get_user_cookie(subject: str) -> CredentialResult:
    """Retrieve and decrypt a subject's TrainingPeaks cookie."""
    _init()
    conn = connect()
    try:
        row = conn.execute("SELECT enc_cookie FROM tp_cookies WHERE subject=?", (subject,)).fetchone()
    finally:
        conn.close()
    if not row:
        return CredentialResult(success=False, message="No credential for this account")
    try:
        cookie = decrypt_blob(bytes(row["enc_cookie"]))
        return CredentialResult(success=True, message="Credential retrieved", cookie=cookie)
    except Exception:
        return CredentialResult(success=False, message="Decryption failed. Sign in again.")


def clear_user_cookie(subject: str) -> CredentialResult:
    """Remove a subject's stored cookie (keeps the user row)."""
    _init()
    conn = connect()
    try:
        conn.execute("DELETE FROM tp_cookies WHERE subject=?", (subject,))
        return CredentialResult(success=True, message="Credential cleared")
    except Exception as e:
        return CredentialResult(success=False, message=f"Clear error ({type(e).__name__})")
    finally:
        conn.close()


def get_user_meta(subject: str) -> dict | None:
    """Return non-secret metadata (email, athlete id) for a subject, if any."""
    _init()
    conn = connect()
    try:
        row = conn.execute(
            "SELECT subject, tp_email, tp_athlete_id, created_at, updated_at FROM users WHERE subject=?",
            (subject,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None
