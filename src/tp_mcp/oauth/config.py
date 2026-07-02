"""Configuration for the hosted OAuth flow, read from environment variables."""

import os
from dataclasses import dataclass

from tp_mcp.auth.storage import ENV_VAR_NAME as TP_AUTH_COOKIE_ENV

# Environment variables
PUBLIC_URL_ENV = "TP_MCP_PUBLIC_URL"
TOKEN_SECRET_ENV = "TP_MCP_TOKEN_SECRET"
AUTH_ENABLED_ENV = "TP_MCP_AUTH_ENABLED"
ACCESS_TTL_ENV = "TP_MCP_ACCESS_TTL"
REFRESH_TTL_ENV = "TP_MCP_REFRESH_TTL"

DEFAULT_ACCESS_TTL = 3600  # 1 hour
DEFAULT_REFRESH_TTL = 60 * 60 * 24 * 60  # 60 days
AUTH_CODE_TTL = 600  # 10 minutes
PENDING_AUTH_TTL = 900  # 15 minutes (login page lifetime)


@dataclass
class OAuthConfig:
    """Resolved OAuth configuration."""

    enabled: bool
    public_url: str
    token_secret: str
    access_ttl: int = DEFAULT_ACCESS_TTL
    refresh_ttl: int = DEFAULT_REFRESH_TTL


def _truthy(val: str | None) -> bool | None:
    if val is None:
        return None
    return val.strip().lower() in ("1", "true", "yes", "on")


def auth_enabled() -> bool:
    """Whether OAuth sign-in is enabled for the HTTP transport.

    Explicit ``TP_MCP_AUTH_ENABLED`` wins. Otherwise auth is on when a public URL
    is configured and no single-user ``TP_AUTH_COOKIE`` is set (so existing
    single-user deployments keep working unauthenticated by default).
    """
    explicit = _truthy(os.environ.get(AUTH_ENABLED_ENV))
    if explicit is not None:
        return explicit
    has_public = bool(os.environ.get(PUBLIC_URL_ENV))
    has_single_user_cookie = bool(os.environ.get(TP_AUTH_COOKIE_ENV))
    return has_public and not has_single_user_cookie


def load_config() -> OAuthConfig:
    """Load and validate OAuth configuration. Raises ValueError if misconfigured."""
    enabled = auth_enabled()
    public_url = (os.environ.get(PUBLIC_URL_ENV) or "").rstrip("/")
    token_secret = os.environ.get(TOKEN_SECRET_ENV) or ""

    if enabled:
        if not public_url:
            raise ValueError(f"{PUBLIC_URL_ENV} is required when OAuth is enabled.")
        if not public_url.startswith("https://") and "localhost" not in public_url and "127.0.0.1" not in public_url:
            raise ValueError(f"{PUBLIC_URL_ENV} must be an https URL (got {public_url!r}).")
        if not token_secret:
            raise ValueError(f"{TOKEN_SECRET_ENV} is required when OAuth is enabled.")

    def _int_env(name: str, default: int) -> int:
        raw = os.environ.get(name)
        try:
            return int(raw) if raw else default
        except ValueError:
            return default

    return OAuthConfig(
        enabled=enabled,
        public_url=public_url,
        token_secret=token_secret,
        access_ttl=_int_env(ACCESS_TTL_ENV, DEFAULT_ACCESS_TTL),
        refresh_ttl=_int_env(REFRESH_TTL_ENV, DEFAULT_REFRESH_TTL),
    )
