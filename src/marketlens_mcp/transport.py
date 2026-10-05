"""Transports (contract 7.2): stdio (default) and streamable HTTP on a
loopback address with a bearer token checked before anything else.

No SSE transport. No telemetry: the server sends nothing anywhere except
the upstream calls a tool makes.
"""

from __future__ import annotations

import hmac
import json
from collections.abc import Mapping
from typing import Any

from fastmcp import FastMCP

LOOPBACK_HOSTS = ("127.0.0.1", "::1")
MIN_TOKEN_CHARS = 32

R9 = (
    "streamable HTTP needs a bearer token: set MARKETLENS_HTTP_TOKEN to a random string of at least 32 "
    'characters, for example: python -c "import secrets; print(secrets.token_urlsafe(32))"'
)


class TransportError(Exception):
    """A transport setting the server refuses (R9, R10)."""


def check_host(host: str) -> None:
    """R10: only 127.0.0.1 or ::1."""
    if host not in LOOPBACK_HOSTS:
        raise TransportError(f"marketlens-mcp listens only on 127.0.0.1 or ::1; --host {host} refused.")


def check_port(port: int) -> None:
    if not 1024 <= port <= 65535:
        raise TransportError(f"--port must be between 1024 and 65535 (got {port}).")


def http_token(env: Mapping[str, str]) -> str:
    """R9: MARKETLENS_HTTP_TOKEN, at least 32 characters."""
    token = (env.get("MARKETLENS_HTTP_TOKEN") or "").strip()
    if len(token) < MIN_TOKEN_CHARS:
        raise TransportError(R9)
    return token


class BearerAuth:
    """Pure ASGI wrapper: every HTTP request must carry
    ``Authorization: Bearer <token>`` (compared in constant time) or it gets
    401 before the MCP app sees it. Lifespan events pass through."""

    def __init__(self, app: Any, token: str):
        self.app = app
        self._expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        given = b""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                given = value
                break
        if not hmac.compare_digest(given, self._expected):
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            body = json.dumps({"error": "unauthorized"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        await self.app(scope, receive, send)


def http_app(mcp: FastMCP, *, host: str, port: int, token: str) -> tuple[BearerAuth, Any]:
    """(the guarded ASGI app, the inner FastMCP Starlette app)."""
    check_host(host)
    inner = mcp.http_app(
        path="/mcp",
        transport="http",
        host_origin_protection=True,
        allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"],
    )
    return BearerAuth(inner, token), inner


async def serve_http(mcp: FastMCP, *, host: str, port: int, token: str) -> None:
    import uvicorn

    app, _ = http_app(mcp, host=host, port=port, token=token)
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="on", access_log=False)
    await uvicorn.Server(config).serve()


async def serve_stdio(mcp: FastMCP) -> None:
    await mcp.run_stdio_async(show_banner=False)
