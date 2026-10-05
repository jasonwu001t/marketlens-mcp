"""Where marketlens keeps its config file and its cache (contract 4.1).

Computed here rather than with platformdirs: the config file is
``$MARKETLENS_CONFIG``, else ``$XDG_CONFIG_HOME/marketlens/config.yaml`` when
``XDG_CONFIG_HOME`` is absolute, else ``~/.config/marketlens/config.yaml`` on
every platform. The cache is ``$MARKETLENS_CACHE_DIR``, else the platform's
user cache folder.
"""

from __future__ import annotations

import os
import pathlib
import sys
from collections.abc import Mapping

APP = "marketlens"


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def home_dir(env: Mapping[str, str] | None = None, *, platform: str | None = None) -> pathlib.Path:
    e = _env(env)
    platform = platform or sys.platform
    if platform == "win32" and e.get("USERPROFILE"):
        return pathlib.Path(e["USERPROFILE"])
    if e.get("HOME"):
        return pathlib.Path(e["HOME"])
    return pathlib.Path.home()


def config_path_is_explicit(env: Mapping[str, str] | None = None) -> bool:
    return bool(_env(env).get("MARKETLENS_CONFIG"))


def config_path(env: Mapping[str, str] | None = None, *, platform: str | None = None) -> pathlib.Path:
    e = _env(env)
    if e.get("MARKETLENS_CONFIG"):
        return pathlib.Path(e["MARKETLENS_CONFIG"]).expanduser()
    xdg = e.get("XDG_CONFIG_HOME")
    if xdg and pathlib.Path(xdg).is_absolute():
        return pathlib.Path(xdg) / APP / "config.yaml"
    return home_dir(e, platform=platform) / ".config" / APP / "config.yaml"


def cache_dir(env: Mapping[str, str] | None = None, *, platform: str | None = None) -> pathlib.Path:
    e = _env(env)
    platform = platform or sys.platform
    if e.get("MARKETLENS_CACHE_DIR"):
        return pathlib.Path(e["MARKETLENS_CACHE_DIR"]).expanduser()
    home = home_dir(e, platform=platform)
    if platform == "darwin":
        return home / "Library" / "Caches" / APP
    if platform == "win32":
        base = pathlib.Path(e["LOCALAPPDATA"]) if e.get("LOCALAPPDATA") else home / "AppData" / "Local"
        return base / APP / "Cache"
    xdg = e.get("XDG_CACHE_HOME")
    if xdg and pathlib.Path(xdg).is_absolute():
        return pathlib.Path(xdg) / APP
    return home / ".cache" / APP


def display_path(path: pathlib.Path | str, *, home: pathlib.Path | None = None) -> str:
    """``path`` with the home directory shown as ``~`` (forward slashes after it)."""
    p = pathlib.Path(path)
    h = home if home is not None else home_dir()
    try:
        rel = p.relative_to(h)
    except ValueError:
        return str(p)
    return "~" if str(rel) == "." else "~/" + rel.as_posix()


def ensure_private_dir(path: pathlib.Path) -> pathlib.Path:
    """Create ``path`` (and parents) and make it owner-only on POSIX."""
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path
