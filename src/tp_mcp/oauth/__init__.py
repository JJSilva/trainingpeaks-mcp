"""OAuth 2.1 Authorization Server + Resource Server for the hosted MCP transport.

This package lets MCP clients "click a button and sign in" with a TrainingPeaks
email/password. The server acts as its own OAuth authorization server: its
``/authorize`` step renders a TrainingPeaks login page, logs in to obtain the
user's ``Production_tpAuth`` cookie, binds it to an opaque subject, and issues the
MCP client its own access/refresh tokens.
"""

from tp_mcp.oauth.config import OAuthConfig, load_config

__all__ = ["OAuthConfig", "load_config"]
