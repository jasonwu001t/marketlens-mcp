"""Fixtures of the data provider tests; see data_harness. Skipped whole when
marketlens-data (import name omni) is not installed.

tests/conftest.py has already, before anything imported omni: blanked every
provider key (so no .env an older omni reads can fill one), set
OMNI_ENV_FILE="" and OMNI_TEST_RUN=1, and put OMNI_DATA_DIR under the OS temp
directory. Outbound sockets are refused for every test there too.
"""

from __future__ import annotations

import os
import tempfile

import pytest

assert os.environ.get("OMNI_ENV_FILE") == "" and os.environ.get("OMNI_TEST_RUN") == "1"
assert os.path.realpath(os.environ["OMNI_DATA_DIR"]).startswith(os.path.realpath(tempfile.gettempdir()))

pytest.importorskip("omni")

from data_harness import KEYS, freeze_clocks, install_upstream  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def pytest_collection_modifyitems(config, items):
    # marketlens-data does its own text I/O without naming an encoding; the
    # repository's EncodingWarning-as-error rule is for this repository's code,
    # which tests/core/test_data_encoding.py checks statically for the provider.
    for item in items:
        if str(item.path).startswith(HERE):
            item.add_marker(pytest.mark.filterwarnings("ignore::EncodingWarning"))


@pytest.fixture(autouse=True)
def omni_store(tmp_path, monkeypatch):
    """A fresh marketlens-data store per test, and a fresh runtime."""
    from omni.store import Store, set_store

    from marketlens_mcp.providers.data import runtime

    runtime.reset()
    # a tool may point OMNI_DATA_DIR elsewhere (providers.data.data_dir): put it back after
    monkeypatch.setenv("OMNI_DATA_DIR", os.environ["OMNI_DATA_DIR"])
    store = Store(root=tmp_path / "omni")
    set_store(store)
    yield store
    set_store(None)
    runtime.reset()


@pytest.fixture
def upstream(monkeypatch):
    up = install_upstream(monkeypatch)
    yield up
    assert up.unrouted == [], f"requests nothing answers: {up.unrouted}"


@pytest.fixture
def keys(monkeypatch):
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    return KEYS


@pytest.fixture
def frozen_clock(monkeypatch):
    freeze_clocks(monkeypatch)
