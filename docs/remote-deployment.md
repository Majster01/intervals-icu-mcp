# Remote Deployment (HTTP / SSE)

How to run the server over HTTP/SSE for remote or hosted use, the available transport flags, and the security model you must apply before exposing it.

By default the server runs over **stdio** — the right transport for local clients like Claude Desktop, Claude Code, and Cursor. For remote deployment (hosted MCP, reverse proxy, Docker-on-a-server, ChatGPT connector), pass `--transport`:

```bash
# Streamable HTTP (recommended — used by ChatGPT and modern remote clients)
intervals-icu-mcp --transport http --host 127.0.0.1 --port 8000

# Legacy SSE (for clients that haven't moved to streamable HTTP yet)
intervals-icu-mcp --transport sse --host 127.0.0.1 --port 8000
```

| Flag | Default | Description |
|---|---|---|
| `--transport` | `stdio` | One of `stdio`, `http`, `sse`, `streamable-http` |
| `--host` | `127.0.0.1` | Interface to bind. Use `0.0.0.0` only inside a container where Docker controls the exposure. |
| `--port` | `8000` | TCP port |
| `--path` | (framework default) | URL path to mount the server under |

> ⚠️ **Security: do not expose an HTTP-mode server to untrusted networks** unless you enable the [OAuth gate](#public-deployment-with-oauth-claude-web-desktop-and-mobile).
>
> The MCP protocol has **no built-in authentication**. Anyone who can reach the URL can exercise every tool with your credentials — read every activity, delete activities, modify your FTP, create calendar events, etc. Binding to `0.0.0.0` on a direct-exposed host (VPS, LAN with open port) is equivalent to publishing your Intervals.icu API key.
>
> For remote access, prefer one of the following:
> - **Tailscale / Cloudflare Tunnel / ZeroTier** — only your authenticated devices can reach the endpoint. Zero code changes, simplest option.
> - **Reverse proxy with auth** (nginx + basic auth, Cloudflare Access, etc.) — terminates TLS and gates access.
> - **SSH tunnel** — `ssh -L 8000:localhost:8000 host` if you just need occasional access from one machine.
>
> Credentials are always read from `INTERVALS_ICU_API_KEY` and `INTERVALS_ICU_ATHLETE_ID` — use env vars (not a committed `.env`) when deploying to a shared host.

## Public deployment with OAuth (Claude web, desktop and mobile)

If you want the server on a public URL so it works as a **custom connector on claude.ai** (web, desktop and the mobile apps), put GitHub OAuth in front of it. Claude.ai connectors can't send custom headers or static tokens; OAuth is the only auth they support.

- Your **Intervals.icu credentials stay on the server** (`INTERVALS_ICU_API_KEY` / `INTERVALS_ICU_ATHLETE_ID`). They're never sent to or entered in a client.
- **OAuth decides who may use them.** Every MCP request needs a token from a GitHub login, and only the GitHub usernames in `MCP_ALLOWED_GITHUB_USERS` get through. Anyone else who adds your URL can complete the GitHub login but sees no tools and every call is denied.
- OAuth is **off unless `MCP_GITHUB_CLIENT_ID` is set**, and it only applies to HTTP transports. If it's half-configured (e.g. no allowlist), the server refuses to start rather than run open.

### 1. Create a GitHub OAuth App

GitHub → Settings → Developer settings → **OAuth Apps** → New OAuth App:

- **Homepage URL**: `https://icu.example.com`
- **Authorization callback URL**: `https://icu.example.com/auth/callback`

Copy the Client ID and generate a Client secret.

### 2. Configure and run

| Variable | Required | Description |
|---|---|---|
| `MCP_GITHUB_CLIENT_ID` | yes | GitHub OAuth App client ID. Setting it turns OAuth on. |
| `MCP_GITHUB_CLIENT_SECRET` | yes | GitHub OAuth App client secret. |
| `MCP_BASE_URL` | yes | Public HTTPS origin of the server, e.g. `https://icu.example.com`. |
| `MCP_ALLOWED_GITHUB_USERS` | yes | Comma-separated GitHub usernames allowed in (case-insensitive). |
| `MCP_JWT_SIGNING_KEY` | recommended | Long random string used to sign the tokens this server issues. It's derived from the client secret if unset, so rotating the secret would log everyone out. |
| `FASTMCP_HOME` | recommended in containers | Where OAuth client registrations and tokens are stored (encrypted). Put it on a persistent volume, or Claude has to reconnect after every redeploy. |

```bash
docker run -d --name intervals-icu-mcp --restart unless-stopped \
  -p 127.0.0.1:8000:8000 \
  -v icu-mcp-data:/data \
  -e FASTMCP_HOME=/data \
  -e INTERVALS_ICU_API_KEY=... \
  -e INTERVALS_ICU_ATHLETE_ID=i123456 \
  -e INTERVALS_ICU_DELETE_MODE=none \
  -e MCP_GITHUB_CLIENT_ID=Ov23li... \
  -e MCP_GITHUB_CLIENT_SECRET=... \
  -e MCP_BASE_URL=https://icu.example.com \
  -e MCP_ALLOWED_GITHUB_USERS=your-github-username \
  -e MCP_JWT_SIGNING_KEY="$(openssl rand -hex 32)" \
  intervals-icu-mcp --transport http --host 0.0.0.0 --port 8000
```

Terminate TLS in front of it with a reverse proxy (Caddy, nginx) or a PaaS that provides HTTPS (Fly.io, Railway, Render). OAuth requires the public URL to be HTTPS. Pass the secrets through your platform's secret store rather than shell history. For a public endpoint, consider `INTERVALS_ICU_DELETE_MODE=none` (or keep the default `safe`).

On startup the log line ends with `oauth=github(1 allowed)` when OAuth is active.

### 3. Add it to Claude

claude.ai → **Settings → Connectors → Add custom connector**, URL `https://icu.example.com/mcp`. Leave the OAuth client ID/secret fields empty: the server supports dynamic client registration. Claude sends you through GitHub once, and the connector is then available on web, desktop and mobile for your account.

### Checking it

```bash
curl -i https://icu.example.com/mcp                                        # 401 + WWW-Authenticate
curl https://icu.example.com/.well-known/oauth-authorization-server         # OAuth metadata
```
