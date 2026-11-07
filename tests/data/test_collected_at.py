"""``Provenance.collected_at`` of a data tool: when what it read was last collected.

marketlens-data records a sync per slice (a dataset and its parameters), whether
or not the sync found anything new. A data tool's answer states the oldest of
the slices it refreshed, and of each other dataset it read, the dataset's
newest sync; unknown where nothing records one. ``knowledge_time`` cannot say
this: a quarterly series synced an hour ago and one whose collection stopped a
month ago can have the same newest value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from marketlens_mcp.providers.data.tools.common import Session

T = datetime(2026, 11, 3, 6, 0, tzinfo=UTC)


class _Store:
    def __init__(self, slices=None, datasets=None, *, fresh=True):
        self.slices, self.datasets, self.fresh = dict(slices or {}), dict(datasets or {}), fresh
        self.synced = []

    def synced_at(self, dataset_id, params):
        return self.slices.get((dataset_id, tuple(sorted(params.items()))))

    def last_sync(self, dataset_id):
        if dataset_id not in self.datasets:
            raise ValueError(f"not a dataset: {dataset_id}")
        return self.datasets[dataset_id]

    def is_fresh(self, dataset_id, params, ttl):
        return self.fresh


class _Session(Session):
    def __init__(self, store, mode="auto"):
        omni = SimpleNamespace(sync=lambda dataset_id, **params: store.synced.append(dataset_id))
        super().__init__(omni, SimpleNamespace(mode=mode, ttl_seconds=3600))
        self._store = store

    @property
    def store(self):
        return self._store


def _key(**params):
    return tuple(sorted(params.items()))


def test_the_oldest_refreshed_slice_is_the_answers():
    store = _Store(
        {
            ("fred.series", _key(series_id="DGS10")): T,
            ("fred.series", _key(series_id="GDPC1")): T - timedelta(days=40),
        }
    )
    s = _Session(store)
    s.refresh("fred.series", {"series_id": "DGS10"})
    s.refresh("fred.series", {"series_id": "GDPC1"})
    assert s.collected_at(["fred.series"]) == T - timedelta(days=40)


def test_a_dataset_read_without_a_refresh_states_its_newest_sync():
    store = _Store(
        {("sec.company_facts", _key(ticker="AAPL")): T}, {"sec.submissions": T - timedelta(hours=5)}
    )
    s = _Session(store)
    s.refresh("sec.company_facts", {"ticker": "AAPL"})
    assert s.collected_at(["sec.company_facts", "sec.submissions"]) == T - timedelta(hours=5)


def test_nothing_recorded_is_unknown_not_now():
    s = _Session(_Store())
    s.refresh("fred.series", {"series_id": "DGS10"})
    assert s.collected_at(["fred.series", "not a dataset"]) is None
    assert _Session(_Store()).collected_at([]) is None


def test_a_sync_this_call_made_is_stated_as_its_record():
    store = _Store(fresh=False)
    s = _Session(store)

    def sync(dataset_id, **params):
        store.slices[(dataset_id, _key(**params))] = T  # the sync's own record

    s.omni = SimpleNamespace(sync=sync)
    assert s.refresh("fred.series", {"series_id": "DGS10"}) is False
    assert s.collected_at(["fred.series"]) == T


def test_a_local_read_states_the_record_it_did_not_refresh():
    store = _Store({("fred.series", _key(series_id="DGS10")): T - timedelta(days=2)}, fresh=False)
    s = _Session(store, mode="local")
    assert s.refresh("fred.series", {"series_id": "DGS10"}) is True
    assert store.synced == []
    assert s.collected_at(["fred.series"]) == T - timedelta(days=2)


def test_a_store_that_keeps_no_slice_record_is_unknown():
    class Older(_Store):
        synced_at = None  # an older marketlens-data: no Store.synced_at

    s = _Session(Older())
    s.refresh("fred.series", {"series_id": "DGS10"})
    assert s.collected_at(["fred.series"]) is None
