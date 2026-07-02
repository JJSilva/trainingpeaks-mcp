"""HTTP (Streamable HTTP) transport for the TrainingPeaks MCP server.

The default transport (``tp-mcp serve``) speaks stdio, which is what
Claude Desktop and other local MCP clients expect. That transport cannot be
used on a hosting platform like Railway, because there is no persistent stdin/
stdout pipe to a parent process.

This module exposes the same MCP ``server`` object over the MCP
**Streamable HTTP** transport, wrapped in a small Starlette app so it can be
served by uvicorn and reached over the network. It listens on ``$PORT`` (the
variable Railway injects) and serves the MCP endpoint at ``/mcp``.

Authentication uses the ``TP_AUTH_COOKIE`` environment variable (already
supported by ``tp_mcp.auth.storage``), so no interactive ``tp-mcp auth`` step
is required in a server deployment.

Run locally with:

    PORT=8000 TP_AUTH_COOKIE="<cookie>" tp-mcp serve-http

or via the module:

    python -m tp_mcp.http_server
"""

import contextlib
import logging
import os
from collections.abc import AsyncIterator

import uvicorn
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from tp_mcp.server import _validate_auth_on_startup, server

logger = logging.getLogger("tp-mcp.http")


def _build_auth(routes: list, config) -> list:
    """Add OAuth AS/RS routes + middleware for the /mcp app.

    Returns the middleware list to pass to Starlette. Mutates ``routes`` in place
    to insert the .well-known metadata, /authorize, /token, /register, /revoke, and
    the interactive /tp-login pages, and to wrap the given /mcp Mount with bearer
    auth. Only called when OAuth is enabled.
    """
    from mcp.server.auth.middleware.auth_context import AuthContextMiddleware
    from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend, RequireAuthMiddleware
    from mcp.server.auth.provider import ProviderTokenVerifier
    from mcp.server.auth.routes import (
        build_resource_metadata_url,
        create_auth_routes,
        create_protected_resource_routes,
    )
    from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
    from pydantic import AnyHttpUrl
    from starlette.middleware import Middleware
    from starlette.middleware.authentication import AuthenticationMiddleware

    from tp_mcp.oauth.login import build_login_routes
    from tp_mcp.oauth.provider import TPOAuthProvider

    issuer = AnyHttpUrl(config.public_url)
    resource = AnyHttpUrl(config.public_url)
    provider = TPOAuthProvider(config)

    # Wrap the /mcp Mount (last route added by _build_app) with bearer auth.
    mcp_mount = routes[-1]
    resource_metadata_url = build_resource_metadata_url(resource)
    guarded = RequireAuthMiddleware(mcp_mount.app, required_scopes=[], resource_metadata_url=resource_metadata_url)
    routes[-1] = Mount("/mcp", app=guarded)

    routes[:-1] = [
        *routes[:-1],
        *create_auth_routes(
            provider=provider,
            issuer_url=issuer,
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
        ),
        *create_protected_resource_routes(
            resource_url=resource,
            authorization_servers=[issuer],
            scopes_supported=[],
        ),
        *build_login_routes(provider, config),
    ]

    return [
        Middleware(AuthenticationMiddleware, backend=BearerAuthBackend(ProviderTokenVerifier(provider))),
        Middleware(AuthContextMiddleware),
    ]


class _NormalizeMcpPath:
    """Rewrite a bare ``/mcp`` request to ``/mcp/`` in-process.

    Mounted ASGI apps only match the trailing-slash form, and Starlette would
    otherwise answer ``/mcp`` with a 307 redirect. Behind Railway's TLS proxy
    that redirect points at ``http://`` and many MCP clients refuse to follow
    it. Rewriting the path here keeps everything on one request/scheme.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            scope = dict(scope)
            scope["path"] = "/mcp/"
            scope["raw_path"] = b"/mcp/"
        await self.app(scope, receive, send)


def _build_app() -> "_NormalizeMcpPath":
    """Build the ASGI app that serves the MCP server over HTTP."""
    # Stateless mode keeps each request self-contained, which plays nicely with
    # platform load balancers/proxies (no sticky-session requirement).
    session_manager = StreamableHTTPSessionManager(
        app=server,
        json_response=False,
        stateless=True,
    )

    async def handle_mcp(scope, receive, send) -> None:
        await session_manager.handle_request(scope, receive, send)

    async def health(_request: Request) -> Response:
        """Lightweight health check for Railway."""
        return JSONResponse({"status": "ok", "service": "trainingpeaks-mcp"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        # Validate the TrainingPeaks cookie on startup (warns if missing/invalid).
        try:
            await _validate_auth_on_startup()
        except Exception:  # pragma: no cover - defensive, never block startup
            logger.exception("Auth validation on startup failed")
        async with session_manager.run():
            logger.info("TrainingPeaks MCP server (Streamable HTTP) started")
            yield
            logger.info("TrainingPeaks MCP server shutting down")

    from tp_mcp.oauth.config import load_config

    config = load_config()

    routes = [
        Route("/", health, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
        Mount("/mcp", app=handle_mcp),
    ]
    middleware: list = []
    if config.enabled:
        logger.info("OAuth sign-in enabled (issuer: %s)", config.public_url)
        middleware = _build_auth(routes, config)
    else:
        logger.warning(
            "OAuth sign-in disabled - the /mcp endpoint is UNAUTHENTICATED. "
            "Set TP_MCP_PUBLIC_URL + TP_MCP_TOKEN_SECRET to enable it."
        )

    starlette_app = Starlette(
        debug=False,
        routes=routes,
        middleware=middleware,
        lifespan=lifespan,
    )
    # Serve the MCP endpoint at both "/mcp" and "/mcp/" with no HTTP redirect.
    return _NormalizeMcpPath(starlette_app)


def run_http_server() -> int:
    """Run the MCP server over Streamable HTTP (entry point for hosting)."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8000"))

    logger.info("Starting TrainingPeaks MCP (Streamable HTTP) on %s:%s/mcp", host, port)
    uvicorn.run(_build_app(), host=host, port=port, log_level="info")
    return 0


# Module-level ASGI app so `uvicorn tp_mcp.http_server:app` also works.
app = _build_app()


if __name__ == "__main__":
    raise SystemExit(run_http_server())
