"""Shared test guards (owner: ml-core). Lane fixtures live in
tests/<lane>/conftest.py; nothing else goes here.

Every test runs with a throwaway config location and cache directory and with
outbound network refused: a test that needs the network is marked `network`
and skipped unless MARKETLENS_RUN_NETWORK=1.
"""

from __future__ import annotations

import atexit
import contextlib
import importlib.util
import os
import shutil
import socket
import tempfile

import pytest

# The data extra (marketlens-data, import name omni) may be installed. Before
# anything imports it: no key from the developer's shell, and none from a .env
# an installed omni might read (an omni that predates OMNI_ENV_FILE loads its
# checkout's .env with override=False, and a present empty value counts as set);
# its test-run marker on; its store under the OS temp directory.
for _name in (
    "FRED_API_KEY",
    "BLS_API_KEY",
    "BEA_API_KEY",
    "EIA_API_KEY",
    "CENSUS_API_KEY",
    "SEC_CONTACT_EMAIL",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_API_SECRET",
    "APCA_API_KEY_ID",
    "APCA_API_SECRET_KEY",
):
    os.environ[_name] = ""
os.environ["OMNI_ENV_FILE"] = ""
os.environ["OMNI_TEST_RUN"] = "1"
_OMNI_DATA = tempfile.mkdtemp(prefix="marketlens-test-omni-")
os.environ["OMNI_DATA_DIR"] = _OMNI_DATA
atexit.register(shutil.rmtree, _OMNI_DATA, True)
if importlib.util.find_spec("omni") is not None:
    # Import it now, while every key is blank, rather than inside a test that
    # removed one (the shared fixture below deletes the Alpaca keys per test).
    with contextlib.suppress(Exception):  # a broken install is reported by the data tests and doctor
        import omni.config  # noqa: F401


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
