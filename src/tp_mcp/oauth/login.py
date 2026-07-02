"""Interactive TrainingPeaks sign-in pages that bridge /authorize to the client.

These unauthenticated Starlette routes render the login/MFA forms, drive
:mod:`tp_mcp.auth.tp_login`, bind the resulting cookie to an opaque subject, and
redirect the browser back to the MCP client's redirect_uri with an auth code.
"""

import dataclasses
import logging

from mcp.server.auth.provider import AuthorizationParams
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from tp_mcp.auth import tp_login as tpl
from tp_mcp.auth.multiuser_store import store_user_cookie
from tp_mcp.auth.validator import validate_auth
from tp_mcp.oauth import store
from tp_mcp.oauth.config import PENDING_AUTH_TTL, OAuthConfig
from tp_mcp.oauth.provider import TPOAuthProvider
from tp_mcp.oauth.templates import login_page, message_page, mfa_page
from tp_mcp.oauth.tokens import subject_for

logger = logging.getLogger("tp-mcp.oauth")

_NO_STORE = {"Cache-Control": "no-store"}


def build_login_routes(provider: TPOAuthProvider, config: OAuthConfig) -> list[Route]:
    """Build the /tp-login routes bound to a provider + config."""

    async def _complete(login_session: str, cookie: str) -> Response:
        """Validate the cookie, bind it to a subject, and redirect back to the client."""
        auth = await validate_auth(cookie)
        if not auth.is_valid:
            return HTMLResponse(
                login_page(login_session, error="That TrainingPeaks session is not valid. Try again."),
                status_code=400,
                headers=_NO_STORE,
            )

        identity = str(auth.user_id or auth.athlete_id or auth.email or "")
        if not identity:
            return HTMLResponse(
                login_page(login_session, error="Could not identify the TrainingPeaks account."),
                status_code=400,
                headers=_NO_STORE,
            )
        subject = subject_for(identity, config.token_secret)
        store_user_cookie(subject, cookie, tp_email=auth.email, tp_athlete_id=auth.athlete_id)

        pending = store.get_pending(login_session)
        if pending is None:
            return HTMLResponse(
                message_page("Session expired", "Please restart the connection from your client."),
                status_code=400,
                headers=_NO_STORE,
            )
        params = AuthorizationParams.model_validate_json(pending["params_json"])
        redirect_url = provider.complete_authorization(params, pending["client_id"], subject)
        store.delete_pending(login_session)
        store.delete_mfa(login_session)
        logger.info("TrainingPeaks sign-in complete for subject %s", subject)
        return RedirectResponse(redirect_url, status_code=302, headers=_NO_STORE)

    async def tp_login_get(request: Request) -> Response:
        login_session = request.query_params.get("login_session", "")
        if not login_session or store.get_pending(login_session) is None:
            return HTMLResponse(
                message_page("Invalid or expired link", "Please restart the connection from your client."),
                status_code=400,
                headers=_NO_STORE,
            )
        return HTMLResponse(login_page(login_session), headers=_NO_STORE)

    async def tp_login_post(request: Request) -> Response:
        form = await request.form()
        login_session = str(form.get("login_session", ""))
        if not login_session or store.get_pending(login_session) is None:
            return HTMLResponse(
                message_page("Session expired", "Please restart the connection from your client."),
                status_code=400,
                headers=_NO_STORE,
            )

        # Manual cookie fallback (for CAPTCHA/MFA-blocked accounts).
        cookie = str(form.get("cookie", "")).strip()
        if cookie:
            return await _complete(login_session, cookie)

        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        result = await tpl.login(username, password)

        if result.outcome == tpl.LoginOutcome.SUCCESS and result.cookie:
            return await _complete(login_session, result.cookie)

        if result.outcome == tpl.LoginOutcome.MFA_REQUIRED and result.mfa_state:
            store.save_mfa(login_session, dataclasses.asdict(result.mfa_state), ttl=PENDING_AUTH_TTL)
            return HTMLResponse(
                mfa_page(login_session, result.mfa_state.available_methods),
                headers=_NO_STORE,
            )

        return HTMLResponse(
            login_page(login_session, error=result.message or "Sign-in failed."),
            status_code=400,
            headers=_NO_STORE,
        )

    async def tp_login_mfa_post(request: Request) -> Response:
        form = await request.form()
        login_session = str(form.get("login_session", ""))
        code = str(form.get("code", "")).strip()
        mfa_dict = store.get_mfa(login_session)
        if mfa_dict is None:
            return HTMLResponse(
                message_page("Session expired", "Please restart the connection from your client."),
                status_code=400,
                headers=_NO_STORE,
            )

        state = tpl.MfaState(**mfa_dict)
        method = str(form.get("method", "")).strip()
        if method:
            state.selected_method = method
        result = await tpl.submit_mfa(state, code)

        if result.outcome == tpl.LoginOutcome.SUCCESS and result.cookie:
            return await _complete(login_session, result.cookie)

        return HTMLResponse(
            mfa_page(login_session, state.available_methods, error=result.message or "Verification failed."),
            status_code=400,
            headers=_NO_STORE,
        )

    return [
        Route("/tp-login", tp_login_get, methods=["GET"]),
        Route("/tp-login", tp_login_post, methods=["POST"]),
        Route("/tp-login/mfa", tp_login_mfa_post, methods=["POST"]),
    ]
