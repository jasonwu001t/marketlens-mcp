"""Registration of built-ins and plugins, and the contexts handed to them
(contract 3.2 and 3.3).

1. The server declares BUILTIN_CAPABILITIES and BUILTIN_MODELS.
2. Each built-in in ``builtins.BUILTIN_PLUGINS`` registers; any failure stops
   the server (exit 2; the message names the module).
3. Entry points in ``marketlens.plugins`` are discovered; only names listed in
   ``plugins.enabled`` are imported. Each registers into a fresh staging
   registry; everything is validated; the plugin is committed whole or
   dropped whole with a reason (R13). A broken plugin never stops the server.
"""

from __future__ import annotations

import dataclasses
import importlib
import importlib.metadata
import logging
import os
import types
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

from marketlens_schema import BUILTIN_MODELS, CanonicalModel, Environment

from . import __version__, fetch, manifest, ratelimit
from .builtins import BUILTIN_PLUGINS
from .config import Config
from .plugin_api import (
    BUILTIN_CAPABILITIES,
    CAPABILITY_ID_RE,
    ENTRY_POINT_GROUP,
    PLUGIN_API_VERSION,
    PLUGIN_NAME_RE,
    CapabilitySpec,
    FetchLimits,
    FetchPage,
    PageResult,
    PluginInfo,
    RegistrationError,
    ToolSpec,
)
from .results_api import ResultStore

__all__ = [
    "BUILTIN_PLUGIN_NAMES",
    "BuiltinPluginError",
    "Catalog",
    "EntryPointRef",
    "FetchLimits",
    "PluginStatus",
    "ServerToolContext",
    "StagingRegistry",
    "ToolEntry",
    "build_catalog",
    "discover_entry_points",
    "skip_message",
]

T = TypeVar("T")

#: Names a plugin may not take (the built-ins and the server itself).
BUILTIN_PLUGIN_NAMES = frozenset({"results", "alpaca", "analytics", "core", "marketlens"})

log = logging.getLogger("marketlens")


class BuiltinPluginError(RuntimeError):
    """A built-in failed to register: our bug; the server must not start."""


@dataclass(frozen=True)
class ToolEntry:
    spec: ToolSpec
    plugin: str  # "builtin:alpaca" or the plugin's name
    env: tuple[str, ...]  # names the tool may read: spec.env + the plugin's env


@dataclass
class PluginStatus:
    name: str
    distribution: str | None
    enabled: bool
    loaded: bool
    api_version: tuple[int, int] | None
    error: str | None


@dataclass
class Catalog:
    """Everything registered: capabilities, tools, models, plugin status."""

    capabilities: list[CapabilitySpec] = field(default_factory=list)
    tools: dict[str, ToolEntry] = field(default_factory=dict)
    models: dict[str, type[CanonicalModel]] = field(default_factory=dict)
    plugins: list[PluginStatus] = field(default_factory=list)
    settings: dict[str, Mapping[str, Any]] = field(default_factory=dict)  # plugin/builtin name -> its section
    shutdown: list[Callable[[], Awaitable[None]]] = field(default_factory=list)

    @property
    def capability_ids(self) -> list[str]:
        return [c.id for c in self.capabilities]


def skip_message(status: PluginStatus) -> str:
    """R13."""
    return f"plugin '{status.name}' not loaded: {status.error}"


# --- staging registry -------------------------------------------------------------------------


class StagingRegistry:
    """What ``register`` receives. Validates every call immediately; nothing
    reaches the catalog until ``commit``."""

    def __init__(self, owner: str, *, builtin: bool, catalog: Catalog, env: Sequence[str] = ()):
        self.owner = owner
        self.builtin = builtin
        self._catalog = catalog
        self._env = tuple(env)
        self.capabilities: list[CapabilitySpec] = []
        self.tools: list[ToolSpec] = []
        self.models: dict[str, type[CanonicalModel]] = {}

    def _all_capability_ids(self) -> set[str]:
        return {c.id for c in self._catalog.capabilities} | {c.id for c in self.capabilities}

    def _all_models(self) -> dict[str, type[CanonicalModel]]:
        return {**self._catalog.models, **self.models}

    def add_capability(self, spec: CapabilitySpec) -> None:
        if not isinstance(spec, CapabilitySpec):
            raise RegistrationError(f"add_capability needs a CapabilitySpec, got {type(spec).__name__}")
        if not CAPABILITY_ID_RE.match(spec.id):
            raise RegistrationError(
                f"capability id {spec.id!r} is not valid (e.g. 'research' or 'myplugin.extra')"
            )
        if spec.id in {c.id for c in BUILTIN_CAPABILITIES}:
            raise RegistrationError(f"capability '{spec.id}' is built in and cannot be declared again")
        if spec.id in self._all_capability_ids():
            raise RegistrationError(f"capability '{spec.id}' is already declared")
        if not spec.title.strip() or not spec.description.strip():
            raise RegistrationError(f"capability '{spec.id}' needs a title and a description")
        self.capabilities.append(
            dataclasses.replace(spec, declared_by="builtin" if self.builtin else self.owner)
        )

    def add_model(self, model: type[CanonicalModel]) -> None:
        if not (isinstance(model, type) and issubclass(model, CanonicalModel)):
            raise RegistrationError(f"add_model needs a CanonicalModel subclass, got {model!r}")
        name = model.schema_name
        if not name:
            raise RegistrationError(f"model {model.__name__} has no schema_name")
        prefix = "marketlens." if self.builtin else f"{self.owner}."
        if not name.startswith(prefix):
            raise RegistrationError(f"model schema name '{name}' must start with '{prefix}'")
        existing = self._all_models().get(name)
        if existing is model:
            return
        if existing is not None:
            raise RegistrationError(f"model schema name '{name}' is already registered")
        try:
            from .results.arrow import arrow_schema

            arrow_schema(model)
            model.model_json_schema()
        except Exception as exc:
            raise RegistrationError(f"model '{name}' has a field marketlens cannot store: {exc}") from None
        if model.model_config.get("extra") != "forbid":
            raise RegistrationError(f"model '{name}' must forbid extra fields")
        self.models[name] = model

    def add_tool(self, spec: ToolSpec) -> None:
        name = getattr(spec, "name", None)
        if name in self._catalog.tools or any(t.name == name for t in self.tools):
            raise RegistrationError(f"tool name '{name}' is already registered")
        manifest.validate_spec(spec, capabilities=self._all_capability_ids(), models=self._all_models())
        self.tools.append(spec)

    def commit(self) -> None:
        label = f"builtin:{self.owner}" if self.builtin else self.owner
        self._catalog.capabilities.extend(self.capabilities)
        self._catalog.models.update(self.models)
        for spec in self.tools:
            env = tuple(dict.fromkeys((*spec.env, *self._env)))
            self._catalog.tools[spec.name] = ToolEntry(spec=spec, plugin=label, env=env)


# --- contexts ---------------------------------------------------------------------------------


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return types.MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


class _EnvReader:
    def __init__(self, names: Iterable[str] | None, source: Mapping[str, str] | None):
        self._names = None if names is None else frozenset(names)
        self._source = source

    def __call__(self, name: str) -> str | None:
        if self._names is not None and name not in self._names:
            raise KeyError(name)
        source = os.environ if self._source is None else self._source
        return source.get(name) or None


class PluginCtx:
    """PluginContext implementation (no I/O)."""

    def __init__(
        self,
        name: str,
        *,
        settings: Mapping[str, Any],
        portfolio_environment: Environment,
        env_names: Iterable[str],
        shutdown: list[Callable[[], Awaitable[None]]],
    ):
        self._name = name
        self._settings = _freeze(dict(settings))
        self._env = _EnvReader(env_names, None)
        self._portfolio = Environment(portfolio_environment)
        self._shutdown = shutdown
        self._log = logging.getLogger(f"marketlens.plugin.{name}")

    @property
    def name(self) -> str:
        return self._name

    @property
    def server_version(self) -> str:
        return __version__

    @property
    def settings(self) -> Mapping[str, Any]:
        return self._settings

    @property
    def portfolio_environment(self) -> Environment:
        return self._portfolio

    @property
    def log(self) -> logging.Logger:
        return self._log

    def env(self, name: str) -> str | None:
        return self._env(name)

    def on_shutdown(self, callback: Callable[[], Awaitable[None]]) -> None:
        self._shutdown.append(callback)


class ServerToolContext:
    """ToolContext implementation used by the server and by marketlens_mcp.testing."""

    def __init__(
        self,
        *,
        tool: str,
        session_id: str,
        settings: Mapping[str, Any],
        capabilities: Iterable[str],
        portfolio_environment: Environment | str,
        limits: FetchLimits,
        results: ResultStore | None,
        log: logging.Logger | None,
        env_names: Iterable[str] | None,
        env_source: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
        results_factory: Callable[[], ResultStore] | None = None,
    ):
        self._tool = tool
        self._session_id = session_id
        self._settings = _freeze(dict(settings))
        self._capabilities = frozenset(capabilities)
        self._portfolio = Environment(portfolio_environment)
        self._limits = limits
        self._results = results
        self._results_factory = results_factory
        self._log = log or logging.getLogger(f"marketlens.tool.{tool}")
        self._env = _EnvReader(env_names, env_source)
        self._clock = clock

    @property
    def tool(self) -> str:
        return self._tool

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def settings(self) -> Mapping[str, Any]:
        return self._settings

    @property
    def capabilities(self) -> frozenset[str]:
        return self._capabilities

    @property
    def portfolio_environment(self) -> Environment:
        return self._portfolio

    @property
    def limits(self) -> FetchLimits:
        return self._limits

    @property
    def results(self) -> ResultStore:
        if self._results is None and self._results_factory is not None:
            self._results = self._results_factory()
        if self._results is None:
            raise RuntimeError("this context has no result store")
        return self._results

    @property
    def log(self) -> logging.Logger:
        return self._log

    def env(self, name: str) -> str | None:
        return self._env(name)

    def now(self) -> datetime:
        return self._clock() if self._clock else datetime.now(UTC)

    async def paginate(self, fetch_page: FetchPage[T], *, start_token: str | None = None) -> PageResult[T]:
        return await fetch.paginate(fetch_page, self._limits, start_token=start_token)

    def limiter(self, key: str, per_minute: int) -> ratelimit.TokenBucket:
        return ratelimit.get_limiter(key, per_minute)


# --- discovery and loading ------------------------------------------------------------------


@dataclass(frozen=True)
class EntryPointRef:
    name: str
    value: str
    distribution: str | None
    load: Callable[[], Any]


def discover_entry_points() -> list[EntryPointRef]:
    """Entry points in the ``marketlens.plugins`` group (nothing is imported)."""
    out = []
    for ep in importlib.metadata.entry_points(group=ENTRY_POINT_GROUP):
        dist = getattr(getattr(ep, "dist", None), "name", None)
        out.append(EntryPointRef(name=ep.name, value=ep.value, distribution=dist, load=ep.load))
    return out


def _import_object(path: str) -> Any:
    module, _, attr = path.partition(":")
    obj = importlib.import_module(module)
    for part in attr.split("."):
        obj = getattr(obj, part)
    return obj


def _version_ok(version: Any) -> bool:
    return (
        isinstance(version, tuple)
        and len(version) == 2
        and version[0] == PLUGIN_API_VERSION[0]
        and version[1] <= PLUGIN_API_VERSION[1]
    )


def _fmt_version(version: Any) -> str:
    if isinstance(version, tuple) and len(version) == 2:
        return f"{version[0]}.{version[1]}"
    return repr(version)


def _builtin_settings(config: Config, name: str) -> Mapping[str, Any]:
    if name == "results":
        return dict(config.results)
    return config.provider_settings(name)


def build_catalog(
    config: Config,
    *,
    discover: Callable[[], Iterable[Any]] = discover_entry_points,
    builtins: Sequence[str] = BUILTIN_PLUGINS,
) -> Catalog:
    cat = Catalog(capabilities=list(BUILTIN_CAPABILITIES), models=dict(BUILTIN_MODELS))
    env_mode = config.portfolio_environment

    for path in builtins:
        module = path.partition(":")[0]
        try:
            info = _import_object(path)
            if not isinstance(info, PluginInfo):
                raise RegistrationError(f"{path} is not a PluginInfo")
            if not _version_ok(info.api_version):
                raise RegistrationError(f"plugin API {_fmt_version(info.api_version)} is not supported")
            staging = StagingRegistry(info.name, builtin=True, catalog=cat, env=info.env)
            settings = _builtin_settings(config, info.name)
            ctx = PluginCtx(
                info.name,
                settings=settings,
                portfolio_environment=env_mode,
                env_names=info.env,
                shutdown=cat.shutdown,
            )
            info.register(staging, ctx)
            staging.commit()
            cat.settings[info.name] = settings
        except Exception as exc:
            raise BuiltinPluginError(
                f"built-in {module} failed to register: {type(exc).__name__}: {exc}"
            ) from exc

    enabled = list(config.plugins_enabled)
    found: dict[str, list[Any]] = {}
    discovery_error = None
    try:
        for ep in discover():
            found.setdefault(ep.name, []).append(ep)
    except Exception as exc:
        discovery_error = f"entry-point discovery failed: {type(exc).__name__}: {exc}"
        log.warning("marketlens %s", discovery_error)
        found = {}

    for name in sorted(found):
        if name not in enabled:
            first = found[name][0]
            cat.plugins.append(
                PluginStatus(name, getattr(first, "distribution", None), False, False, None, None)
            )

    for name in enabled:
        eps = found.get(name, [])
        status = PluginStatus(
            name, getattr(eps[0], "distribution", None) if eps else None, True, False, None, None
        )
        cat.plugins.append(status)
        if not eps:
            status.error = discovery_error or "not installed"
            continue
        if len(eps) > 1:
            dists = ", ".join(str(getattr(e, "distribution", None)) for e in eps)
            status.error = f"installed twice ({dists}); uninstall one"
            continue
        if not PLUGIN_NAME_RE.match(name):
            status.error = f"plugin name {name!r} is not valid"
            continue
        if name in BUILTIN_PLUGIN_NAMES:
            status.error = f"the name '{name}' is reserved for marketlens itself"
            continue
        try:
            obj = eps[0].load()
        except (Exception, SystemExit) as exc:  # a plugin calling sys.exit is broken, not fatal
            status.error = f"could not be imported: {type(exc).__name__}: {exc}"
            continue
        if not isinstance(obj, PluginInfo):
            status.error = f"the entry point object is not a PluginInfo ({type(obj).__name__})"
            continue
        status.api_version = obj.api_version if isinstance(obj.api_version, tuple) else None
        if obj.name != name:
            status.error = f"the entry point is '{name}' but the plugin names itself '{obj.name}'"
            continue
        if not _version_ok(obj.api_version):
            status.error = (
                f"plugin API {_fmt_version(obj.api_version)} is not supported by this server "
                f"(plugin API {_fmt_version(PLUGIN_API_VERSION)})"
            )
            continue
        staging = StagingRegistry(name, builtin=False, catalog=cat, env=obj.env)
        settings = config.plugin_settings(name)
        shutdown: list[Callable[[], Awaitable[None]]] = []
        ctx = PluginCtx(
            name, settings=settings, portfolio_environment=env_mode, env_names=obj.env, shutdown=shutdown
        )
        try:
            obj.register(staging, ctx)
        except RegistrationError as exc:
            status.error = str(exc)
            continue
        except (Exception, SystemExit) as exc:
            status.error = f"register() raised {type(exc).__name__}: {exc}"
            continue
        staging.commit()
        cat.shutdown.extend(shutdown)
        cat.settings[name] = settings
        status.loaded = True
    return cat


def new_process_id() -> str:
    return uuid.uuid4().hex
