"""Fixtures of the analytics tests (owner: ml-analytics); see analytics_harness."""

import pytest
from analytics_harness import FakeContext, FakeStore, _no_network  # noqa: F401


@pytest.fixture
def store() -> FakeStore:
    return FakeStore()


@pytest.fixture
def ctx(store: FakeStore) -> FakeContext:
    return FakeContext(results=store)
