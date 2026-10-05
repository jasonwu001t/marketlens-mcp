"""The Alpaca HTTP client: keys, base URLs, rate limiting, retries, error mapping, GET only."""

from __future__ import annotations

import asyncio
import hashlib
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from alpaca_harness import TEST_KEYS, FakeAlpaca, FakeContext

from marketlens_mcp.plugin_api import ToolError
from marketlens_mcp.providers.alpaca import client as client_mod
from marketlens_mcp.providers.alpaca.client import AlpacaClient
from marketlens_schema.base import Environment

R11 = "ALPACA_API_KEY and ALPACA_SECRET_KEY are not set in this server's environment, so Alpaca tools cannot run."
R11_HINT = "Add both to the env block of this server in your MCP client's configuration."


def get(ctx, api, path, params=None, **kw):
    async def run():
        async with AlpacaClient(ctx) as c:
            return await c.get(api, path, params, **kw)

    return asyncio.run(run())


def refused(ctx, api="data", path="/v2/clock") -> ToolError:
    with pytest.raises(ToolError) as e:
        get(ctx, api, path)
    return e.value


def test_missing_keys_refuse_with_r11(alpaca):
    err = refused(FakeContext(environ={}))
    assert (err.code, err.message, err.hint) == ("alpaca_keys_missing", R11, R11_HINT)
    assert alpaca.requests == []


def test_apca_key_names_are_accepted(alpaca):
    alpaca.add("/v2/clock", {"ok": True})
    ctx = FakeContext(environ={"APCA_API_KEY_ID": "PKALT", "APCA_API_SECRET_KEY": "alt-secret"})
    assert get(ctx, "trading", "/v2/clock") == {"ok": True}
    assert alpaca.requests[0].headers["APCA-API-KEY-ID"] == "PKALT"


def test_alpaca_names_win_over_apca_names(alpaca):
    alpaca.add("/v2/clock", {"ok": True})
    ctx = FakeContext(environ={**TEST_KEYS, "APCA_API_KEY_ID": "PKALT", "APCA_API_SECRET_KEY": "alt"})
    get(ctx, "trading", "/v2/clock")
    assert alpaca.requests[0].headers["APCA-API-KEY-ID"] == TEST_KEYS["ALPACA_API_KEY"]


def test_an_undeclared_env_name_is_treated_as_unset(alpaca):
    # The server's ctx.env raises KeyError for names a spec does not declare.
    alpaca.add("/v2/clock", {"ok": True})
    ctx = FakeContext(declared_env=("ALPACA_API_KEY", "ALPACA_SECRET_KEY"))
    assert get(ctx, "trading", "/v2/clock") == {"ok": True}


def test_headers(alpaca):
    alpaca.add("/v2/clock", {})
    get(FakeContext(), "trading", "/v2/clock")
    h = alpaca.requests[0].headers
    assert h["APCA-API-KEY-ID"] == TEST_KEYS["ALPACA_API_KEY"]
    assert h["APCA-API-SECRET-KEY"] == TEST_KEYS["ALPACA_SECRET_KEY"]
    assert h["User-Agent"] == "marketlens-mcp/0.1.0"
    assert h["Accept"] == "application/json"


@pytest.mark.parametrize(
    ("api", "env", "settings", "host"),
    [
        ("data", Environment.PAPER, {}, "data.alpaca.markets"),
        ("data", Environment.LIVE, {}, "data.alpaca.markets"),
        ("trading", Environment.PAPER, {}, "paper-api.alpaca.markets"),
        ("trading", Environment.LIVE, {}, "api.alpaca.markets"),
        (
            "trading",
            Environment.PAPER,
            {"trading_url": "https://broker.example.test/"},
            "broker.example.test",
        ),
        ("data", Environment.PAPER, {"data_url": "https://data.example.test"}, "data.example.test"),
    ],
)
def test_base_urls(alpaca, api, env, settings, host):
    alpaca.add("/v2/clock", {})
    get(FakeContext(portfolio_environment=env, settings=settings), api, "/v2/clock")
    url = alpaca.requests[0].url
    assert (url.scheme, url.host, url.path) == ("https", host, "/v2/clock")


def test_query_parameters_drop_none_and_join_lists(alpaca):
    alpaca.add("/v2/stocks/bars", {})
    get(
        FakeContext(),
        "data",
        "/v2/stocks/bars",
        {"symbols": ["AAPL", "BRK.B"], "end": None, "limit": 5, "flag": True},
    )
    assert alpaca.params() == {"symbols": "AAPL,BRK.B", "limit": "5", "flag": "true"}


def test_every_request_waits_for_the_rate_limiter(alpaca):
    alpaca.add("/v2/clock", {})
    ctx = FakeContext()
    get(ctx, "trading", "/v2/clock")
    get(ctx, "trading", "/v2/clock")
    key = "alpaca:" + hashlib.sha256(TEST_KEYS["ALPACA_API_KEY"].encode()).hexdigest()[:8]
    assert list(ctx.limiters) == [key]
    assert ctx.limiters[key].per_minute == 190
    assert ctx.limiters[key].acquired == 2


def test_rate_limit_setting_is_used(alpaca):
    alpaca.add("/v2/clock", {})
    ctx = FakeContext(settings={"rate_limit_per_minute": 120})
    get(ctx, "trading", "/v2/clock")
    assert next(iter(ctx.limiters.values())).per_minute == 120


def test_the_rate_limit_never_reaches_alpacas_200_per_minute(alpaca):
    alpaca.add("/v2/clock", {})
    ctx = FakeContext(settings={"rate_limit_per_minute": 500})
    get(ctx, "trading", "/v2/clock")
    assert next(iter(ctx.limiters.values())).per_minute == 199


def test_quotes_description_states_the_round_lot_conversion(specs):
    assert "round lots" in specs["market_quotes"].description


def test_429_honours_retry_after_then_succeeds(alpaca, _no_sleep):
    alpaca.add(
        "/v2/clock",
        httpx.Response(429, headers={"Retry-After": "5"}, json={"message": "too many"}),
        {"ok": 1},
    )
    ctx = FakeContext()
    assert get(ctx, "trading", "/v2/clock") == {"ok": 1}
    assert _no_sleep == [5.0]
    assert next(iter(ctx.limiters.values())).acquired == 2


def test_429_gives_up_after_two_retries(alpaca, _no_sleep):
    alpaca.add("/v2/clock", httpx.Response(429, json={"message": "too many"}))
    err = refused(FakeContext(), "trading")
    assert err.code == "alpaca_rate_limited" and err.retryable
    assert _no_sleep == [3.0, 3.0]  # default Retry-After
    assert len(alpaca.requests) == 3


def _429(retry_after: str) -> httpx.Response:
    return httpx.Response(429, headers={"Retry-After": retry_after}, json={"message": "too many"})


@pytest.mark.parametrize("header", ["inf", "-inf", "Infinity", "nan", "NaN", "soon", ""])
def test_a_non_finite_or_unreadable_retry_after_waits_the_default(alpaca, _no_sleep, header):
    alpaca.add("/v2/clock", _429(header), {"ok": 1})
    assert get(FakeContext(), "trading", "/v2/clock") == {"ok": 1}
    assert _no_sleep == [3.0]


@pytest.mark.parametrize(
    ("header", "slept"),
    [
        ("60", 60.0),  # the cap itself is honoured
        ("0", 0.0),
        ("-5", 0.0),
        ("Fri, 02 Oct 2026 20:00:07 GMT", 7.0),  # an HTTP-date, counted from ctx.now()
        ("Fri, 02 Oct 2026 19:59:00 GMT", 0.0),  # already past
    ],
)
def test_retry_after_seconds_and_http_dates_are_honoured(alpaca, _no_sleep, header, slept):
    alpaca.add("/v2/clock", _429(header), {"ok": 1})
    assert get(FakeContext(), "trading", "/v2/clock") == {"ok": 1}
    assert _no_sleep == [slept]


@pytest.mark.parametrize(
    "header", ["61", "86400", "1e308", "Sat, 03 Oct 2026 20:00:00 GMT", "Fri, 31 Dec 9999 23:59:59 GMT"]
)
def test_a_retry_after_beyond_the_cap_is_rate_limited_at_once(alpaca, _no_sleep, header):
    alpaca.add("/v2/clock", _429(header), {"ok": 1})
    err = refused(FakeContext(), "trading")
    assert (err.code, err.retryable) == ("alpaca_rate_limited", True)
    assert err.message == (
        "Alpaca is rate-limiting these API keys (HTTP 429) and asked to wait longer than 60 seconds."
    )
    assert _no_sleep == [] and len(alpaca.requests) == 1


def test_5xx_is_retried_once(alpaca, _no_sleep):
    alpaca.add("/v2/clock", httpx.Response(503, text="down"), {"ok": 1})
    assert get(FakeContext(), "trading", "/v2/clock") == {"ok": 1}
    assert _no_sleep == [1.0]


def test_5xx_twice_is_unavailable(alpaca, _no_sleep):
    alpaca.add("/v2/clock", httpx.Response(500, text="boom"))
    err = refused(FakeContext(), "trading")
    assert err.code == "alpaca_unavailable" and err.retryable
    assert len(alpaca.requests) == 2


@pytest.mark.parametrize(("status", "env"), [(401, Environment.PAPER), (403, Environment.LIVE)])
def test_auth_errors(alpaca, status, env):
    alpaca.add("/v2/account", httpx.Response(status, json={"message": "forbidden."}))
    err = refused(FakeContext(portfolio_environment=env), "trading", "/v2/account")
    assert err.code == "alpaca_auth"
    assert err.message == (
        f"Alpaca refused the API keys (HTTP {status}). Check ALPACA_API_KEY and ALPACA_SECRET_KEY, "
        f"and that they belong to the {env.value} account."
    )


def test_subscription_errors_are_not_entitled(alpaca):
    alpaca.add(
        "/v2/stocks/bars",
        httpx.Response(403, json={"message": "subscription does not permit querying recent SIP data"}),
    )
    err = refused(FakeContext(), "data", "/v2/stocks/bars")
    assert err.code == "alpaca_not_entitled"
    assert "subscription does not permit querying recent SIP data" in err.message
    assert "stock_feed: iex" in err.hint and "delayed_sip" in err.hint


@pytest.mark.parametrize("status", [400, 404, 422])
def test_rejections_carry_alpacas_message_capped(alpaca, status):
    alpaca.add("/v2/assets/ZZZZ", httpx.Response(status, json={"code": 40410000, "message": "x" * 500}))
    err = refused(FakeContext(), "trading", "/v2/assets/ZZZZ")
    assert err.code == "alpaca_rejected" and not err.retryable
    assert err.message.startswith(f"Alpaca rejected the request (HTTP {status}): xxx")
    assert len(err.message) <= 300 + len(f"Alpaca rejected the request (HTTP {status}): ")


def test_timeout(alpaca):
    def boom(request):
        raise httpx.ReadTimeout("slow", request=request)

    alpaca.add("/v2/clock", boom)
    err = refused(FakeContext(), "trading")
    assert err.code == "alpaca_timeout" and err.retryable


def test_connection_failure(alpaca):
    def boom(request):
        raise httpx.ConnectError("refused", request=request)

    alpaca.add("/v2/clock", boom)
    err = refused(FakeContext(), "trading")
    assert err.code == "alpaca_unavailable"


def test_every_request_has_a_30_second_timeout(alpaca):
    alpaca.add("/v2/clock", {})
    get(FakeContext(), "trading", "/v2/clock")
    assert alpaca.requests[0].extensions["timeout"] == {
        "connect": 30.0,
        "read": 30.0,
        "write": 30.0,
        "pool": 30.0,
    }


def test_a_redirect_is_never_followed(alpaca):
    # The key headers are custom ones that httpx would carry to another host.
    alpaca.add(
        "/v2/clock",
        httpx.Response(302, headers={"Location": "https://elsewhere.example.test/v2/clock"}, json={"ok": 1}),
    )
    err = refused(FakeContext(), "trading")
    assert (err.code, err.retryable) == ("alpaca_unavailable", False)
    assert err.message == "Alpaca answered with a redirect (HTTP 302), which this server does not follow."
    assert "trading_url" in err.hint
    assert [q.url.host for q in alpaca.requests] == ["paper-api.alpaca.markets"]


def test_the_http_client_ignores_proxy_and_certificate_environment():
    async def run():
        async with AlpacaClient(FakeContext()) as c:
            return c._http.trust_env, c._http.follow_redirects

    assert asyncio.run(run()) == (False, False)


def test_errors_never_echo_keys(alpaca):
    secret = TEST_KEYS["ALPACA_SECRET_KEY"]
    alpaca.add(
        "/v2/clock", httpx.Response(422, json={"message": f"bad key {secret} {TEST_KEYS['ALPACA_API_KEY']}"})
    )
    err = refused(FakeContext(), "trading")
    assert secret not in err.message and TEST_KEYS["ALPACA_API_KEY"] not in err.message
    assert "<redacted>" in err.message


def test_trading_api_numbers_can_be_read_as_exact_decimals(alpaca):
    alpaca.add("/v2/account/portfolio/history", httpx.Response(200, text='{"equity": [8425.21, null]}'))
    data = get(FakeContext(), "trading", "/v2/account/portfolio/history", decimals=True)
    assert data == {"equity": [Decimal("8425.21"), None]}


def test_the_client_can_only_get():
    for name in ("post", "put", "patch", "delete", "request", "stream"):
        assert not hasattr(AlpacaClient, name), name


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_the_fake_alpaca_answers_get_only(method):
    # Every test's fake Alpaca refuses other methods (and its fixture fails the
    # test at teardown), so a provider that sent one could not pass the suite.
    fake = FakeAlpaca()
    fake.add("/v2/orders", {"ok": True})

    async def send():
        async with httpx.AsyncClient(transport=fake.transport) as http:
            return await http.request(method, "https://paper-api.alpaca.markets/v2/orders")

    resp = asyncio.run(send())
    assert resp.status_code == 405 and method in resp.json()["message"]
    assert [r.method for r in fake.requests] == [method]
    assert fake.non_get() == [f"{method} /v2/orders"]


def _docs_method_argument(rel: str, line: str) -> bool:
    """The one line allowed to name HTTP methods in quotes: the documentation
    lookup's ``method`` argument (it reads Alpaca's docs about an endpoint)."""
    return rel == "tools/provider_docs.py" and line.strip().startswith("method: Literal[")


def test_no_write_call_appears_anywhere_in_the_provider():
    root = Path(client_mod.__file__).parent
    calls = re.compile(
        r"\.(post|put|patch|delete|request|stream)\(|\b(send|build_request)\(|httpx\.Request\("
    )
    methods = re.compile(r"""["'](POST|PUT|PATCH|DELETE)["']""")
    offenders = []
    for p in sorted(root.rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        for i, line in enumerate(p.read_text().splitlines(), 1):
            if calls.search(line) or (methods.search(line) and not _docs_method_argument(rel, line)):
                offenders.append(f"{rel}:{i}: {line.strip()}")
    assert offenders == []


@pytest.mark.parametrize("decimals", [False, True])
def test_a_success_that_is_not_json_is_a_readable_error(alpaca, decimals):
    alpaca.add("/v2/clock", httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(ToolError) as e:
        get(FakeContext(), "trading", "/v2/clock", decimals=decimals)
    assert e.value.code == "alpaca_unavailable" and "not JSON" in e.value.message and e.value.retryable
