# Deploying TrainingPeaks MCP on Railway

This server normally runs over **stdio** (`tp-mcp serve`) for local clients like
Claude Desktop. To run it as a hosted, network-reachable service it also ships a
**Streamable HTTP** transport (`tp-mcp serve-http`), which is what Railway runs.

There are two ways to run the HTTP server:

- **Multi-user OAuth sign-in (recommended).** Each user connects their MCP client
  and clicks *Sign in*, entering their own TrainingPeaks email & password (with
  MFA). The server logs into TrainingPeaks, stores that user's cookie encrypted,
  and issues the client its own tokens. No cookie wrangling, and it re-challenges
  automatically when tokens expire.
- **Single-user (legacy).** One shared account via the `TP_AUTH_COOKIE` env var.
  The `/mcp` endpoint is then **unauthenticated** — treat the URL as a secret.

The HTTP server:
- listens on `$PORT` (Railway sets this automatically),
- serves the MCP endpoint at **`/mcp`**,
- exposes a health check at **`/health`**,
- in OAuth mode, serves `/.well-known/oauth-authorization-server`,
  `/.well-known/oauth-protected-resource`, `/authorize`, `/token`, `/register`,
  `/revoke`, and the interactive `/tp-login` pages.

---

## Option A — Multi-user OAuth sign-in (recommended)

### 1. Deploy on Railway

1. Go to https://railway.app → **New Project** → **Deploy from GitHub repo**
2. Select your fork; Railway detects `railway.json` + `Dockerfile` and builds.
3. In **Settings → Networking**, click **Generate Domain** to get a public URL,
   e.g. `https://<your-app>.up.railway.app`.
4. Attach a **persistent volume** (Railway → your service → **Volumes**), mounted
   at e.g. `/data`. This holds the encrypted token/cookie database so users stay
   signed in across redeploys.

### 2. Configure environment variables

In the service's **Variables** tab:

| Variable | Required | Value |
|---|---|---|
| `TP_MCP_PUBLIC_URL` | yes | Your generated domain, e.g. `https://<your-app>.up.railway.app` (https, no trailing slash) |
| `TP_MCP_TOKEN_SECRET` | yes | A long random secret. Keep it **stable** — changing it invalidates all issued tokens. Generate: `python -c "import secrets; print(secrets.token_urlsafe(48))"` |
| `TP_MCP_DB_PATH` | recommended | Path on the persistent volume, e.g. `/data/tp_mcp.db` |
| `TP_MCP_ENC_KEY` | recommended | Base64 32-byte at-rest key for stored cookies. Generate: `python -c "import base64,os; print(base64.b64encode(os.urandom(32)).decode())"` |
| `TP_MCP_ACCESS_TTL` | optional | Access-token lifetime in seconds (default 3600) |
| `TP_MCP_REFRESH_TTL` | optional | Refresh-token lifetime in seconds (default 60 days) |

> Do **not** set `TP_AUTH_COOKIE` in OAuth mode. OAuth is enabled automatically
> when `TP_MCP_PUBLIC_URL` is set and `TP_AUTH_COOKIE` is not (or force it with
> `TP_MCP_AUTH_ENABLED=true`).

### 3. Connect a client

Point any OAuth-capable Streamable-HTTP MCP client at
`https://<your-app>.up.railway.app/mcp`. The client discovers the authorization
server, opens the sign-in page, and the user logs in with their TrainingPeaks
email & password (completing MFA if enabled). If the automated sign-in is blocked
by a CAPTCHA, the page offers a fallback to paste the `Production_tpAuth` cookie.

**Expiry / re-challenge.** Access tokens refresh automatically via the client's
refresh token. If the underlying TrainingPeaks session expires, tools return an
auth error and the user simply signs in again from their client.

---

## Option B — Single-user (legacy `TP_AUTH_COOKIE`)

### 1. Get your TrainingPeaks cookie

1. Log in at https://app.trainingpeaks.com
2. Open DevTools (F12) → Application → Cookies → `app.trainingpeaks.com`
3. Copy the value of the **`Production_tpAuth`** cookie

> This cookie expires periodically; when it does, update the `TP_AUTH_COOKIE`
> variable in Railway and redeploy/restart.

### 2. Deploy

Same as above, then in **Variables** add only:
- `TP_AUTH_COOKIE` = the `Production_tpAuth` value

OAuth stays **off** and the `/mcp` endpoint is unauthenticated.

> Security: anyone who can reach the public URL can call your TrainingPeaks
> account. Treat the URL as a secret, or use Option A.

---

## Run the HTTP server locally (optional)

OAuth mode:

```bash
pip install .
PORT=8000 \
TP_MCP_PUBLIC_URL="http://localhost:8000" \
TP_MCP_TOKEN_SECRET="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')" \
TP_MCP_DB_PATH="$PWD/tp_mcp.db" \
tp-mcp serve-http
# MCP endpoint: http://localhost:8000/mcp  (health: /health)
```

Single-user mode:

```bash
PORT=8000 TP_AUTH_COOKIE="<your-cookie>" tp-mcp serve-http
```
