"""Streamable HTTP on loopback with a bearer token (contract 7.2). Driven
in-process through httpx's ASGI transport: no socket is opened."""

from __future__ import annotations

import httpx
import pytest
from coresupport import run

from marketlens_mcp import server, transport

TOKEN = "t" * 40
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_r10_only_loopback_hosts():
    transport.check_host("127.0.0.1")
    transport.check_host("::1")
    for host in ("0.0.0.0", "localhost", "192.168.1.5", "::"):
        with pytest.raises(transport.TransportError) as info:
            transport.check_host(host)
        assert str(info.value) == f"marketlens-mcp listens only on 127.0.0.1 or ::1; --host {host} refused."


def test_r9_token_required_and_long_enough():
    r9 = (
        "streamable HTTP needs a bearer token: set MARKETLENS_HTTP_TOKEN to a random string of at least 32 "
        'characters, for example: python -c "import secrets; print(secrets.token_urlsafe(32))"'
    )
    for env in ({}, {"MARKETLENS_HTTP_TOKEN": "short"}, {"MARKETLENS_HTTP_TOKEN": " " * 40}):
        with pytest.raises(transport.TransportError) as info:
            transport.http_token(env)
        assert str(info.value) == r9
    assert transport.http_token({"MARKETLENS_HTTP_TOKEN": TOKEN}) == TOKEN


async def _requests(app, inner, calls):
    out = []
    async with inner.router.lifespan_context(inner):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8765"
        ) as c:
            for headers in calls:
                out.append(await c.post("/mcp", json=INIT, headers={**HEADERS, **headers}))
    return out


def test_bearer_token_checked_before_anything_else(tmp_path):
    rt = server.build_runtime(discover=list)
    mcp = server.build_server(rt)
    app, inner = transport.http_app(mcp, host="127.0.0.1", port=8765, token=TOKEN)
    missing, wrong, scheme, good = run(
        _requests(
            app,
            inner,
            [
                {},
                {"Authorization": "Bearer " + "x" * 40},
                {"Authorization": TOKEN},
                {"Authorization": "Bearer " + TOKEN},
            ],
        )
    )
    for r in (missing, wrong, scheme):
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}
        assert r.headers["www-authenticate"] == "Bearer"
    assert good.status_code == 200
    assert "marketlens" in good.text  # the initialize result names the server


def test_host_and_origin_protection(tmp_path):
    rt = server.build_runtime(discover=list)
    mcp = server.build_server(rt)
    app, inner = transport.http_app(mcp, host="127.0.0.1", port=8765, token=TOKEN)

    async def bound_to_loopback(scope, receive, send):
        # uvicorn reports the bound address; httpx's ASGI transport would report the URL's host
        if scope["type"] == "http":
            scope = {**scope, "server": ("127.0.0.1", 8765)}
        await app(scope, receive, send)

    async def go():
        auth = {**HEADERS, "Authorization": "Bearer " + TOKEN}
        async with inner.router.lifespan_context(inner):
            asgi = httpx.ASGITransport(app=bound_to_loopback)
            async with httpx.AsyncClient(transport=asgi, base_url="http://evil.example:8765") as c:
                bad_host = await c.post("/mcp", json=INIT, headers=auth)
            async with httpx.AsyncClient(transport=asgi, base_url="http://127.0.0.1:8765") as c:
                bad_origin = await c.post(
                    "/mcp", json=INIT, headers={**auth, "Origin": "http://evil.example"}
                )
                fine = await c.post("/mcp", json=INIT, headers=auth)
        return bad_host, bad_origin, fine

    bad_host, bad_origin, fine = run(go())
    assert bad_host.status_code == 421
    assert bad_origin.status_code == 403
    assert fine.status_code == 200
