"""TrainingPeaks-backed OAuth 2.1 authorization server provider.

Implements ``mcp.server.auth.provider.OAuthAuthorizationServerProvider``. The
distinctive piece is :meth:`authorize`, which does NOT talk to TrainingPeaks
directly: it parks the (already SDK-validated) authorization request and redirects
the browser to our own ``/tp-login`` page. That page logs the user into
TrainingPeaks and calls back to mint the authorization code.
"""

import time
from urllib.parse import urlencode

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from tp_mcp.oauth import store
from tp_mcp.oauth.config import AUTH_CODE_TTL, PENDING_AUTH_TTL, OAuthConfig
from tp_mcp.oauth.tokens import new_login_session, new_token


class TPOAuthProvider:
    """OAuth AS provider whose login step is a TrainingPeaks sign-in."""

    def __init__(self, config: OAuthConfig):
        self.config = config

    # --- Clients (Dynamic Client Registration) ---------------------------
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        return store.get_client(client_id)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        store.save_client(client_info)

    # --- Authorization ---------------------------------------------------
    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        """Park the request and redirect to our TrainingPeaks login page.

        PKCE and redirect_uri validation already happened in the SDK's authorize
        handler, so ``params`` can be trusted and stored verbatim.
        """
        login_session = new_login_session()
        store.save_pending(
            login_session=login_session,
            client_id=client.client_id,
            params_json=params.model_dump_json(),
            ttl=PENDING_AUTH_TTL,
        )
        query = urlencode({"login_session": login_session})
        return f"{self.config.public_url}/tp-login?{query}"

    def complete_authorization(self, pending_params: AuthorizationParams, client_id: str, subject: str) -> str:
        """Mint an authorization code bound to ``subject`` and build the client redirect.

        Called by the login handler after a successful TrainingPeaks sign-in.
        Returns the URL to redirect the browser back to the MCP client.
        """
        code_value = new_token()
        auth_code = AuthorizationCode(
            code=code_value,
            scopes=pending_params.scopes or [],
            expires_at=time.time() + AUTH_CODE_TTL,
            client_id=client_id,
            code_challenge=pending_params.code_challenge,
            redirect_uri=pending_params.redirect_uri,
            redirect_uri_provided_explicitly=pending_params.redirect_uri_provided_explicitly,
            resource=pending_params.resource,
            subject=subject,
        )
        store.save_auth_code(auth_code)

        redirect_params: dict[str, str] = {"code": code_value}
        if pending_params.state is not None:
            redirect_params["state"] = pending_params.state
        sep = "&" if "?" in str(pending_params.redirect_uri) else "?"
        return f"{pending_params.redirect_uri}{sep}{urlencode(redirect_params)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        code = store.get_auth_code(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        # Single-use: consume the code immediately.
        store.delete_auth_code(authorization_code.code)
        return self._issue_tokens(
            client_id=client.client_id,
            scopes=authorization_code.scopes,
            subject=authorization_code.subject,
            resource=authorization_code.resource,
        )

    # --- Refresh ---------------------------------------------------------
    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        token = store.get_refresh_token(refresh_token)
        if token is None or token.client_id != client.client_id:
            return None
        return token

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        # Rotate: invalidate the presented refresh token, issue a fresh pair.
        store.delete_refresh_token(refresh_token.token)
        requested = scopes or refresh_token.scopes
        return self._issue_tokens(
            client_id=client.client_id,
            scopes=requested,
            subject=refresh_token.subject,
            resource=None,
        )

    # --- Access token verification --------------------------------------
    async def load_access_token(self, token: str) -> AccessToken | None:
        return store.get_access_token(token)

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        if isinstance(token, AccessToken):
            store.delete_access_token(token.token)
        else:
            store.delete_refresh_token(token.token)

    # --- helpers ---------------------------------------------------------
    def _issue_tokens(
        self, *, client_id: str, scopes: list[str], subject: str | None, resource: str | None
    ) -> OAuthToken:
        now = int(time.time())
        access_value = new_token()
        refresh_value = new_token()
        store.save_access_token(
            AccessToken(
                token=access_value,
                client_id=client_id,
                scopes=scopes,
                expires_at=now + self.config.access_ttl,
                resource=resource,
                subject=subject,
            )
        )
        store.save_refresh_token(
            RefreshToken(
                token=refresh_value,
                client_id=client_id,
                scopes=scopes,
                expires_at=now + self.config.refresh_ttl,
                subject=subject,
            )
        )
        return OAuthToken(
            access_token=access_value,
            token_type="Bearer",
            expires_in=self.config.access_ttl,
            scope=" ".join(scopes) if scopes else None,
            refresh_token=refresh_value,
        )
