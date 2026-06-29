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


def _build_app() -> Starlette:
    """Build the Starlette app that serves the MCP server over HTTP."""
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

    return Starlette(
        debug=False,
        routes=[
            Route("/", health, methods=["GET"]),
            Route("/health", health, methods=["GET"]),
            # MCP endpoint. A POST to "/mcp" 307-redirects to "/mcp/", which
            # preserves method+body, so both paths work for compliant clients.
            Mount("/mcp", app=handle_mcp),
        ],
        lifespan=lifespan,
    )


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
