"""The ``marketlens-mcp`` console script (contract 1.3).

    marketlens-mcp                       serve over stdio (so `uvx marketlens-mcp` works)
    marketlens-mcp serve [--transport stdio|http] [--port N] [--host 127.0.0.1|::1]
    marketlens-mcp tools [--json|--markdown] [--all]
    marketlens-mcp capabilities | plugins
    marketlens-mcp config path|show|init
    marketlens-mcp doctor [--network]
    marketlens-mcp schema [--out DIR]
    marketlens-mcp results list|purge [--yes]
    marketlens-mcp add-capability NAME --capability ID --provider alpaca|local [...]
    marketlens-mcp readme [--check]
    marketlens-mcp --version

Every command turns off fastmcp's banner and PyPI update check first.
Logs go to stderr only (stdout carries the MCP protocol on stdio).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import pathlib
import sys
import tempfile
from collections.abc import Sequence
from typing import Any

from marketlens_schema import SCHEMA_VERSION

from . import __version__, paths
from .config import DEFAULT_CONFIG_TEXT, ConfigError
from .plugin_api import PLUGIN_API_VERSION
from .registry import BuiltinPluginError

#: Injected by tests (httpx.MockTransport) for ``doctor --network``.
HTTP_TRANSPORT: Any = None


def _quiet() -> None:
    from .server import quiet_fastmcp

    quiet_fastmcp()


def _logging() -> None:
    level = os.environ.get("MARKETLENS_LOG_LEVEL", "WARNING").upper()
    logging.basicConfig(
        stream=sys.stderr,
        level=getattr(logging, level, logging.WARNING),
        format="%(levelname)s %(name)s: %(message)s",
    )


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _runtime():
    from .server import build_runtime

    return build_runtime()


# --- serve ------------------------------------------------------------------------------------


def cmd_serve(args: argparse.Namespace) -> int:
    from . import server, transport

    rt = _runtime()
    port = args.port or rt.config.http_port
    if args.transport == "http":
        token = transport.http_token(os.environ)
        transport.check_host(args.host)
        transport.check_port(port)
    for line in server.startup_notices(rt):
        _err(line)
    mcp = server.build_server(rt)
    if args.transport == "http":
        asyncio.run(transport.serve_http(mcp, host=args.host, port=port, token=token))
    else:
        asyncio.run(transport.serve_stdio(mcp))
    return 0


# --- tools, capabilities, plugins -------------------------------------------------------------


def tools_document(rt, *, include_disabled: bool) -> dict[str, Any]:
    """The verbatim ``tools --json`` format (launchers that attest the tool list depend on it)."""
    tools = []
    for entry in sorted(rt.catalog.tools.values(), key=lambda e: e.spec.name):
        enabled = rt.policy.is_enabled(entry.spec.capability)
        if not enabled and not include_disabled:
            continue
        tools.append(
            {
                "name": entry.spec.name,
                "capability": entry.spec.capability,
                "provider": entry.spec.provider,
                "plugin": entry.plugin,
                "enabled": enabled,
                "output_risk": entry.spec.output_risk,
            }
        )
    caps = [
        {
            "id": s.spec.id,
            "enabled": s.enabled,
            "default": s.spec.default_enabled or s.spec.locked,
            "source": s.source,
            "declared_by": s.spec.declared_by,
        }
        for s in rt.policy.states
    ]
    plugins = [
        {
            "name": p.name,
            "enabled": p.enabled,
            "loaded": p.loaded,
            "api_version": list(p.api_version) if p.api_version else None,
            "error": p.error,
        }
        for p in rt.catalog.plugins
    ]
    return {
        "server": "marketlens-mcp",
        "version": __version__,
        "schema_version": SCHEMA_VERSION,
        "plugin_api": list(PLUGIN_API_VERSION),
        "config_path": rt.config.display_path,
        "tools": tools,
        "capabilities": caps,
        "plugins": plugins,
    }


def cmd_tools(args: argparse.Namespace) -> int:
    rt = _runtime()
    if args.json:
        print(json.dumps(tools_document(rt, include_disabled=args.all), indent=2))
        return 0
    if args.markdown:
        from .readme import tool_table

        states = {s.spec.id: s.enabled for s in rt.policy.states}
        print(
            tool_table(
                rt.catalog.tools.values(), rt.catalog.capabilities, states=states, show_disabled=args.all
            )
        )
        return 0
    doc = tools_document(rt, include_disabled=args.all)
    for t in doc["tools"]:
        flag = "" if t["enabled"] else "  (off)"
        print(f"{t['name']:<40} {t['capability']:<16} {t['plugin']}{flag}")
    print(
        f"{len(doc['tools'])} tools ({sum(t['enabled'] for t in doc['tools'])} enabled); config {doc['config_path']}"
    )
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    rt = _runtime()
    print(f"{'capability':<18} {'default':<8} {'state':<6} {'source':<15} {'declared by':<14} title")
    for s in rt.policy.states:
        default = "on" if (s.spec.default_enabled or s.spec.locked) else "off"
        state = "on" if s.enabled else "off"
        print(
            f"{s.spec.id:<18} {default:<8} {state:<6} {s.source:<15} {s.spec.declared_by:<14} {s.spec.title}"
        )
    return 0


def cmd_plugins(args: argparse.Namespace) -> int:
    rt = _runtime()
    if not rt.catalog.plugins:
        print("No plugins installed or named in plugins.enabled.")
        return 0
    for p in rt.catalog.plugins:
        version = ".".join(map(str, p.api_version)) if p.api_version else "-"
        print(
            f"{p.name:<20} dist={p.distribution or '-'} enabled={'yes' if p.enabled else 'no'} "
            f"loaded={'yes' if p.loaded else 'no'} api={version}" + (f" error: {p.error}" if p.error else "")
        )
    return 0


# --- config -----------------------------------------------------------------------------------


def cmd_config(args: argparse.Namespace) -> int:
    target = paths.config_path()
    if args.action == "path":
        print(paths.display_path(target))
        return 0
    if args.action == "show":
        rt = _runtime()
        caps = {s.spec.id: s.enabled for s in rt.policy.states}
        print(rt.config.effective_yaml(capabilities=caps), end="")
        return 0
    if target.exists():
        _err(f"{paths.display_path(target)} already exists; marketlens-mcp config init never overwrites it.")
        return 1
    paths.ensure_private_dir(target.parent)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(DEFAULT_CONFIG_TEXT)
    print(f"Wrote {paths.display_path(target)}. Edit it, then restart the server.")
    return 0


# --- doctor -----------------------------------------------------------------------------------


def guard_self_test() -> list[str]:
    """Problems found (empty = the guard and the lock-down hold)."""
    import pyarrow as pa

    from .results import guard
    from .results.store import StoreRoot
    from .results_api import QueryRefused

    problems = []
    with tempfile.TemporaryDirectory(prefix="marketlens-doctor-") as tmp:
        store = StoreRoot(pathlib.Path(tmp)).session("doctor")
        from datetime import UTC, datetime

        from marketlens_schema import Provenance

        info = store.put(
            pa.table({"x": [1, 2, 3]}),
            tool="doctor",
            model="marketlens.QueryRow",
            provenance=Provenance(provider="marketlens", route="doctor", fetched_at=datetime.now(UTC)),
        )
        rid = info.result_id
        probes = {
            "read_csv": "SELECT * FROM read_csv('/etc/hosts')",
            "ATTACH": "ATTACH 'x.db'",
            "COPY": f"COPY {rid} TO 'x.csv'",
            "INSTALL": "INSTALL httpfs",
            "getenv": "SELECT getenv('HOME')",
        }
        for label, sql in probes.items():
            try:
                guard.check(sql, live_ids={rid})
                problems.append(f"the guard accepted {label}")
            except QueryRefused:
                pass
        with store.open([rid]) as session:
            setting = session.query("SELECT current_setting('enable_external_access') AS v").to_pylist()[0][
                "v"
            ]
            if setting not in (False, "false"):
                problems.append("external access is not disabled in result sessions")
            for label, sql in (
                ("read_csv", "SELECT * FROM read_csv('/etc/hosts')"),
                ("COPY", f"COPY {rid} TO 'x.csv'"),
            ):
                try:
                    session.query(sql)
                    problems.append(f"a locked session ran {label}")
                except Exception:  # noqa: S110 - refusal is the expected outcome
                    pass
    return problems


def _network_check(rt) -> tuple[bool, str]:
    import httpx

    key, secret = os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY")
    if not key or not secret:
        return False, "Alpaca clock: skipped (ALPACA_API_KEY and ALPACA_SECRET_KEY are not both set)"
    settings = rt.config.provider_settings("alpaca")
    base = settings.get("trading_url") or (
        "https://api.alpaca.markets"
        if rt.config.portfolio_environment == "live"
        else "https://paper-api.alpaca.markets"
    )
    headers = {
        "APCA-API-KEY-ID": key,
        "APCA-API-SECRET-KEY": secret,
        "User-Agent": f"marketlens-mcp/{__version__}",
    }
    try:
        with httpx.Client(transport=HTTP_TRANSPORT, timeout=15, trust_env=False) as client:
            r = client.get(base.rstrip("/") + "/v2/clock", headers=headers)
    except httpx.HTTPError as exc:
        return False, f"Alpaca clock: failed ({type(exc).__name__})"
    if r.status_code != 200:
        return False, f"Alpaca clock: failed (HTTP {r.status_code})"
    return True, "Alpaca clock: ok"


def cmd_doctor(args: argparse.Namespace) -> int:
    from .server import enabled_entries, startup_notices

    failures = 0
    try:
        rt = _runtime()
    except (ConfigError, BuiltinPluginError) as exc:
        print(f"config: FAILED ({exc})")
        return 1
    print(f"config: ok ({rt.config.display_path}{'' if rt.config.exists else ', not present: all defaults'})")
    print(
        f"version: marketlens-mcp {__version__}, schema {SCHEMA_VERSION}, plugin API {PLUGIN_API_VERSION[0]}.{PLUGIN_API_VERSION[1]}"
    )
    names = sorted({n for e in enabled_entries(rt) for n in e.env})
    for name in names:
        state = "set" if os.environ.get(name) else "unset (tools that need it will refuse)"
        print(f"environment {name}: {state}")
    cache = rt.store_root.cache_dir
    try:
        paths.ensure_private_dir(rt.store_root.results_dir)
        probe = rt.store_root.results_dir / ".doctor-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        usage = rt.store_root.usage()
        print(
            f"cache: ok ({paths.display_path(cache)}; {usage.results} results, {usage.bytes:,} of {usage.max_bytes:,} bytes)"
        )
    except OSError as exc:
        failures += 1
        print(f"cache: FAILED ({paths.display_path(cache)} is not writable: {type(exc).__name__})")
    problems = guard_self_test()
    if problems:
        failures += 1
        print("query guard self-test: FAILED (" + "; ".join(problems) + ")")
    else:
        print(
            "query guard self-test: ok (read_csv, ATTACH, COPY, INSTALL, getenv refused; external access off)"
        )
    for line in startup_notices(rt):
        print(line)
        if line.startswith("plugin "):
            failures += 1
    loaded = [p.name for p in rt.catalog.plugins if p.loaded]
    print(f"plugins: {', '.join(loaded) if loaded else 'none loaded'}")
    print(f"tools enabled: {len(enabled_entries(rt))}")
    if args.network:
        ok, line = _network_check(rt)
        print(line)
        failures += 0 if ok else 1
    return 1 if failures else 0


# --- schema, results, scaffold, readme ----------------------------------------------------------


def cmd_schema(args: argparse.Namespace) -> int:
    from marketlens_schema import jsonschema

    rt = _runtime()
    schemas = jsonschema.export_all(rt.catalog.models)
    index = jsonschema.index(schemas)
    if args.out:
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        for name, schema in schemas.items():
            (out / f"{name}.json").write_text(
                json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        (out / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Wrote {len(schemas)} schemas and index.json to {args.out}")
    else:
        print(json.dumps(index, indent=2, sort_keys=True))
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    from .config import load_config
    from .results.store import StoreLimits, StoreRoot

    cfg = load_config()
    root = StoreRoot(paths.cache_dir(), StoreLimits.from_config(cfg.results, cfg.fetch_limits.max_rows))
    if args.action == "list":
        usage = root.usage()
        print(
            f"{usage.results} result{'s' if usage.results != 1 else ''}, {usage.bytes:,} of {usage.max_bytes:,} bytes in "
            f"{paths.display_path(root.results_dir)}"
            + (f"; oldest created {usage.oldest_created_at.isoformat()}" if usage.oldest_created_at else "")
        )
        return 0
    if not args.yes:
        _err("results purge deletes every stored result of every session; run it again with --yes.")
        return 1
    n = root.purge()
    print(f"Deleted {n} stored result{'s' if n != 1 else ''}.")
    return 0


def cmd_add_capability(args: argparse.Namespace) -> int:
    from .scaffold import add_capability, next_steps

    add_capability(
        args.name,
        capability=args.capability,
        provider=args.provider,
        operation=args.operation,
        model=args.model,
        package=args.package,
        root=args.root,
    )
    print(next_steps(args.name, args.provider, args.package))
    return 0


def cmd_readme(args: argparse.Namespace) -> int:
    from .readme import builtin_table, replace_table

    path = pathlib.Path("README.md")
    if not path.is_file():
        _err("README.md not found in the current folder (run this in a marketlens-mcp checkout).")
        return 1
    text = path.read_text(encoding="utf-8")
    try:
        new = replace_table(text, builtin_table())
    except ValueError as exc:
        _err(f"README.md: {exc}")
        return 1
    if args.check:
        if new != text:
            _err("README.md tool table is out of date; run: make readme")
            return 1
        print("README.md tool table is current.")
        return 0
    if new != text:
        path.write_text(new, encoding="utf-8")
        print("README.md tool table updated.")
    else:
        print("README.md tool table is current.")
    return 0


# --- parser -----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="marketlens-mcp", description="A read-only MCP server for market data and analytics."
    )
    p.add_argument("--version", action="store_true", help="print the version and exit")
    sub = p.add_subparsers(dest="command")

    s = sub.add_parser("serve", help="run the server (default: stdio)")
    s.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    s.add_argument("--port", type=int, default=None, help="HTTP port (default: http.port, 8765)")
    s.add_argument("--host", default="127.0.0.1", help="HTTP host: 127.0.0.1 or ::1")
    s.set_defaults(func=lambda a: cmd_serve(a))

    t = sub.add_parser("tools", help="tools of the installed configuration")
    fmt = t.add_mutually_exclusive_group()
    fmt.add_argument("--json", action="store_true")
    fmt.add_argument("--markdown", action="store_true")
    t.add_argument("--all", action="store_true", help="also list disabled tools")
    t.set_defaults(func=cmd_tools)

    sub.add_parser("capabilities", help="every capability and its state").set_defaults(func=cmd_capabilities)
    sub.add_parser("plugins", help="discovered plugins and their state").set_defaults(func=cmd_plugins)

    c = sub.add_parser("config", help="the config file")
    c.add_argument("action", choices=("path", "show", "init"))
    c.set_defaults(func=cmd_config)

    d = sub.add_parser("doctor", help="check the installation")
    d.add_argument("--network", action="store_true", help="also call the provider once")
    d.set_defaults(func=cmd_doctor)

    sc = sub.add_parser("schema", help="JSON Schemas of the registered models")
    sc.add_argument("--out", default=None)
    sc.set_defaults(func=cmd_schema)

    r = sub.add_parser("results", help="the result store")
    r.add_argument("action", choices=("list", "purge"))
    r.add_argument("--yes", action="store_true")
    r.set_defaults(func=cmd_results)

    a = sub.add_parser("add-capability", help="scaffold a new tool")
    a.add_argument("name")
    a.add_argument("--capability", required=True)
    a.add_argument("--provider", required=True, choices=("alpaca", "local"))
    a.add_argument("--operation", default=None)
    a.add_argument("--model", default=None)
    a.add_argument("--package", default=None)
    a.add_argument("--root", default=None)
    a.set_defaults(func=cmd_add_capability)

    rd = sub.add_parser("readme", help="regenerate (or --check) the README tool table")
    rd.add_argument("--check", action="store_true")
    rd.set_defaults(func=cmd_readme)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    _quiet()
    _logging()
    parser = build_parser()
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.version:
        print(
            f"marketlens-mcp {__version__} (schema {SCHEMA_VERSION}, plugin API "
            f"{PLUGIN_API_VERSION[0]}.{PLUGIN_API_VERSION[1]})"
        )
        return 0
    if args.command is None:
        args = parser.parse_args(["serve"])
    from .scaffold import ScaffoldError
    from .transport import TransportError

    try:
        if args.command == "serve":
            return cmd_serve(args)
        return args.func(args)
    except (ConfigError, TransportError) as exc:
        _err(str(exc))
        return 2
    except BuiltinPluginError as exc:
        _err(f"marketlens-mcp cannot start: {exc}")
        return 2
    except ScaffoldError as exc:
        _err(f"add-capability: {exc}")
        return 1
    except KeyboardInterrupt:
        return 130
