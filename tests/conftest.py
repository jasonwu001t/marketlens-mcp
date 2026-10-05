"""Shared test guards (owner: ml-core). Lane fixtures live in
tests/<lane>/conftest.py; nothing else goes here.

Every test runs with a throwaway config location and cache directory and with
outbound network refused: a test that needs the network is marked `network`
and skipped unless MARKETLENS_RUN_NETWORK=1.
"""

from __future__ import annotations

import os
import socket

import pytest


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    # No config file at the default location -> every setting takes its default.
    # A test that needs a config writes tmp_path / "xdg" / "marketlens" / "config.yaml"
    # (or sets MARKETLENS_CONFIG to a file it created).
    monkeypatch.delenv("MARKETLENS_CONFIG", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("MARKETLENS_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("FASTMCP_CHECK_FOR_UPDATES", "off")
    monkeypatch.setenv("FASTMCP_SHOW_SERVER_BANNER", "false")
    for name in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "MARKETLENS_HTTP_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if request.node.get_closest_marker("network") and os.environ.get("MARKETLENS_RUN_NETWORK") == "1":
        yield
        return
    if request.node.get_closest_marker("network"):
        pytest.skip("network test (set MARKETLENS_RUN_NETWORK=1 to run)")

    real_connect = socket.socket.connect

    def guarded(self, address):
        host = address[0] if isinstance(address, tuple) else address
        if host in ("127.0.0.1", "::1", "localhost") or isinstance(address, str):
            return real_connect(self, address)
        raise RuntimeError(f"test tried to reach the network: {address!r}")

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield
