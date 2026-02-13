from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter()


@router.get("/", include_in_schema=False)
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=307)


@router.get("/login", include_in_schema=False, response_class=HTMLResponse)
async def login_page() -> HTMLResponse:
    html = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Account Login</title>
    <style>
      :root {
        --bg: #0f172a;
        --card: #111827;
        --muted: #94a3b8;
        --text: #e2e8f0;
        --accent: #22c55e;
        --danger: #ef4444;
        --line: #1f2937;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        font-family: "Segoe UI", Arial, sans-serif;
        background: radial-gradient(1200px 700px at 20% -20%, #1e293b, var(--bg));
        color: var(--text);
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 24px;
      }
      .card {
        width: 100%;
        max-width: 480px;
        background: linear-gradient(180deg, #111827, #0b1220);
        border: 1px solid var(--line);
        border-radius: 14px;
        padding: 22px;
      }
      h1 { margin: 0 0 8px; font-size: 1.5rem; }
      p { margin: 0 0 18px; color: var(--muted); }
      label { display: block; margin: 10px 0 6px; font-size: 0.92rem; }
      input {
        width: 100%;
        border: 1px solid #334155;
        background: #0b1220;
        color: var(--text);
        border-radius: 10px;
        padding: 10px 12px;
      }
      button {
        width: 100%;
        margin-top: 14px;
        border: 0;
        background: var(--accent);
        color: #052e16;
        border-radius: 10px;
        padding: 10px 12px;
        cursor: pointer;
        font-weight: 700;
      }
      .secondary {
        background: transparent;
        color: var(--text);
        border: 1px solid #334155;
      }
      .status {
        margin-top: 12px;
        padding: 10px;
        border-radius: 10px;
        background: #0b1220;
        border: 1px solid #334155;
        white-space: pre-wrap;
      }
      .error { border-color: var(--danger); }
      .meta { margin-top: 12px; font-size: 0.85rem; color: var(--muted); }
      a { color: #7dd3fc; }
    </style>
  </head>
  <body>
    <main class="card">
      <h1>Sign In</h1>
      <p>Local starter login for your FastAPI auth service.</p>

      <label for="email">Email</label>
      <input id="email" type="email" placeholder="you@example.com" />

      <label for="password">Password</label>
      <input id="password" type="password" placeholder="Your password" />

      <button id="loginBtn" type="button">Login</button>
      <button id="meBtn" type="button" class="secondary">Fetch /me</button>
      <button id="googleBtn" type="button" class="secondary">Continue with Google</button>
      <button id="msBtn" type="button" class="secondary">Continue with Microsoft</button>

      <div id="status" class="status">Ready.</div>
      <p class="meta">
        API docs: <a href="/api/v1/docs" target="_blank" rel="noreferrer">/api/v1/docs</a><br />
        Note: login requires verified email.<br />
        OAuth callback paths:
        <code>/login/oauth/google/callback</code>,
        <code>/login/oauth/microsoft/callback</code>
      </p>
    </main>
    <script>
      const statusEl = document.getElementById("status");
      let accessToken = "";

      function setStatus(message, isError = false) {
        statusEl.textContent = message;
        statusEl.classList.toggle("error", isError);
      }

      async function parseResponse(res) {
        let body = null;
        try { body = await res.json(); } catch (_) {}
        return { ok: res.ok, status: res.status, body };
      }

      async function startOAuth(provider) {
        setStatus("Starting " + provider + " OAuth...");
        const res = await fetch("/api/v1/oauth/" + provider + "/authorize");
        const result = await parseResponse(res);
        if (!result.ok || !result.body?.authorization_url) {
          const msg = result.body?.error?.message || JSON.stringify(result.body) || "OAuth authorize failed";
          setStatus("OAuth failed (" + result.status + "): " + msg, true);
          return;
        }
        window.location.href = result.body.authorization_url;
      }

      document.getElementById("loginBtn").addEventListener("click", async () => {
        const email = document.getElementById("email").value.trim();
        const password = document.getElementById("password").value;
        if (!email || !password) {
          setStatus("Enter email and password.", true);
          return;
        }
        setStatus("Logging in...");
        const res = await fetch("/api/v1/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password })
        });
        const result = await parseResponse(res);
        if (!result.ok) {
          const msg = result.body?.error?.message || JSON.stringify(result.body) || "Login failed";
          setStatus("Login failed (" + result.status + "): " + msg, true);
          return;
        }
        accessToken = result.body.access_token || "";
        setStatus("Login successful. Access token captured in memory.");
      });

      document.getElementById("meBtn").addEventListener("click", async () => {
        if (!accessToken) {
          setStatus("Login first.", true);
          return;
        }
        setStatus("Loading profile...");
        const res = await fetch("/api/v1/me", {
          headers: { Authorization: "Bearer " + accessToken }
        });
        const result = await parseResponse(res);
        if (!result.ok) {
          const msg = result.body?.error?.message || JSON.stringify(result.body) || "Request failed";
          setStatus("Fetch /me failed (" + result.status + "): " + msg, true);
          return;
        }
        setStatus("Profile:\\n" + JSON.stringify(result.body, null, 2));
      });

      document.getElementById("googleBtn").addEventListener("click", async () => startOAuth("google"));
      document.getElementById("msBtn").addEventListener("click", async () => startOAuth("microsoft"));
    </script>
  </body>
</html>
"""
    return HTMLResponse(content=html)


@router.get("/login/oauth/{provider}/callback", include_in_schema=False, response_class=HTMLResponse)
async def oauth_callback_page(provider: str) -> HTMLResponse:
    provider = provider.lower().strip()
    if provider not in {"google", "microsoft"}:
        raise HTTPException(status_code=404, detail="OAuth provider page not found")

    html = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>OAuth Callback</title>
    <style>
      body {
        margin: 0;
        min-height: 100vh;
        display: grid;
        place-items: center;
        font-family: "Segoe UI", Arial, sans-serif;
        background: #0f172a;
        color: #e2e8f0;
      }
      .card {
        width: 100%;
        max-width: 640px;
        border: 1px solid #334155;
        border-radius: 12px;
        padding: 20px;
        background: #111827;
      }
      pre {
        white-space: pre-wrap;
        background: #0b1220;
        border: 1px solid #334155;
        border-radius: 8px;
        padding: 10px;
      }
      a { color: #7dd3fc; }
    </style>
  </head>
  <body>
    <main class="card">
      <h2>OAuth Callback: __PROVIDER__</h2>
      <pre id="status">Processing...</pre>
      <p><a href="/login">Back to login</a></p>
    </main>
    <script>
      const statusEl = document.getElementById("status");
      function setStatus(v) { statusEl.textContent = v; }

      async function run() {
        const params = new URLSearchParams(window.location.search);
        const code = params.get("code");
        const state = params.get("state");
        const error = params.get("error");
        const errorDescription = params.get("error_description");
        if (error) {
          setStatus("Provider returned error: " + error + "\\n" + (errorDescription || ""));
          return;
        }
        if (!code || !state) {
          setStatus("Missing code/state in callback URL.");
          return;
        }

        const redirectUri = window.location.origin + window.location.pathname;
        const res = await fetch("/api/v1/oauth/__PROVIDER__/callback", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ code, state, redirect_uri: redirectUri })
        });

        let body = null;
        try { body = await res.json(); } catch (_) {}
        if (!res.ok) {
          setStatus("OAuth callback failed (" + res.status + "):\\n" + JSON.stringify(body, null, 2));
          return;
        }
        if (body && body.access_token) {
          localStorage.setItem("access_token", body.access_token);
        }
        if (body && body.refresh_token) {
          localStorage.setItem("refresh_token", body.refresh_token);
        }
        setStatus("OAuth login succeeded. Tokens saved to localStorage.\\n\\n" + JSON.stringify(body, null, 2));
      }
      run();
    </script>
  </body>
</html>
""".replace("__PROVIDER__", provider)
    return HTMLResponse(content=html)
