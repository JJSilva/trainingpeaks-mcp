"""HTTP client wrapper for TrainingPeaks API."""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from tp_mcp.auth import get_credential

TP_API_BASE = "https://tpapi.trainingpeaks.com"
DEFAULT_TIMEOUT = 30.0
MIN_REQUEST_INTERVAL = 0.15  # 150ms between requests to avoid rate limiting
TOKEN_ENDPOINT = "/users/v3/token"
TOKEN_REFRESH_BUFFER = 60  # Refresh token 60s before expiry


class APIError(Exception):
    """Base exception for API errors."""

    pass


class AuthenticationError(APIError):
    """Authentication failed or expired."""

    pass


class NotFoundError(APIError):
    """Resource not found."""

    pass


class RateLimitError(APIError):
    """Rate limit exceeded."""

    pass


class ErrorCode(Enum):
    """Error codes for API responses."""

    AUTH_EXPIRED = "AUTH_EXPIRED"
    AUTH_INVALID = "AUTH_INVALID"
    NOT_FOUND = "NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    PREMIUM_REQUIRED = "PREMIUM_REQUIRED"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    API_ERROR = "API_ERROR"
    NETWORK_ERROR = "NETWORK_ERROR"


@dataclass
class APIResponse:
    """Wrapper for API responses."""

    success: bool
    data: dict[str, Any] | list[Any] | None = None
    error_code: ErrorCode | None = None
    message: str = ""

    @property
    def is_error(self) -> bool:
        """Check if response is an error."""
        return not self.success


@dataclass
class RawResponse:
    """Wrapper for raw binary API responses."""

    success: bool
    content: bytes = field(default_factory=bytes)
    content_type: str | None = None
    content_disposition: str | None = None
    error_code: ErrorCode | None = None
    message: str = ""

    @property
    def is_error(self) -> bool:
        """Check if response is an error."""
        return not self.success


@dataclass
class TokenCache:
    """In-memory cache for OAuth access token."""

    access_token: str | None = None
    expires_at: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    def is_valid(self, buffer_seconds: int = TOKEN_REFRESH_BUFFER) -> bool:
        """Check if token is valid with buffer before expiry."""
        if not self.access_token:
            return False
        return time.time() < (self.expires_at - buffer_seconds)

    def clear(self) -> None:
        """Clear the cached token."""
        self.access_token = None
        self.expires_at = 0.0


# Sentinel key for the single-user / local (subject is None) case.
_DEFAULT_SUBJECT = "__default__"


@dataclass
class _IdentityCache:
    """All per-subject cached state. One instance per distinct OAuth subject.

    Isolating this per subject is what makes the server safe for multiple users:
    one user's access token / athlete id / profile can never be served to another.
    """

    token_cache: TokenCache = field(default_factory=TokenCache)
    athlete_id: int | None = None
    user_data: dict | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class TPClient:
    """Async HTTP client for TrainingPeaks API.

    Handles authentication, error handling, and response parsing.
    """

    # Per-subject caches: persist across instances within the MCP server process,
    # keyed by OAuth subject so different users never share token/athlete state.
    _identity_caches: dict[str, _IdentityCache] = {}
    _caches_guard: asyncio.Lock = asyncio.Lock()

    @staticmethod
    def _subject_key() -> str:
        """Resolve the cache key for the current request's subject.

        Read at call time (never captured in __init__) so each awaited call maps
        to the correct user. ``None`` subject -> the single shared default bucket,
        which reproduces the previous single-user behavior exactly.
        """
        from tp_mcp.client.context import current_subject

        return current_subject.get() or _DEFAULT_SUBJECT

    @classmethod
    async def _get_identity_cache(cls) -> _IdentityCache:
        """Get or create the identity cache for the current subject."""
        key = cls._subject_key()
        cache = cls._identity_caches.get(key)
        if cache is None:
            async with cls._caches_guard:
                cache = cls._identity_caches.get(key)
                if cache is None:
                    cache = _IdentityCache()
                    cls._identity_caches[key] = cache
        return cache

    @classmethod
    def invalidate_subject(cls, subject: str | None = None) -> None:
        """Drop cached state for a subject (e.g. after a cookie rotation)."""
        cls._identity_caches.pop(subject or _DEFAULT_SUBJECT, None)

    async def current_access_token(self) -> str | None:
        """Return the cached OAuth access token for the current subject, if any."""
        cache = await self._get_identity_cache()
        return cache.token_cache.access_token

    def __init__(self, timeout: float = DEFAULT_TIMEOUT):
        """Initialize the client.

        Args:
            timeout: Request timeout in seconds.
        """
        self.base_url = TP_API_BASE
        self.timeout = timeout
        self._client: httpx.AsyncClient | None = None
        self._athlete_id: int | None = None
        self._last_request_time: float = 0.0

    async def __aenter__(self) -> "TPClient":
        """Enter async context."""
        await self._ensure_client()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        """Exit async context."""
        await self.close()

    async def _ensure_client(self) -> None:
        """Ensure the HTTP client is initialized."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self.timeout)

    async def _throttle(self) -> None:
        """Enforce minimum interval between requests to avoid rate limiting."""
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < MIN_REQUEST_INTERVAL:
            await asyncio.sleep(MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.monotonic()

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_headers(self, access_token: str | None) -> dict[str, str]:
        """Get request headers with Bearer token authentication.

        Args:
            access_token: The current subject's OAuth access token.

        Returns:
            Headers dict with Authorization header.
        """
        return {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _get_cookie_headers(self, cookie: str) -> dict[str, str]:
        """Get request headers with cookie authentication (for token exchange).

        Args:
            cookie: The Production_tpAuth cookie value.

        Returns:
            Headers dict with Cookie header.
        """
        return {
            "Cookie": f"Production_tpAuth={cookie}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    async def _exchange_cookie_for_token(self) -> APIResponse:
        """Exchange the stored cookie for an OAuth access token.

        Returns:
            APIResponse with token data or error.
        """
        await self._ensure_client()
        await self._throttle()
        assert self._client is not None

        cred = get_credential()
        if not cred.success or not cred.cookie:
            return APIResponse(
                success=False,
                error_code=ErrorCode.AUTH_INVALID,
                message="No credential stored. Run 'tp-mcp auth' to authenticate.",
            )

        url = f"{self.base_url}{TOKEN_ENDPOINT}"
        headers = self._get_cookie_headers(cred.cookie)

        try:
            response = await self._client.request(
                method="GET",
                url=url,
                headers=headers,
            )

            if response.status_code == 401:
                return APIResponse(
                    success=False,
                    error_code=ErrorCode.AUTH_EXPIRED,
                    message="Cookie expired. Use 'tp_refresh_auth' tool to re-authenticate.",
                )

            if response.status_code != 200:
                return APIResponse(
                    success=False,
                    error_code=ErrorCode.API_ERROR,
                    message=f"Token exchange failed: {response.status_code}",
                )

            data = response.json()
            if not data.get("success") or "token" not in data:
                return APIResponse(
                    success=False,
                    error_code=ErrorCode.API_ERROR,
                    message="Invalid token response format",
                )

            return APIResponse(success=True, data=data)

        except httpx.TimeoutException:
            return APIResponse(
                success=False,
                error_code=ErrorCode.NETWORK_ERROR,
                message="Token exchange timed out.",
            )
        except httpx.RequestError as e:
            return APIResponse(
                success=False,
                error_code=ErrorCode.NETWORK_ERROR,
                message=f"Network error during token exchange: {e}",
            )

    async def _ensure_access_token(self) -> APIResponse:
        """Ensure a valid access token is cached.

        Uses double-check locking to prevent concurrent refresh races.

        Returns:
            APIResponse indicating success or the error that occurred.
        """
        cache = await self._get_identity_cache()
        tc = cache.token_cache

        # Fast path: token is still valid
        if tc.is_valid():
            return APIResponse(success=True)

        # Slow path: need to refresh (per-subject lock)
        async with tc._lock:
            # Double-check after acquiring lock
            if tc.is_valid():
                return APIResponse(success=True)

            # Exchange cookie for token
            result = await self._exchange_cookie_for_token()
            if not result.success:
                return result

            # Cache the token
            token_data = result.data["token"]  # type: ignore[index, call-overload]
            tc.access_token = token_data["access_token"]
            expires_in = token_data.get("expires_in", 3600)
            tc.expires_at = time.time() + expires_in

            return APIResponse(success=True)

    async def _request(
        self,
        method: str,
        endpoint: str,
        json: dict[str, Any] | list[Any] | None = None,
        params: dict[str, Any] | None = None,
        _retry_on_401: bool = True,
    ) -> APIResponse:
        """Make an authenticated API request.

        Args:
            method: HTTP method (GET, POST, PUT, DELETE).
            endpoint: API endpoint (e.g., "/users/v3/user").
            json: JSON body for POST/PUT requests.
            params: Query parameters.
            _retry_on_401: Internal flag to prevent infinite retry loops.

        Returns:
            APIResponse with data or error.
        """
        await self._ensure_client()
        assert self._client is not None

        # Ensure we have a valid access token
        token_result = await self._ensure_access_token()
        if not token_result.success:
            return token_result

        await self._throttle()

        cache = await self._get_identity_cache()
        url = f"{self.base_url}{endpoint}"
        headers = self._get_headers(cache.token_cache.access_token)

        try:
            response = await self._client.request(
                method=method,
                url=url,
                headers=headers,
                json=json,
                params=params,
            )

            # Handle 401 with retry logic
            if response.status_code == 401 and _retry_on_401:
                # Token might have expired mid-request, clear and retry once
                cache.token_cache.clear()
                return await self._request(method, endpoint, json=json, params=params, _retry_on_401=False)

            return self._handle_response(response)

        except httpx.TimeoutException:
            return APIResponse(
                success=False,
                error_code=ErrorCode.NETWORK_ERROR,
                message="Request timed out. Check your network connection.",
            )
        except httpx.RequestError as e:
            return APIResponse(
                success=False,
                error_code=ErrorCode.NETWORK_ERROR,
                message=f"Network error: {e}",
            )

    def _handle_response(self, response: httpx.Response) -> APIResponse:
        """Handle API response and convert to APIResponse.

        Args:
            response: The httpx response.

        Returns:
            APIResponse with data or error.
        """
        if response.status_code == 200:
            try:
                data = response.json()
                return APIResponse(success=True, data=data)
            except Exception:
                return APIResponse(success=True, data=None)

        if response.status_code == 201:
            try:
                data = response.json()
                return APIResponse(success=True, data=data)
            except Exception:
                return APIResponse(success=True, data=None)

        if response.status_code == 204:
            return APIResponse(success=True, data=None)

        if response.status_code == 401:
            # Don't auto-clear - could be temporary. User can run 'tp-mcp auth-clear' if needed.
            return APIResponse(
                success=False,
                error_code=ErrorCode.AUTH_EXPIRED,
                message="Session expired or invalid. Run 'tp-mcp auth' to re-authenticate.",
            )

        if response.status_code == 403:
            return APIResponse(
                success=False,
                error_code=ErrorCode.AUTH_INVALID,
                message="Access denied. Check your permissions or re-authenticate.",
            )

        if response.status_code == 404:
            return APIResponse(
                success=False,
                error_code=ErrorCode.NOT_FOUND,
                message="Resource not found.",
            )

        if response.status_code == 429:
            return APIResponse(
                success=False,
                error_code=ErrorCode.RATE_LIMITED,
                message="Rate limited. Please wait before making more requests.",
            )

        # Generic error
        return APIResponse(
            success=False,
            error_code=ErrorCode.API_ERROR,
            message=f"API error: {response.status_code}",
        )

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> APIResponse:
        """Make a GET request.

        Args:
            endpoint: API endpoint.
            params: Query parameters.

        Returns:
            APIResponse.
        """
        return await self._request("GET", endpoint, params=params)

    async def post(self, endpoint: str, json: dict[str, Any] | list[Any] | None = None) -> APIResponse:
        """Make a POST request.

        Args:
            endpoint: API endpoint.
            json: JSON body.

        Returns:
            APIResponse.
        """
        return await self._request("POST", endpoint, json=json)

    async def put(self, endpoint: str, json: dict[str, Any] | list[Any] | None = None) -> APIResponse:
        """Make a PUT request.

        Args:
            endpoint: API endpoint.
            json: JSON body.

        Returns:
            APIResponse.
        """
        return await self._request("PUT", endpoint, json=json)

    async def delete(self, endpoint: str) -> APIResponse:
        """Make a DELETE request.

        Args:
            endpoint: API endpoint.

        Returns:
            APIResponse.
        """
        return await self._request("DELETE", endpoint)

    async def get_raw(self, endpoint: str, params: dict[str, Any] | None = None) -> RawResponse:
        """Make an authenticated GET request and return the raw binary response.

        Handles token refresh and retries on 401 exactly once, mirroring _request logic.

        Args:
            endpoint: API endpoint.
            params: Query parameters.

        Returns:
            RawResponse with binary content and headers, or error.
        """
        await self._ensure_client()
        assert self._client is not None

        token_result = await self._ensure_access_token()
        if not token_result.success:
            return RawResponse(
                success=False,
                error_code=token_result.error_code,
                message=token_result.message,
            )

        await self._throttle()

        cache = await self._get_identity_cache()
        url = f"{self.base_url}{endpoint}"
        headers = {**self._get_headers(cache.token_cache.access_token), "Accept": "*/*"}

        try:
            response = await self._client.request("GET", url=url, headers=headers, params=params)

            if response.status_code == 401:
                cache.token_cache.clear()
                token_result = await self._ensure_access_token()
                if not token_result.success:
                    return RawResponse(
                        success=False,
                        error_code=token_result.error_code,
                        message=token_result.message,
                    )
                await self._throttle()
                headers = {**self._get_headers(cache.token_cache.access_token), "Accept": "*/*"}
                response = await self._client.request("GET", url=url, headers=headers, params=params)

        except httpx.TimeoutException:
            return RawResponse(
                success=False,
                error_code=ErrorCode.NETWORK_ERROR,
                message="Request timed out. Check your network connection.",
            )
        except httpx.RequestError as e:
            return RawResponse(
                success=False,
                error_code=ErrorCode.NETWORK_ERROR,
                message=f"Network error: {e}",
            )

        if response.status_code == 401:
            return RawResponse(
                success=False,
                error_code=ErrorCode.AUTH_EXPIRED,
                message="Session expired or invalid. Run 'tp-mcp auth' to re-authenticate.",
            )
        if response.status_code == 404:
            return RawResponse(
                success=False,
                error_code=ErrorCode.NOT_FOUND,
                message="Resource not found.",
            )
        if response.status_code != 200:
            return RawResponse(
                success=False,
                error_code=ErrorCode.API_ERROR,
                message=f"API error: {response.status_code} - {response.text}",
            )

        return RawResponse(
            success=True,
            content=response.content,
            content_type=response.headers.get("Content-Type"),
            content_disposition=response.headers.get("Content-Disposition"),
        )

    @property
    def athlete_id(self) -> int | None:
        """Get the cached athlete ID."""
        return self._athlete_id

    @athlete_id.setter
    def athlete_id(self, value: int | None) -> None:
        """Set the athlete ID."""
        self._athlete_id = value

    async def _get_user_data(self) -> dict | None:
        """Get user data, using the per-subject cache to avoid redundant API calls."""
        cache = await self._get_identity_cache()
        if cache.user_data is not None:
            return cache.user_data

        async with cache.lock:
            if cache.user_data is not None:
                return cache.user_data
            response = await self.get("/users/v3/user")
            if not response.success or not response.data:
                return None
            cache.user_data = response.data.get("user", response.data)
            return cache.user_data

    async def ensure_athlete_id(self) -> int | None:
        """Get athlete ID, resolving coach athlete targeting via context var.

        For coach accounts, reads the athlete_override context variable to
        target a specific athlete by name or ID. When no override is set,
        resolves to the coach's own athlete entry.

        Caches per subject only when no athlete override is active.
        """
        from tp_mcp.client.context import athlete_override

        athlete = athlete_override.get()
        cache = await self._get_identity_cache()

        # Use cache only when no specific athlete is requested
        if athlete is None:
            if cache.athlete_id is not None:
                self._athlete_id = cache.athlete_id
                return cache.athlete_id

            if self._athlete_id is not None:
                cache.athlete_id = self._athlete_id
                return self._athlete_id

        user_data = await self._get_user_data()
        if not user_data:
            return None

        person_id = user_data.get("personId")
        athletes = user_data.get("athletes", [])

        athlete_id = None

        if athlete is not None:
            # Resolve by explicit athlete name or ID
            try:
                target_id = int(athlete)
                for a in athletes:
                    if a.get("athleteId") == target_id:
                        athlete_id = target_id
                        break
            except ValueError:
                # Search by name (case-insensitive), detecting ambiguity
                search = athlete.lower()
                matches = []
                for a in athletes:
                    first = (a.get("firstName") or "").lower()
                    last = (a.get("lastName") or "").lower()
                    full = f"{first} {last}"
                    if search in (first, last, full):
                        matches.append(a)

                if len(matches) == 1:
                    athlete_id = matches[0].get("athleteId")
                elif len(matches) > 1:
                    names = [
                        f"{a.get('firstName', '')} {a.get('lastName', '')} (ID: {a.get('athleteId')})"
                        for a in matches
                    ]
                    raise ValueError(
                        f"Ambiguous athlete name '{athlete}' matches {len(matches)} athletes: "
                        + ", ".join(names)
                        + ". Use full name or athlete ID to disambiguate."
                    ) from None
        elif athletes:
            # Default: find the coach's own athlete entry
            for a in athletes:
                if a.get("coachedBy") == person_id and a.get("email", "").lower() == user_data.get("email", "").lower():
                    athlete_id = a.get("athleteId")
                    break
            # Fallback: match by last name
            if not athlete_id:
                user_last = (user_data.get("lastName") or "").lower()
                for a in athletes:
                    if (a.get("lastName") or "").lower() == user_last and a.get("coachedBy") == person_id:
                        athlete_id = a.get("athleteId")
                        break
            # Final fallback: personId or first athlete entry (non-coach accounts)
            if not athlete_id:
                athlete_id = person_id or (athletes[0].get("athleteId") if athletes else None)
        else:
            athlete_id = person_id

        if athlete_id:
            self._athlete_id = athlete_id
            if athlete is None:
                cache.athlete_id = athlete_id

        return athlete_id

    async def test_token_exchange(self) -> dict[str, Any]:
        """Test the full token exchange flow for diagnostics.

        Returns:
            Dict with test results including success status and step details.
        """
        result: dict[str, Any] = {"success": False, "step": "init", "details": {}}

        # Step 1: Check credential
        cred = get_credential()
        if not cred.success or not cred.cookie:
            result["step"] = "credential_check"
            result["error"] = "No credential stored. Run 'tp-mcp auth' to authenticate."
            return result

        result["details"]["has_credential"] = True

        # Step 2: Exchange cookie for token
        exchange_result = await self._exchange_cookie_for_token()
        if not exchange_result.success:
            result["step"] = "token_exchange"
            result["error"] = exchange_result.message
            result["error_code"] = exchange_result.error_code.value if exchange_result.error_code else None
            return result

        result["details"]["token_exchange"] = "success"
        token_data = exchange_result.data["token"]  # type: ignore[index, call-overload]
        result["details"]["expires_in"] = token_data.get("expires_in")

        # Step 3: Verify token structure
        if "access_token" not in token_data:
            result["step"] = "token_validation"
            result["error"] = "Token response missing access_token"
            return result

        result["details"]["token_valid"] = True

        # Step 4: Make test API call
        test_response = await self.get("/users/v3/user")
        if not test_response.success:
            result["step"] = "api_test"
            result["error"] = test_response.message
            result["error_code"] = test_response.error_code.value if test_response.error_code else None
            return result

        result["details"]["api_test"] = "success"
        if isinstance(test_response.data, dict):
            result["details"]["user_id"] = test_response.data.get("Id")

        result["success"] = True
        result["step"] = "complete"
        return result
