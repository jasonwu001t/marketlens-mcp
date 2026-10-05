"""The configuration file (contract 4.2) and its refusal texts (4.4).

Read once at start. Every violation refuses to start with one readable line.
Secrets never live in the file: they come from the environment only.
Unknown capability ids are judged by the policy (R3), because a plugin's
capability is known only after the plugins named in ``plugins.enabled`` load.
"""

from __future__ import annotations

import copy
import math
import os
import pathlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import yaml

from marketlens_schema import Environment

from . import paths
from .plugin_api import PLUGIN_NAME_RE, FetchLimits

DEFAULT_CONFIG_TEXT = """\
# marketlens-mcp configuration. Restart the server after editing.
version: 1

# Capabilities: omit one to take its default. "results" is always on.
capabilities:
  market: true            # stock, option, crypto, fixed-income market data
  reference: true         # assets, contracts, calendar, clock, corporate actions
  news: true              # news (third-party text, marked untrusted)
  analytics: true         # returns, volatility, correlation, ... on stored results
  macro: true             # FRED, BLS, BEA (needs the data extra)
  filings: true           # SEC EDGAR (needs the data extra)
  fed_treasury: true      # FOMC, NY Fed rates, Treasury curve and auctions (needs the data extra)
  calendars: false        # Nasdaq calendars: an unofficial endpoint (needs the data extra)
  portfolio: false        # brokerage account reads; what the model reads leaves your machine
  results.export: false   # write stored results to files in results.export_dir
  provider.docs: false    # search the provider's documentation (outbound call)

portfolio:
  environment: paper      # paper | live

providers:
  alpaca:
    stock_feed: iex       # iex | sip | delayed_sip | boats | overnight (your plan decides)
    options_feed: indicative   # indicative | opra
    crypto_location: us   # us | us-1 | us-2 | eu-1 | bs-1
    rate_limit_per_minute: 190
    trading_url: null     # override; default by portfolio.environment
    data_url: null        # override; default https://data.alpaca.markets
  data:
    mode: auto            # auto (fetch and keep) | local (read only what is stored)
    data_dir: null        # absolute folder for the data store; default: the per-user data folder
    ttl_hours: 12         # how long a fetched slice is reused before it is fetched again
    calendar_ttl_minutes: 60
    call_timeout_seconds: 120

results:
  inline_max_rows: 200
  inline_max_tokens: 6000
  query_max_rows: 200
  query_max_bytes: 24000
  query_timeout_seconds: 10
  query_memory_limit: 1GB
  ttl_hours: 24
  max_store_gb: 5
  export_dir: null        # absolute path of an existing folder; required for results.export

fetch:
  max_rows: 50000
  max_pages: 20

plugins:
  enabled: []             # entry-point names of installed plugins to load, e.g. [myplugin]
  settings: {}            # per-plugin settings: {myplugin: {...}}

http:
  port: 8765
"""

#: The secrets marketlens reads, only ever from the environment.
SECRET_ENV = (
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "MARKETLENS_HTTP_TOKEN",
    "FRED_API_KEY",
    "BEA_API_KEY",
    "BLS_API_KEY",
)
#: Shown as set/unset by ``config show``: the secrets, and the contact address
#: SEC EDGAR asks for (personal, so its value is not shown either).
SHOWN_ENV = (*SECRET_ENV, "SEC_CONTACT_EMAIL")


class ConfigError(Exception):
    """A configuration the server refuses to start with (one readable line)."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# --- the schema of the file ----------------------------------------------------------------


class _Rule:
    type_text = ""

    def check(self, value: Any) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


class _Int(_Rule):
    type_text = "an integer"

    def __init__(self, lo: int, hi: int):
        self.lo, self.hi = lo, hi

    def check(self, value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)


class _Num(_Rule):
    type_text = "a number"

    def __init__(self, lo: float, hi: float):
        self.lo, self.hi = lo, hi

    def check(self, value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class _Choice(_Rule):
    def __init__(self, *choices: str):
        self.choices = choices
        self.type_text = "one of " + ", ".join(choices)

    def check(self, value: Any) -> bool:
        return isinstance(value, str) and value in self.choices


class _OptStr(_Rule):
    type_text = "a string or null"

    def check(self, value: Any) -> bool:
        return value is None or isinstance(value, str)


class _AbsPathOrNull(_Rule):
    type_text = "an absolute path"

    def check(self, value: Any) -> bool:
        return value is None or (isinstance(value, str) and pathlib.Path(value).expanduser().is_absolute())


_SIZE_RE = re.compile(r"^\d+(?:\.\d+)?\s*(?:KB|MB|GB|TB|KiB|MiB|GiB|TiB)$")


class _Size(_Rule):
    type_text = "a size such as 1GB or 512MB"

    def check(self, value: Any) -> bool:
        return isinstance(value, str) and bool(_SIZE_RE.match(value))


class _Version(_Rule):
    type_text = "1"

    def check(self, value: Any) -> bool:
        return value == 1 and not isinstance(value, bool)


class _PluginNames(_Rule):
    type_text = "a list of plugin names"

    def check(self, value: Any) -> bool:
        return isinstance(value, list) and all(isinstance(v, str) and PLUGIN_NAME_RE.match(v) for v in value)


SCHEMA: dict[str, Any] = {
    "version": _Version(),
    "capabilities": "capabilities",
    "portfolio": {"environment": _Choice("paper", "live")},
    "providers": {
        "alpaca": {
            "stock_feed": _Choice("iex", "sip", "delayed_sip", "boats", "overnight"),
            "options_feed": _Choice("indicative", "opra"),
            "crypto_location": _Choice("us", "us-1", "us-2", "eu-1", "bs-1"),
            "rate_limit_per_minute": _Int(1, 199),
            "trading_url": _OptStr(),
            "data_url": _OptStr(),
        },
        "data": {
            "mode": _Choice("auto", "local"),
            "data_dir": _AbsPathOrNull(),
            "ttl_hours": _Num(1, 168),
            "calendar_ttl_minutes": _Int(5, 1440),
            "call_timeout_seconds": _Int(10, 600),
        },
    },
    "results": {
        "inline_max_rows": _Int(1, 1000),
        "inline_max_tokens": _Int(500, 20000),
        "query_max_rows": _Int(1, 200),
        "query_max_bytes": _Int(1000, 200000),
        "query_timeout_seconds": _Num(1, 120),
        "query_memory_limit": _Size(),
        "ttl_hours": _Num(1, 168),
        "max_store_gb": _Num(0.1, 100),
        "export_dir": _OptStr(),
    },
    "fetch": {"max_rows": _Int(1, 1_000_000), "max_pages": _Int(1, 200)},
    "plugins": {"enabled": _PluginNames(), "settings": "plugin_settings"},
    "http": {"port": _Int(1024, 65535)},
}


def _defaults() -> dict[str, Any]:
    data = yaml.safe_load(DEFAULT_CONFIG_TEXT)
    data.pop("capabilities")
    return data


#: Every setting's default (the init file without its capabilities section:
#: an omitted capability takes its declaration's default).
DEFAULTS: dict[str, Any] = _defaults()


def _fmt_num(x: float) -> str:
    return str(int(x)) if isinstance(x, float) and x.is_integer() else str(x)


@dataclass(frozen=True)
class Config:
    """The validated configuration, defaults merged in."""

    path: pathlib.Path
    exists: bool
    data: Mapping[str, Any]
    capabilities: Mapping[str, bool] = field(default_factory=dict)

    @property
    def display_path(self) -> str:
        return paths.display_path(self.path)

    @property
    def portfolio_environment(self) -> Environment:
        return Environment(self.data["portfolio"]["environment"])

    @property
    def results(self) -> Mapping[str, Any]:
        return self.data["results"]

    @property
    def fetch_limits(self) -> FetchLimits:
        f = self.data["fetch"]
        return FetchLimits(max_rows=f["max_rows"], max_pages=f["max_pages"])

    @property
    def plugins_enabled(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(self.data["plugins"]["enabled"]))

    def plugin_settings(self, name: str) -> Mapping[str, Any]:
        return copy.deepcopy(self.data["plugins"]["settings"].get(name) or {})

    def provider_settings(self, name: str) -> Mapping[str, Any]:
        return copy.deepcopy(self.data["providers"].get(name) or {})

    @property
    def http_port(self) -> int:
        return self.data["http"]["port"]

    def effective_yaml(
        self, env: Mapping[str, str] | None = None, *, capabilities: Mapping[str, bool] | None = None
    ) -> str:
        """The effective configuration as YAML; secrets as set/unset comments.
        ``capabilities`` (the resolved policy) replaces the configured values."""
        e = os.environ if env is None else env
        body = {
            "version": 1,
            "capabilities": dict(self.capabilities if capabilities is None else capabilities),
            **{k: v for k, v in self.data.items() if k != "version"},
        }
        text = f"# effective marketlens config (file: {self.display_path}{'' if self.exists else ', not present'})\n"
        text += yaml.safe_dump(body, sort_keys=False, default_flow_style=False)
        text += "# environment (values never shown):\n"
        for name in SHOWN_ENV:
            text += f"# {name}: {'set' if e.get(name) else 'unset'}\n"
        return text


def _refuse(shown: str, text: str) -> ConfigError:
    return ConfigError(f"marketlens config {shown}: {text}")


def _validate(
    node: Any,
    schema: dict[str, Any],
    prefix: str,
    shown: str,
    out: dict[str, Any],
    enabled_plugins: list[str],
) -> None:
    for key, value in node.items():
        dotted = f"{prefix}{key}"
        if key not in schema:
            known = ", ".join(schema)
            raise _refuse(shown, f"unknown setting '{dotted}'. Known settings here: {known}.")
        rule = schema[key]
        if isinstance(rule, dict):
            if value is None:
                value = {}
            if not isinstance(value, dict):
                raise _refuse(shown, f"{dotted} must be a mapping (got {value!r}).")
            out.setdefault(key, {})
            _validate(value, rule, dotted + ".", shown, out[key], enabled_plugins)
        elif rule == "capabilities":
            caps = {} if value is None else value
            if not isinstance(caps, dict):
                raise _refuse(shown, f"{dotted} must be a mapping (got {value!r}).")
            for cap, on in caps.items():
                if cap == "results":
                    raise _refuse(shown, "capability 'results' is always on and cannot be configured.")
                if not isinstance(on, bool):
                    raise _refuse(shown, f"{dotted}.{cap} must be true or false (got {on!r}).")
            out[key] = dict(caps)
        elif rule == "plugin_settings":
            settings = {} if value is None else value
            if not isinstance(settings, dict):
                raise _refuse(shown, f"{dotted} must be a mapping (got {value!r}).")
            for name, section in settings.items():
                if name not in enabled_plugins:
                    raise _refuse(
                        shown,
                        f"unknown setting '{dotted}.{name}'. Known settings here: {', '.join(enabled_plugins)}.",
                    )
                if section is not None and not isinstance(section, dict):
                    raise _refuse(shown, f"{dotted}.{name} must be a mapping (got {section!r}).")
            out[key] = {k: (v or {}) for k, v in settings.items()}
        else:
            if not rule.check(value):
                raise _refuse(shown, f"{dotted} must be {rule.type_text} (got {value!r}).")
            if isinstance(rule, (_Int, _Num)) and not (rule.lo <= value <= rule.hi):
                raise _refuse(
                    shown,
                    f"{dotted} must be between {_fmt_num(rule.lo)} and {_fmt_num(rule.hi)} (got {value}).",
                )
            out[key] = value


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Read and validate the config file (or take every default when the
    default file is absent). Raises ConfigError with the contract's texts."""
    e = os.environ if env is None else env
    path = paths.config_path(e)
    shown = paths.display_path(path, home=paths.home_dir(e))
    if paths.config_path_is_explicit(e) and not path.is_file():
        raise ConfigError(f"MARKETLENS_CONFIG points to {shown}, which does not exist.")
    data = copy.deepcopy(DEFAULTS)
    if not path.is_file():
        return Config(path=path, exists=False, data=data)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        raise ConfigError(f"marketlens config {shown} is not valid YAML: {first}") from None
    if raw is None:
        return Config(path=path, exists=True, data=data)
    if not isinstance(raw, dict):
        raise _refuse(shown, f"the top level must be a mapping (got {raw!r}).")
    plugins = raw.get("plugins")
    enabled = []
    if isinstance(plugins, dict) and _PluginNames().check(plugins.get("enabled", [])):
        enabled = list(plugins.get("enabled") or [])
    out: dict[str, Any] = {}
    _validate(raw, SCHEMA, "", shown, out, enabled)
    capabilities = out.pop("capabilities", {})
    for section, values in out.items():
        if isinstance(values, dict) and isinstance(data.get(section), dict):
            _merge(data[section], values)
        else:
            data[section] = values
    if capabilities.get("results.export") is True:
        export_dir = data["results"]["export_dir"]
        if (
            not export_dir
            or not pathlib.Path(export_dir).is_absolute()
            or not pathlib.Path(export_dir).is_dir()
        ):
            raise _refuse(
                shown, "results.export is on, so results.export_dir must be an existing absolute folder."
            )
    return Config(path=path, exists=True, data=data, capabilities=capabilities)


def _merge(base: dict[str, Any], over: dict[str, Any]) -> None:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict) and k != "settings":
            _merge(base[k], v)
        else:
            base[k] = v
