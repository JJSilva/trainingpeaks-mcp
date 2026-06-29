# Deploying TrainingPeaks MCP on Railway

This server normally runs over **stdio** (`tp-mcp serve`) for local clients like
Claude Desktop. To run it as a hosted, network-reachable service it also ships a
**Streamable HTTP** transport (`tp-mcp serve-http`), which is what Railway runs.

The HTTP server:
- listens on `$PORT` (Railway sets this automatically),
- serves the MCP endpoint at **`/mcp`**,
- exposes a health check at **`/health`**,
- reads your TrainingPeaks auth from the **`TP_AUTH_COOKIE`** environment variable.

## 1. Get your TrainingPeaks cookie

1. Log in at https://app.trainingpeaks.com
2. Open DevTools (F12) → Application → Cookies → `app.trainingpeaks.com`
3. Copy the value of the **`Production_tpAuth`** cookie

> Note: this cookie expires periodically; when it does, update the
> `TP_AUTH_COOKIE` variable in Railway and redeploy/restart.

## 2. Deploy on Railway

1. Go to https://railway.app → **New Project** → **Deploy from GitHub repo**
2. Select **`JJSilva/trainingpeaks-mcp`** (authorize Railway for your GitHub if prompted)
3. Railway detects `railway.json` + `Dockerfile` and builds the image
4. In the service's **Variables** tab, add:
   - `TP_AUTH_COOKIE` = the `Production_tpAuth` value from step 1
5. In **Settings → Networking**, click **Generate Domain** to get a public URL
6. Deploy. Your MCP endpoint will be:

   ```
   https://<your-app>.up.railway.app/mcp
   ```

Verify it's up by visiting `https://<your-app>.up.railway.app/health` — it should
return `{"status":"ok",...}`.

## 3. Connect a client

Point any Streamable-HTTP MCP client at `https://<your-app>.up.railway.app/mcp`.

## Run the HTTP server locally (optional)

```bash
pip install .
PORT=8000 TP_AUTH_COOKIE="<your-cookie>" tp-mcp serve-http
# -> http://localhost:8000/mcp  (health: http://localhost:8000/health)
```

## Security note

Anyone who can reach the public URL can call your TrainingPeaks account through
the MCP tools — there is **no authentication on the HTTP endpoint itself**, only
the upstream TrainingPeaks cookie. Treat the Railway URL as a secret, or put an
authenticating proxy in front of it if you need stronger protection.
