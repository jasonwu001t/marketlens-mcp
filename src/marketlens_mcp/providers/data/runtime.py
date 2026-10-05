"""How the data tools reach marketlens-data (import name ``omni``).

* ``available()`` says whether omni is importable, without importing it.
  Registration depends on this alone.
* ``ensure()`` runs before the first omni call: marketlens never lets omni
  read a ``.env`` (``OMNI_ENV_FILE=""``), points omni's store at the folder
  of ``store_path()``, imports omni and checks the names the tools use.
* Every omni call runs on ONE process-wide worker thread (omni is synchronous
  and its store locks per dataset; one worker removes every ordering doubt).
  ``call()`` waits ``providers.data.call_timeout_seconds``; past that the work
  continues in the background and the tool answers D7.
* ``map_error()`` turns what omni raises into the readable D-codes. Messages
  never carry a key's value or a filesystem path.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import logging
import os
import pathlib
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

from marketlens_mcp import paths
from marketlens_mcp.plugin_api import ToolError
from marketlens_mcp.results.guard import scrub

from . import sources

T = TypeVar("T")
log = logging.getLogger("marketlens.data")

DIST = "marketlens-data"
#: The folder name of the per-user data store (the same rule marketlens-data uses).
APP_DIR = "marketlens-data"

DEFAULTS: dict[str, Any] = {
    "mode": "auto",
    "data_dir": None,
    "ttl_hours": 12,
    "calendar_ttl_minutes": 60,
    "call_timeout_seconds": 120,
}

#: The marketlens-data names the tools use; all of them belong to its public surface.
REQUIRED_API: tuple[str, ...] = (
    "omni.query",
    "omni.read_raw",
    "omni.sync",
    "omni.list_datasets",
    "omni.sources.base.resolve",
    "omni.sources.base.get_source",
    "omni.sources.base.authority_of",
    "omni.store.get_store",
    "omni.calendars.capture_day",
    "omni.calendars.capture_ticker_history",
    "omni.catalog.series_meta",
    "omni.catalog.TENOR_LABELS",
    "omni.config.MissingCredential",
    "omni.errors.classify",
    "omni.errors.PermanentError",
)

D1_TEXT = (
    "The data tools need the marketlens-data package: install marketlens-mcp[data] (Python 3.13 or later)."
)


# --- settings and the store's folder -----------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    mode: str
    data_dir: str | None
    ttl_seconds: int
    calendar_ttl_seconds: int
    call_timeout_seconds: float


def settings_of(section: Mapping[str, Any]) -> Settings:
    """``providers.data`` (validated by the config loader) with defaults filled in."""
    s = {**DEFAULTS, **dict(section or {})}
    return Settings(
        mode=str(s["mode"]),
        data_dir=s["data_dir"],
        ttl_seconds=int(float(s["ttl_hours"]) * 3600),
        calendar_ttl_seconds=int(s["calendar_ttl_minutes"]) * 60,
        call_timeout_seconds=float(s["call_timeout_seconds"]),
    )


def default_data_dir(env: Mapping[str, str] | None = None, *, platform: str | None = None) -> pathlib.Path:
    """The per-user data folder: macOS ``~/Library/Application Support/marketlens-data``;
    Windows ``%LOCALAPPDATA%\\marketlens-data``; elsewhere ``$XDG_DATA_HOME/marketlens-data``
    (when absolute) or ``~/.local/share/marketlens-data``."""
    e = os.environ if env is None else env
    platform = platform or sys.platform
    home = paths.home_dir(e, platform=platform)
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_DIR
    if platform == "win32":
        base = pathlib.Path(e["LOCALAPPDATA"]) if e.get("LOCALAPPDATA") else home / "AppData" / "Local"
        return base / APP_DIR
    xdg = e.get("XDG_DATA_HOME")
    if xdg and pathlib.Path(xdg).is_absolute():
        return pathlib.Path(xdg) / APP_DIR
    return home / ".local" / "share" / APP_DIR


def store_path(
    settings: Settings | Mapping[str, Any],
    env: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> pathlib.Path:
    """``providers.data.data_dir``, else ``OMNI_DATA_DIR``, else the per-user folder."""
    e = os.environ if env is None else env
    configured = settings.data_dir if isinstance(settings, Settings) else dict(settings or {}).get("data_dir")
    if configured:
        return pathlib.Path(configured).expanduser()
    value = (e.get("OMNI_DATA_DIR") or "").strip()
    if value:
        p = pathlib.Path(value).expanduser()
        return p if p.is_absolute() else (pathlib.Path.cwd() / p).resolve()
    return default_data_dir(e, platform=platform)


# --- the extra ---------------------------------------------------------------------------------


def available() -> bool:
    """True when omni can be imported (nothing is imported here)."""
    try:
        return importlib.util.find_spec("omni") is not None
    except (ImportError, ValueError):
        return False


def api_problems() -> list[str]:
    """The REQUIRED_API names omni lacks (imports omni)."""
    missing = []
    for dotted in REQUIRED_API:
        module, _, attr = dotted.rpartition(".")
        try:
            mod = importlib.import_module(module)
        except Exception:
            missing.append(dotted)
            continue
        if not hasattr(mod, attr):
            missing.append(dotted)
    return missing


def distribution() -> tuple[str, str]:
    """(distribution name, version) of the installed omni."""
    try:
        dists = importlib.metadata.packages_distributions().get("omni") or []
    except Exception:
        dists = []
    name = DIST if DIST in dists else (dists[0] if dists else "omni")
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        version = str(getattr(sys.modules.get("omni"), "__version__", "unknown"))
    return name, version


def prepare_environment() -> None:
    """marketlens never lets omni read a .env file: keys come from the server's environment."""
    os.environ["OMNI_ENV_FILE"] = ""


_lock = threading.Lock()
_ready_path: pathlib.Path | None = None


def ensure(settings: Settings) -> Any:
    """Point omni at the store, import it and check its API. Returns the omni module.
    Raises ToolError D1 (missing or incomplete) or D8 (the folder cannot be created)."""
    global _ready_path
    with _lock:
        path = store_path(settings)
        if _ready_path == path and "omni" in sys.modules:
            return sys.modules["omni"]
        prepare_environment()
        os.environ["OMNI_DATA_DIR"] = str(path)
        if not available():
            raise ToolError("data_extra_missing", D1_TEXT)
        try:
            omni = importlib.import_module("omni")
        except Exception:
            log.warning("marketlens-data could not be imported", exc_info=True)
            raise ToolError("data_extra_missing", D1_TEXT) from None
        missing = api_problems()
        if missing:
            log.warning("marketlens-data lacks %s", ", ".join(missing))
            raise ToolError("data_extra_missing", D1_TEXT)
        try:
            paths.ensure_private_dir(path)
        except OSError as exc:
            raise store_error(exc) from None
        _ready_path = path
        return omni


def reset() -> None:
    """Forget the prepared store (tests)."""
    global _ready_path
    with _lock:
        _ready_path = None


# --- the worker thread -------------------------------------------------------------------------

_pool_lock = threading.Lock()
_pool: ThreadPoolExecutor | None = None


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="marketlens-data")
        return _pool


def _consume(future: asyncio.Future) -> None:
    if not future.cancelled():
        future.exception()  # retrieved: an abandoned fetch's failure is not "never retrieved"


async def call(
    settings: Settings,
    fn: Callable[[Any], T],
    *,
    source: str,
    tool: str,
    subject: tuple[str, str] | None = None,
) -> T:
    """Run ``fn(omni)`` on the worker thread after ``ensure``; map its failure."""

    def job() -> T:
        omni = ensure(settings)
        return fn(omni)

    future = asyncio.get_running_loop().run_in_executor(_executor(), job)
    future.add_done_callback(_consume)
    try:
        return await asyncio.wait_for(asyncio.shield(future), settings.call_timeout_seconds)
    except TimeoutError:
        raise ToolError(
            "data_timeout",
            f"{sources.label(source)} is still fetching after {settings.call_timeout_seconds:g} s; the fetch "
            f"continues in the background, so call {tool} again in a minute.",
            retryable=True,
        ) from None
    except ToolError:
        raise
    except Exception as exc:
        raise map_error(exc, source=source, tool=tool, subject=subject) from None


async def drain() -> None:
    """Wait until the worker has finished what it was given (tests)."""
    await asyncio.wrap_future(_executor().submit(lambda: None))


# --- errors ------------------------------------------------------------------------------------


def redact(text: str) -> str:
    """Every key value of the data sources present in the environment, then every path, removed."""
    values = sorted(
        {v for name in sources.KEY_VARIABLES if len(v := (os.environ.get(name) or "").strip()) >= 4},
        key=len,
        reverse=True,
    )
    for value in values:
        text = text.replace(value, "<redacted>")
    known = [os.environ.get("OMNI_DATA_DIR") or ""]
    return scrub(text, *known)


def first_line(exc: BaseException, limit: int = 300) -> str:
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    text = lines[0] if lines else type(exc).__name__
    text = redact(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def store_error(exc: BaseException) -> ToolError:
    return ToolError(
        "data_store",
        f"The local data store cannot be read or written ({type(exc).__name__}); check "
        "providers.data.data_dir.",
    )


def _status(exc: BaseException) -> int | None:
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None)


def map_error(
    exc: BaseException, *, source: str, tool: str, subject: tuple[str, str] | None = None
) -> ToolError:
    """D2-D10 for an exception raised by omni (ToolError passes through)."""
    if isinstance(exc, ToolError):
        return exc
    import httpx
    from omni import config as omni_config
    from omni import errors as omni_errors
    from omni.http.client import NotFound

    label = sources.label(source)
    if isinstance(exc, omni_config.MissingCredential):
        key = sources.BY_SOURCE.get(source)
        var = key.variables[0] if key and key.variables else "its key"
        url = key.url if key and key.url else "the publisher's site"
        return ToolError(
            "data_key_missing",
            f"{label} needs {var}, which is not set in this server's environment.",
            hint=f"Get a free key at {url} and add {var} to the env block of this server in your MCP "
            "client's configuration.",
        )
    store_lock = getattr(sys.modules.get("omni.store"), "StoreLockTimeout", None)
    if store_lock is not None and isinstance(exc, store_lock):
        return store_error(exc)
    if isinstance(exc, omni_errors.PermanentError):
        kind, value = subject or ("data", "this request")
        return ToolError("data_not_found", f"{label} has no {kind} for {value}.")
    kind, reason = omni_errors.classify(exc)
    if kind == omni_errors.TRANSIENT:
        return ToolError(
            "data_unavailable",
            f"{label} did not answer ({redact(str(reason))}); try again later.",
            retryable=True,
        )
    if isinstance(exc, (TypeError, ValueError, NotFound)) or (
        isinstance(exc, httpx.HTTPStatusError) and _status(exc) in (400, 404, 422)
    ):
        return ToolError("data_rejected", f"{label} refused the request: {first_line(exc)}")
    if isinstance(exc, OSError):
        return store_error(exc)
    log.warning("%s: %s failed", tool, label, exc_info=exc)
    return ToolError("data_upstream", f"{label} failed: {first_line(exc)}")
