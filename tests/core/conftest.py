"""ml-core fixtures (helpers live in coresupport.py)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest


@pytest.fixture
def store(tmp_path) -> Iterator:
    from marketlens_mcp.testing import temp_store

    yield temp_store(tmp_path / "cache")
