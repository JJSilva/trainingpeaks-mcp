"""Opaque token minting and stable subject derivation."""

import hashlib
import hmac
import secrets


def new_token() -> str:
    """Generate a random opaque token (access / refresh / auth code)."""
    return secrets.token_urlsafe(32)


def new_login_session() -> str:
    """Generate a random id for a pending authorization / login page."""
    return secrets.token_urlsafe(24)


def subject_for(identity: str, secret: str) -> str:
    """Derive a stable opaque subject for a TrainingPeaks identity.

    Uses HMAC-SHA256 keyed with the server token secret so the same TrainingPeaks
    user always maps to the same subject (one cookie row per user), without
    storing the raw identity as the key.

    Args:
        identity: A stable TrainingPeaks identifier (user id, else email).
        secret: The server ``TP_MCP_TOKEN_SECRET``.

    Returns:
        A ``tp_`` prefixed hex subject id.
    """
    digest = hmac.new(secret.encode("utf-8"), str(identity).encode("utf-8"), hashlib.sha256).hexdigest()
    return "tp_" + digest[:32]
