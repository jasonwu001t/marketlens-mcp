"""Config and cache locations (contract 4.1): no platformdirs, computed here."""

from __future__ import annotations

import os
import pathlib
import stat
import sys

import pytest

from marketlens_mcp import paths


def test_config_path_prefers_marketlens_config(tmp_path):
    target = tmp_path / "custom.yaml"
    env = {"MARKETLENS_CONFIG": str(target), "XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path)}
    assert paths.config_path(env) == target
    assert paths.config_path_is_explicit(env)


def test_config_path_uses_absolute_xdg_config_home(tmp_path):
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "home")}
    assert paths.config_path(env) == tmp_path / "xdg" / "marketlens" / "config.yaml"
    assert not paths.config_path_is_explicit(env)


def test_relative_xdg_config_home_is_ignored(tmp_path):
    env = {"XDG_CONFIG_HOME": "relative/dir", "HOME": str(tmp_path / "home")}
    assert paths.config_path(env) == tmp_path / "home" / ".config" / "marketlens" / "config.yaml"


def test_config_path_defaults_to_dot_config_on_every_platform(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "USERPROFILE": str(tmp_path / "home")}
    for platform in ("darwin", "linux", "win32"):
        assert (
            paths.config_path(env, platform=platform)
            == tmp_path / "home" / ".config" / "marketlens" / "config.yaml"
        )


def test_cache_dir_override(tmp_path):
    env = {"MARKETLENS_CACHE_DIR": str(tmp_path / "c"), "HOME": str(tmp_path)}
    assert paths.cache_dir(env) == tmp_path / "c"


@pytest.mark.parametrize(
    ("platform", "extra", "expected"),
    [
        ("darwin", {}, ("Library", "Caches", "marketlens")),
        ("linux", {}, (".cache", "marketlens")),
        ("win32", {}, ("AppData", "Local", "marketlens", "Cache")),
    ],
)
def test_cache_dir_platform_defaults(tmp_path, platform, extra, expected):
    env = {"HOME": str(tmp_path), "USERPROFILE": str(tmp_path), **extra}
    assert paths.cache_dir(env, platform=platform) == tmp_path.joinpath(*expected)


def test_cache_dir_xdg_cache_home_and_localappdata(tmp_path):
    assert paths.cache_dir(
        {"HOME": str(tmp_path), "XDG_CACHE_HOME": str(tmp_path / "xc")}, platform="linux"
    ) == (tmp_path / "xc" / "marketlens")
    assert paths.cache_dir(
        {"HOME": str(tmp_path), "LOCALAPPDATA": str(tmp_path / "la")}, platform="win32"
    ) == (tmp_path / "la" / "marketlens" / "Cache")


def test_display_path_abbreviates_home(tmp_path):
    home = tmp_path / "home"
    assert paths.display_path(home / ".config" / "marketlens" / "config.yaml", home=home) == (
        "~/.config/marketlens/config.yaml"
    )
    assert paths.display_path(tmp_path / "elsewhere.yaml", home=home) == str(tmp_path / "elsewhere.yaml")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_private_dirs_are_0700(tmp_path):
    d = paths.ensure_private_dir(tmp_path / "a" / "b")
    assert d.is_dir()
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_env_defaults_to_process_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("MARKETLENS_CACHE_DIR", str(tmp_path / "zz"))
    assert paths.cache_dir() == pathlib.Path(tmp_path / "zz")
