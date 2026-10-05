"""Every marketlens-data dataset is mapped to a tool or excluded with a
reason, and every mapped tool states the point-in-time rule of what it reads."""

from __future__ import annotations

import omni

from marketlens_mcp.providers.data import parity
from marketlens_mcp.providers.data.tools import all_specs, common, datasets_by_tool


def test_every_dataset_is_mapped_or_excluded():
    assert set(omni.list_datasets()) == set(parity.PARITY)
    mapped = [d for d, e in parity.PARITY.items() if isinstance(e, parity.Mapped)]
    assert len(mapped) == 27


def test_each_tool_describes_the_point_in_time_rule_of_its_datasets():
    from omni.sources.base import authority_of, resolve

    specs = {s.name: s for s in all_specs()}
    for tool, datasets in datasets_by_tool().items():
        for dataset_id in datasets:
            policy = resolve(dataset_id)[1].pit
            assert common.PIT[policy] in specs[tool].description, (tool, dataset_id, policy)
            if dataset_id != "fred.series":
                assert authority_of(dataset_id) in ("official", "exchange", "third-party"), dataset_id
