"""Programmatic TrainingPeaks email/password login.

Obtains the ``Production_tpAuth`` cookie by driving the same login form a browser
uses at ``https://home.trainingpeaks.com/login``. This is a single, isolated
module so the (reverse-engineered, therefore fragile) login mechanics live in one
place.

Flow:
1. ``GET /login`` -> captures the HttpOnly ``__RequestVerificationToken`` cookie
   and the matching hidden form field (ASP.NET anti-forgery double-submit).
2. ``POST /login`` with ``Username`` / ``Password`` / the token. On success
   TrainingPeaks issues the ``Production_tpAuth`` cookie on ``.trainingpeaks.com``.
3. If the account has MFA enabled TrainingPeaks returns a challenge page instead;
   :func:`submit_mfa` completes it with the user's code.

CAVEAT (reCAPTCHA v3): the login page loads Google reCAPTCHA v3 (invisible,
score-based). A headless POST cannot mint a valid reCAPTCHA token, so depending on
TrainingPeaks' score enforcement an automated login may be rejected. When that (or
any other bot check) happens we return :attr:`LoginOutcome.CAPTCHA_REQUIRED` so the
caller can fall back to manual cookie entry. This is why manual cookie paste remains
a first-class fallback in both the CLI and the hosted login page.

SECURITY:
- Password / MFA code / cookie / anti-forgery token are NEVER logged and are held
  in ``repr=False`` fields with custom ``__repr__`` (mirrors ``auth/browser.py``).
- Error messages use exception *type* only, never response bodies.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from enum import Enum
from html.parser import HTMLParser

import httpx

TP_LOGIN_BASE = "https://home.trainingpeaks.com"
LOGIN_PATH = "/login"
RVT_FIELD = "__RequestVerificationToken"
AUTH_COOKIE = "Production_tpAuth"
LOGIN_TIMEOUT = 30.0
# A realistic desktop UA; TrainingPeaks rejects obvious bot user agents.
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class LoginOutcome(Enum):
    """Classification of a login attempt."""

    SUCCESS = "success"
    MFA_REQUIRED = "mfa_required"
    BAD_CREDENTIALS = "bad_credentials"
    CAPTCHA_REQUIRED = "captcha_required"
    NETWORK_ERROR = "network_error"
    UNKNOWN = "unknown"


@dataclass
class MfaState:
    """Opaque continuation state for a two-step (MFA) login.

    Carries everything :func:`submit_mfa` needs to resume the flow across a
    separate request. Never logged.
    """

    cookies: dict[str, str] = field(default_factory=dict, repr=False)
    verification_token: str | None = field(default=None, repr=False)
    username: str | None = field(default=None, repr=False)
    available_methods: list[str] = field(default_factory=list)
    selected_method: str | None = None
    attempts: int = 0

    def __repr__(self) -> str:
        return (
            f"MfaState(methods={self.available_methods}, "
            f"selected={self.selected_method!r}, attempts={self.attempts})"
        )


@dataclass
class LoginResult:
    """Result of a login (or MFA) attempt.

    ``cookie`` holds the ``Production_tpAuth`` value only on
    :attr:`LoginOutcome.SUCCESS`.
    """

    outcome: LoginOutcome
    cookie: str | None = field(default=None, repr=False)
    mfa_state: MfaState | None = None
    message: str = ""

    @property
    def success(self) -> bool:
        return self.outcome == LoginOutcome.SUCCESS

    def __repr__(self) -> str:
        cookie_status = "present" if self.cookie else "None"
        return (
            f"LoginResult(outcome={self.outcome.value}, cookie=<{cookie_status}>, "
            f"message={self.message!r})"
        )


class _LoginFormParser(HTMLParser):
    """Extract signals we need from a TrainingPeaks login/MFA page.

    Collected:
    - the hidden ``__RequestVerificationToken`` value (rotates per page),
    - ``<option>`` values inside a ``SelectedMfaMethod`` ``<select>`` (MFA methods),
    - whether a visible MFA/verification-code input is present.
    """

    def __init__(self) -> None:
        super().__init__()
        self.verification_token: str | None = None
        self.mfa_methods: list[str] = []
        self.has_code_input = False
        self._in_mfa_select = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        name = a.get("name", "")
        if tag == "input":
            if name == RVT_FIELD and a.get("value"):
                self.verification_token = a["value"]
            # A verification-code input appears only on the MFA challenge page.
            itype = a.get("type", "").lower()
            lname = name.lower()
            if ("code" in lname or "otp" in lname or a.get("autocomplete") == "one-time-code") and itype != "hidden":
                self.has_code_input = True
        elif tag == "select" and name == "SelectedMfaMethod":
            self._in_mfa_select = True
        elif tag == "option" and self._in_mfa_select:
            val = a.get("value")
            if val:
                self.mfa_methods.append(val)

    def handle_endtag(self, tag: str) -> None:
        if tag == "select":
            self._in_mfa_select = False


def _parse_page(html: str) -> _LoginFormParser:
    parser = _LoginFormParser()
    with contextlib.suppress(Exception):
        parser.feed(html)
    return parser


def _cookie_jar_snapshot(client: httpx.AsyncClient) -> dict[str, str]:
    """Snapshot the cookie jar as a plain dict for serialization into MfaState."""
    return {c.name: c.value for c in client.cookies.jar if c.value is not None}


def _extract_auth_cookie(client: httpx.AsyncClient) -> str | None:
    for c in client.cookies.jar:
        if c.name == AUTH_COOKIE and c.value:
            return c.value
    return None


def _looks_like_captcha_block(html: str) -> bool:
    """Heuristic: TrainingPeaks blocked us with a bot/captcha challenge.

    The login page always *loads* reCAPTCHA v3, so mere presence of the script is
    not a block. We look for explicit block/too-many-attempts wording instead.
    """
    lowered = html.lower()
    markers = (
        "verify you are human",
        "are you a robot",
        "unusual activity",
        "too many attempts",
        "temporarily locked",
        "captcha verification failed",
        "please complete the captcha",
    )
    return any(m in lowered for m in markers)


def _looks_like_bad_credentials(html: str) -> bool:
    lowered = html.lower()
    markers = (
        "invalid username or password",
        "incorrect username or password",
        "the username or password",
        "login failed",
        "invalid login",
    )
    return any(m in lowered for m in markers)


def _classify(client: httpx.AsyncClient, response: httpx.Response) -> tuple[LoginOutcome, _LoginFormParser]:
    """Classify a post-login response.

    Priority: cookie (success) > MFA challenge > captcha block > bad creds > unknown.
    """
    parser = _parse_page(response.text)

    # 1. Most reliable success signal: the auth cookie is now in the jar.
    if _extract_auth_cookie(client) is not None:
        return LoginOutcome.SUCCESS, parser

    # 2. MFA challenge: a visible code input and/or method selector, and we're
    #    no longer on a page still asking for the password.
    on_mfa_url = "mfa" in str(response.url).lower()
    if parser.has_code_input or on_mfa_url or (parser.mfa_methods and "/login" not in str(response.url).lower()):
        return LoginOutcome.MFA_REQUIRED, parser

    # 3. Bot / captcha block.
    if _looks_like_captcha_block(response.text):
        return LoginOutcome.CAPTCHA_REQUIRED, parser

    # 4. Explicit invalid-credentials messaging.
    if _looks_like_bad_credentials(response.text):
        return LoginOutcome.BAD_CREDENTIALS, parser

    return LoginOutcome.UNKNOWN, parser


async def _fetch_login_page(client: httpx.AsyncClient) -> str | None:
    """GET the login page; returns the hidden anti-forgery token (or None)."""
    resp = await client.get(f"{TP_LOGIN_BASE}{LOGIN_PATH}")
    parser = _parse_page(resp.text)
    return parser.verification_token


def _build_form(
    *,
    username: str,
    password: str,
    token: str | None,
    selected_mfa_method: str | None,
    attempts: int,
    code: str | None = None,
) -> dict[str, str]:
    form = {
        "Username": username,
        "Password": password,
        RVT_FIELD: token or "",
        "Attempts": str(attempts),
        "CaptchaHidden": "",
        "CaptchaToken": "",
        "SelectedMfaMethod": selected_mfa_method or "",
        "submit": "submit",
    }
    if code is not None:
        # Field name is best-effort; TrainingPeaks' MFA input name may differ and
        # can be adjusted once a real MFA capture is available.
        form["Code"] = code
        form["MfaCode"] = code
    return form


def _new_client(client: httpx.AsyncClient | None) -> tuple[httpx.AsyncClient, bool]:
    """Return (client, owns_client). Owned clients must be closed by the caller."""
    if client is not None:
        return client, False
    return (
        httpx.AsyncClient(
            timeout=LOGIN_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ),
        True,
    )


async def login(
    username: str,
    password: str,
    *,
    client: httpx.AsyncClient | None = None,
    mfa_method: str | None = None,
) -> LoginResult:
    """Attempt an email/password login against TrainingPeaks.

    Args:
        username: TrainingPeaks account email.
        password: Account password (used once; never stored or logged).
        client: Optional shared ``httpx.AsyncClient`` (reuses one cookie jar across
            ``login`` -> ``submit_mfa``). One is created and closed otherwise.
        mfa_method: Optional pre-selected MFA method to hint the server with.

    Returns:
        LoginResult. On :attr:`LoginOutcome.MFA_REQUIRED`, ``mfa_state`` is set for
        a follow-up :func:`submit_mfa` call.
    """
    if not username or not password:
        return LoginResult(LoginOutcome.BAD_CREDENTIALS, message="Email and password are required.")

    http, owns = _new_client(client)
    try:
        token = await _fetch_login_page(http)
        form = _build_form(
            username=username,
            password=password,
            token=token,
            selected_mfa_method=mfa_method,
            attempts=0,
        )
        resp = await http.post(f"{TP_LOGIN_BASE}{LOGIN_PATH}", data=form)
        outcome, parser = _classify(http, resp)

        if outcome == LoginOutcome.SUCCESS:
            return LoginResult(LoginOutcome.SUCCESS, cookie=_extract_auth_cookie(http), message="Login successful.")

        if outcome == LoginOutcome.MFA_REQUIRED:
            state = MfaState(
                cookies=_cookie_jar_snapshot(http),
                verification_token=parser.verification_token or token,
                username=username,
                available_methods=parser.mfa_methods,
                selected_method=mfa_method or (parser.mfa_methods[0] if parser.mfa_methods else None),
                attempts=1,
            )
            return LoginResult(
                LoginOutcome.MFA_REQUIRED,
                mfa_state=state,
                message="Multi-factor authentication required.",
            )

        if outcome == LoginOutcome.CAPTCHA_REQUIRED:
            return LoginResult(
                LoginOutcome.CAPTCHA_REQUIRED,
                message=(
                    "TrainingPeaks blocked the automated sign-in with a CAPTCHA / bot check. "
                    "Sign in via your browser and paste the Production_tpAuth cookie instead."
                ),
            )

        if outcome == LoginOutcome.BAD_CREDENTIALS:
            return LoginResult(LoginOutcome.BAD_CREDENTIALS, message="Invalid email or password.")

        return LoginResult(
            LoginOutcome.UNKNOWN,
            message=f"Could not determine login result (HTTP {resp.status_code}).",
        )

    except httpx.TimeoutException:
        return LoginResult(LoginOutcome.NETWORK_ERROR, message="Login request timed out.")
    except httpx.RequestError as e:
        return LoginResult(LoginOutcome.NETWORK_ERROR, message=f"Network error during login ({type(e).__name__}).")
    finally:
        if owns:
            await http.aclose()


async def submit_mfa(
    state: MfaState,
    code: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> LoginResult:
    """Complete an MFA challenge started by :func:`login`.

    Args:
        state: The :class:`MfaState` returned by :func:`login`.
        code: The one-time verification code entered by the user.
        client: Optional shared client. If omitted, a fresh client is seeded from
            ``state.cookies`` so the anti-forgery / session cookies carry over.

    Returns:
        LoginResult with the cookie on success, or an error outcome.
    """
    if not code or not code.strip():
        return LoginResult(LoginOutcome.BAD_CREDENTIALS, message="Verification code is required.")

    http, owns = _new_client(client)
    try:
        # Seed the jar from the snapshot when we don't share a live client.
        if owns:
            for name, value in state.cookies.items():
                http.cookies.set(name, value, domain=".trainingpeaks.com")

        form = _build_form(
            username=state.username or "",
            password="",  # not re-sent on the MFA step
            token=state.verification_token,
            selected_mfa_method=state.selected_method,
            attempts=state.attempts + 1,
            code=code.strip(),
        )
        resp = await http.post(f"{TP_LOGIN_BASE}{LOGIN_PATH}", data=form)
        outcome, _ = _classify(http, resp)

        if outcome == LoginOutcome.SUCCESS:
            return LoginResult(LoginOutcome.SUCCESS, cookie=_extract_auth_cookie(http), message="Login successful.")
        if outcome == LoginOutcome.CAPTCHA_REQUIRED:
            return LoginResult(
                LoginOutcome.CAPTCHA_REQUIRED,
                message="TrainingPeaks blocked the sign-in with a CAPTCHA / bot check.",
            )
        # Anything else at this stage means the code was wrong/expired.
        return LoginResult(
            LoginOutcome.BAD_CREDENTIALS,
            message="Invalid or expired verification code.",
        )

    except httpx.TimeoutException:
        return LoginResult(LoginOutcome.NETWORK_ERROR, message="MFA request timed out.")
    except httpx.RequestError as e:
        return LoginResult(LoginOutcome.NETWORK_ERROR, message=f"Network error during MFA ({type(e).__name__}).")
    finally:
        if owns:
            await http.aclose()
