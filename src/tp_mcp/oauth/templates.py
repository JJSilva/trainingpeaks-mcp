"""Server-rendered HTML for the TrainingPeaks sign-in pages (no template engine)."""

from html import escape

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
  background: #0f172a; color: #e2e8f0; padding: 24px;
}
.card {
  width: 100%; max-width: 400px; background: #1e293b; border: 1px solid #334155;
  border-radius: 14px; padding: 28px; box-shadow: 0 10px 40px rgba(0,0,0,.35);
}
h1 { font-size: 1.15rem; margin: 0 0 4px; }
p.sub { margin: 0 0 20px; color: #94a3b8; font-size: .85rem; }
label { display: block; font-size: .8rem; margin: 14px 0 6px; color: #cbd5e1; }
input, select {
  width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #475569;
  background: #0f172a; color: #e2e8f0; font-size: .95rem;
}
button {
  width: 100%; margin-top: 20px; padding: 11px; border: 0; border-radius: 8px;
  background: #2563eb; color: #fff; font-size: .95rem; font-weight: 600; cursor: pointer;
}
button:hover { background: #1d4ed8; }
.err { background: #7f1d1d; color: #fecaca; border-radius: 8px; padding: 10px 12px;
       font-size: .85rem; margin-bottom: 14px; }
details { margin-top: 18px; border-top: 1px solid #334155; padding-top: 14px; }
summary { cursor: pointer; color: #94a3b8; font-size: .82rem; }
.hint { color: #64748b; font-size: .75rem; margin-top: 6px; line-height: 1.4; }
"""


def _page(body: str, title: str = "Sign in to TrainingPeaks") -> str:
    return (
        f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head><body>{body}</body></html>"
    )


def login_page(login_session: str, error: str | None = None) -> str:
    err = f"<div class='err'>{escape(error)}</div>" if error else ""
    ls = escape(login_session)
    body = f"""
    <div class="card">
      <h1>Connect TrainingPeaks</h1>
      <p class="sub">Sign in with your TrainingPeaks account to authorize this app.</p>
      {err}
      <form method="post" action="/tp-login">
        <input type="hidden" name="login_session" value="{ls}">
        <label for="email">Email</label>
        <input id="email" name="email" type="email" autocomplete="username" required>
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
        <button type="submit">Sign in</button>
      </form>
      <details>
        <summary>Can't sign in? Paste a cookie instead</summary>
        <p class="hint">If sign-in is blocked (e.g. a CAPTCHA), log in at
          app.trainingpeaks.com, open DevTools &rarr; Application &rarr; Cookies, and
          copy the <code>Production_tpAuth</code> value.</p>
        <form method="post" action="/tp-login">
          <input type="hidden" name="login_session" value="{ls}">
          <label for="cookie">Production_tpAuth cookie</label>
          <input id="cookie" name="cookie" type="password" autocomplete="off">
          <button type="submit">Use cookie</button>
        </form>
      </details>
    </div>
    """
    return _page(body)


def mfa_page(login_session: str, methods: list[str], error: str | None = None) -> str:
    err = f"<div class='err'>{escape(error)}</div>" if error else ""
    ls = escape(login_session)
    if methods:
        opts = "".join(f"<option value='{escape(m)}'>{escape(m)}</option>" for m in methods)
        selector = f"<label for='method'>Method</label><select id='method' name='method'>{opts}</select>"
    else:
        selector = ""
    body = f"""
    <div class="card">
      <h1>Two-factor authentication</h1>
      <p class="sub">Enter the verification code for your TrainingPeaks account.</p>
      {err}
      <form method="post" action="/tp-login/mfa">
        <input type="hidden" name="login_session" value="{ls}">
        {selector}
        <label for="code">Verification code</label>
        <input id="code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" required>
        <button type="submit">Verify</button>
      </form>
    </div>
    """
    return _page(body, title="Two-factor authentication")


def message_page(title: str, message: str) -> str:
    body = f"""
    <div class="card">
      <h1>{escape(title)}</h1>
      <p class="sub">{escape(message)}</p>
    </div>
    """
    return _page(body, title=title)
