"""Tests for opt-in OAuth protection of remote deployments (remote_auth.py)."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from asgi_lifespan import LifespanManager
from fastmcp import FastMCP
from fastmcp.server.auth import AccessToken, AuthContext
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.middleware.authorization import AuthMiddleware

from intervals_icu_mcp.remote_auth import (
    RemoteAuthConfigError,
    allowed_github_users,
    allowed_users_check,
    build_auth_provider,
)

OAUTH_VARS = (
    "MCP_GITHUB_CLIENT_ID",
    "MCP_GITHUB_CLIENT_SECRET",
    "MCP_BASE_URL",
    "MCP_ALLOWED_GITHUB_USERS",
    "MCP_JWT_SIGNING_KEY",
)


@pytest.fixture(autouse=True)
def _clean_oauth_env(monkeypatch):
    for name in OAUTH_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def full_oauth_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_GITHUB_CLIENT_ID", "Ov23liTEST")
    monkeypatch.setenv("MCP_GITHUB_CLIENT_SECRET", "secret")
    monkeypatch.setenv("MCP_BASE_URL", "https://icu.example.com")
    monkeypatch.setenv("MCP_ALLOWED_GITHUB_USERS", "Owner, second ")
    # Keep the OAuth proxy's encrypted file store out of the real user data dir.
    monkeypatch.setattr("fastmcp.settings.home", tmp_path)


def _ctx(login: object | None) -> AuthContext:
    token = None
    if login is not None:
        token = AccessToken(token="t", client_id="c", scopes=[], claims={"login": login})
    return AuthContext(token=token, component=None)  # type: ignore[arg-type]


class TestBuildAuthProvider:
    def test_disabled_when_unset(self):
        assert build_auth_provider() is None

    def test_builds_github_provider_when_fully_configured(self, full_oauth_env):
        assert isinstance(build_auth_provider(), GitHubProvider)

    @pytest.mark.parametrize("missing", ["MCP_GITHUB_CLIENT_SECRET", "MCP_BASE_URL"])
    def test_partial_config_fails_closed(self, full_oauth_env, monkeypatch, missing):
        monkeypatch.delenv(missing)
        with pytest.raises(RemoteAuthConfigError, match=missing):
            build_auth_provider()

    def test_missing_allowlist_fails_closed(self, full_oauth_env, monkeypatch):
        monkeypatch.setenv("MCP_ALLOWED_GITHUB_USERS", " , ")
        with pytest.raises(RemoteAuthConfigError, match="MCP_ALLOWED_GITHUB_USERS"):
            build_auth_provider()


class TestAllowedUsersCheck:
    def test_allowlist_is_parsed_and_lowercased(self, full_oauth_env):
        assert allowed_github_users() == {"owner", "second"}

    @pytest.mark.parametrize(
        ("login", "allowed"),
        [("owner", True), ("OWNER", True), ("second", True), ("stranger", False), (None, False)],
    )
    def test_login_matching(self, full_oauth_env, login, allowed):
        assert allowed_users_check(_ctx(login)) is allowed

    def test_non_string_login_denied(self, full_oauth_env):
        assert allowed_users_check(_ctx(12345)) is False


class TestHttpEnforcement:
    """End-to-end over streamable HTTP, with a static verifier standing in for GitHub."""

    @asynccontextmanager
    async def _http_client(self) -> AsyncIterator[httpx.AsyncClient]:
        verifier = StaticTokenVerifier(
            tokens={
                "owner-token": {"client_id": "c", "scopes": [], "login": "Owner"},
                "stranger-token": {"client_id": "c", "scopes": [], "login": "stranger"},
            }
        )
        server = FastMCP("test", auth=verifier)
        server.add_middleware(AuthMiddleware(auth=allowed_users_check))

        @server.tool
        def ping() -> str:
            return "pong"

        app = server.http_app()
        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                yield client

    @staticmethod
    async def _rpc(client: httpx.AsyncClient, token: str, method: str, params: dict) -> dict:
        headers = {
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        }
        init = await client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
        )
        headers["mcp-session-id"] = init.headers["mcp-session-id"]
        await client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        resp = await client.post(
            "/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": method, "params": params}
        )
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                return json.loads(line.removeprefix("data: "))
        raise AssertionError(f"No SSE data frame in body: {resp.text!r}")

    async def test_request_without_token_is_rejected(self, full_oauth_env):
        async with self._http_client() as client:
            resp = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
            assert resp.status_code == 401

    async def test_allowed_user_sees_and_calls_tools(self, full_oauth_env):
        async with self._http_client() as client:
            listed = await self._rpc(client, "owner-token", "tools/list", {})
            assert [t["name"] for t in listed["result"]["tools"]] == ["ping"]
            called = await self._rpc(
                client, "owner-token", "tools/call", {"name": "ping", "arguments": {}}
            )
            assert called["result"]["content"][0]["text"] == "pong"

    async def test_other_github_user_gets_nothing(self, full_oauth_env):
        async with self._http_client() as client:
            listed = await self._rpc(client, "stranger-token", "tools/list", {})
            assert listed["result"]["tools"] == []
            called = await self._rpc(
                client, "stranger-token", "tools/call", {"name": "ping", "arguments": {}}
            )
            assert "error" in called or called["result"]["isError"] is True
