"""Optional OAuth protection for remote (HTTP/SSE) deployments.

When ``MCP_GITHUB_CLIENT_ID`` is set, the server requires a GitHub OAuth login
and only lets through the GitHub users listed in ``MCP_ALLOWED_GITHUB_USERS``.
Intervals.icu credentials still come from ``INTERVALS_ICU_API_KEY`` /
``INTERVALS_ICU_ATHLETE_ID`` on the server — OAuth only decides *who* may use
them. Unset, the server behaves exactly as before (stdio or open HTTP).
"""

import os

from fastmcp.server.auth import AuthContext
from fastmcp.server.auth.providers.github import GitHubProvider

REQUIRED_VARS = ("MCP_GITHUB_CLIENT_ID", "MCP_GITHUB_CLIENT_SECRET", "MCP_BASE_URL")


class RemoteAuthConfigError(RuntimeError):
    """Raised when OAuth is partially configured — fail closed rather than run open."""


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def allowed_github_users() -> frozenset[str]:
    """Lower-cased GitHub logins from ``MCP_ALLOWED_GITHUB_USERS`` (comma-separated)."""
    raw = _env("MCP_ALLOWED_GITHUB_USERS")
    return frozenset(u.strip().lower() for u in raw.split(",") if u.strip())


def oauth_enabled() -> bool:
    return bool(_env("MCP_GITHUB_CLIENT_ID"))


def build_auth_provider() -> GitHubProvider | None:
    """Build the GitHub OAuth provider from env, or return None when OAuth is off.

    Raises:
        RemoteAuthConfigError: If OAuth is enabled but a required var or the
            user allowlist is missing.
    """
    if not oauth_enabled():
        return None

    missing = [name for name in REQUIRED_VARS if not _env(name)]
    if missing:
        raise RemoteAuthConfigError(
            f"MCP_GITHUB_CLIENT_ID is set but {', '.join(missing)} is missing."
        )
    if not allowed_github_users():
        raise RemoteAuthConfigError(
            "MCP_ALLOWED_GITHUB_USERS must list at least one GitHub username when "
            "OAuth is enabled — otherwise any GitHub account could use your credentials."
        )

    return GitHubProvider(
        client_id=_env("MCP_GITHUB_CLIENT_ID"),
        client_secret=_env("MCP_GITHUB_CLIENT_SECRET"),
        base_url=_env("MCP_BASE_URL"),
        jwt_signing_key=_env("MCP_JWT_SIGNING_KEY") or None,
    )


def allowed_users_check(ctx: AuthContext) -> bool:
    """Auth check: allow only tokens whose GitHub login is on the allowlist."""
    if ctx.token is None:
        return False
    login = ctx.token.claims.get("login")
    if not isinstance(login, str):
        return False
    return login.lower() in allowed_github_users()
