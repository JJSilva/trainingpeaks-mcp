"""Tests for the TrainingPeaks email/password login driver.

These exercise the HTML parsing and response classification without any network
access (httpx clients are constructed only to hold cookie jars).
"""

import httpx
import pytest

from tp_mcp.auth import tp_login as tpl

LOGIN_HTML = """
<form action="https://home.trainingpeaks.com/login" method="post">
  <input name="__RequestVerificationToken" type="hidden" value="TOKEN-ABC">
  <input name="Username"><input name="Password" type="password">
  <select name="SelectedMfaMethod"></select>
</form>
"""

MFA_HTML = """
<form action="https://home.trainingpeaks.com/login" method="post">
  <input name="__RequestVerificationToken" type="hidden" value="TOKEN-MFA">
  <select name="SelectedMfaMethod">
    <option value="sms">Text message</option>
    <option value="totp">Authenticator app</option>
  </select>
  <input name="Code" type="text" autocomplete="one-time-code">
</form>
"""

CAPTCHA_HTML = "<html><body>Please verify you are human before continuing.</body></html>"
BADCREDS_HTML = "<html><body><span>Invalid username or password.</span></body></html>"


def test_parse_hidden_token():
    p = tpl._parse_page(LOGIN_HTML)
    assert p.verification_token == "TOKEN-ABC"


def test_parse_mfa_page():
    p = tpl._parse_page(MFA_HTML)
    assert p.verification_token == "TOKEN-MFA"
    assert p.mfa_methods == ["sms", "totp"]
    assert p.has_code_input is True


def test_classify_success_by_cookie():
    client = httpx.AsyncClient()
    client.cookies.set("Production_tpAuth", "COOKIEVAL", domain=".trainingpeaks.com")
    resp = httpx.Response(200, text=LOGIN_HTML, request=httpx.Request("POST", "https://home.trainingpeaks.com/login"))
    outcome, _ = tpl._classify(client, resp)
    assert outcome == tpl.LoginOutcome.SUCCESS
    assert tpl._extract_auth_cookie(client) == "COOKIEVAL"


def test_classify_mfa():
    client = httpx.AsyncClient()
    resp = httpx.Response(200, text=MFA_HTML, request=httpx.Request("POST", "https://home.trainingpeaks.com/login"))
    outcome, parser = tpl._classify(client, resp)
    assert outcome == tpl.LoginOutcome.MFA_REQUIRED
    assert parser.mfa_methods == ["sms", "totp"]


def test_classify_captcha():
    client = httpx.AsyncClient()
    resp = httpx.Response(200, text=CAPTCHA_HTML, request=httpx.Request("POST", "https://home.trainingpeaks.com/login"))
    outcome, _ = tpl._classify(client, resp)
    assert outcome == tpl.LoginOutcome.CAPTCHA_REQUIRED


def test_classify_bad_credentials():
    client = httpx.AsyncClient()
    resp = httpx.Response(200, text=BADCREDS_HTML, request=httpx.Request("POST", "https://home.trainingpeaks.com/login"))
    outcome, _ = tpl._classify(client, resp)
    assert outcome == tpl.LoginOutcome.BAD_CREDENTIALS


@pytest.mark.asyncio
async def test_login_requires_credentials():
    r = await tpl.login("", "")
    assert r.outcome == tpl.LoginOutcome.BAD_CREDENTIALS


@pytest.mark.asyncio
async def test_submit_mfa_requires_code():
    r = await tpl.submit_mfa(tpl.MfaState(username="u"), "")
    assert r.outcome == tpl.LoginOutcome.BAD_CREDENTIALS


def test_secrets_not_in_repr():
    r = tpl.LoginResult(tpl.LoginOutcome.SUCCESS, cookie="topsecret", message="ok")
    assert "topsecret" not in repr(r)
    state = tpl.MfaState(cookies={"x": "y"}, verification_token="tok", username="me")
    assert "tok" not in repr(state)


@pytest.mark.asyncio
async def test_login_success_via_mock(monkeypatch):
    """A full login() call classified as success without real network."""

    async def fake_get(self, url, *a, **k):
        return httpx.Response(200, text=LOGIN_HTML, request=httpx.Request("GET", url))

    async def fake_post(self, url, *a, **k):
        # Simulate TP setting the auth cookie on the jar.
        self.cookies.set("Production_tpAuth", "FRESH", domain=".trainingpeaks.com")
        return httpx.Response(200, text="<html>ok</html>", request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)

    r = await tpl.login("user@example.com", "pw")
    assert r.outcome == tpl.LoginOutcome.SUCCESS
    assert r.cookie == "FRESH"
