"""A tiny example plugin used by the tests (and mirrored in WRITING_A_PLUGIN.md).

It declares one capability (off by default), one model and one tool. The
broken variants below exercise the plugin isolation rules.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from marketlens_mcp.plugin_api import (
    PLUGIN_API_VERSION,
    CapabilitySpec,
    PluginContext,
    PluginInfo,
    Registry,
    ToolContext,
    ToolError,
    ToolOutput,
    ToolSpec,
)
from marketlens_schema import AbsenceCode, CanonicalModel, Delay, Provenance, UtcDatetime, unit

GOLDEN = "tests/core/test_registry.py"


class Thing(CanonicalModel):
    """One thing, observed at a time."""

    schema_name: ClassVar[str] = "example.Thing"
    time_column: ClassVar[str | None] = "t"
    group_column: ClassVar[str | None] = "name"
    value_columns: ClassVar[tuple[str, ...]] = ("size",)

    name: str
    t: UtcDatetime = unit("UTC", description="When it was observed")
    size: float | None = unit("count", default=None, description="How big it was")


class ThingsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(3, ge=1, le=1000, description="How many things to return")
    fail: bool = Field(False, description="Raise a readable error instead")


async def things(ctx: ToolContext, args: ThingsArgs) -> ToolOutput:
    if args.fail:
        raise ToolError(
            "example_failed", "The example tool was asked to fail.", hint="Call it with fail=false."
        )
    start = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    rows = [
        Thing(
            name=f"thing{i % 3}",
            t=start + dt.timedelta(minutes=i),
            size=float(i) if i % 5 else None,
            absent=None if i % 5 else {"size": AbsenceCode.NO_DATA},
        )
        for i in range(args.count)
    ]
    prov = Provenance(
        provider="example",
        route="local:example_things",
        fetched_at=ctx.now(),
        delay=Delay.REALTIME,
        request={"count": args.count},
    )
    return ToolOutput(model=Thing, provenance=prov, rows=rows)


THINGS = ToolSpec(
    name="example_things",
    capability="example",
    title="Example things",
    description="Returns synthetic things (name, time, size). Large results are stored; you get a result_id.",
    readme="Synthetic rows for tests",
    input_model=ThingsArgs,
    output_model=Thing,
    provider="local",
    route="local:example_things",
    handler=things,
    golden_test=GOLDEN,
    env=("EXAMPLE_TOKEN",),
)

EXAMPLE_CAPABILITY = CapabilitySpec("example", "Example", "Synthetic things for tests.", False)


def register(registry: Registry, ctx: PluginContext) -> None:
    registry.add_capability(EXAMPLE_CAPABILITY)
    registry.add_model(Thing)
    registry.add_tool(THINGS)


PLUGIN = PluginInfo(
    name="example",
    api_version=PLUGIN_API_VERSION,
    register=register,
    description="Synthetic things.",
    env=("EXAMPLE_HOME",),
)


# --- broken variants ---------------------------------------------------------------------


def _raises(registry: Registry, ctx: PluginContext) -> None:
    registry.add_capability(CapabilitySpec("raiser", "Raiser", "Declared before the failure.", True))
    raise RuntimeError("boom while registering")


RAISES = PluginInfo(name="raiser", api_version=PLUGIN_API_VERSION, register=_raises)


def _collides(registry: Registry, ctx: PluginContext) -> None:
    import dataclasses

    registry.add_capability(CapabilitySpec("collider", "Collider", "Collides.", True))
    registry.add_tool(dataclasses.replace(THINGS, name="results_query", capability="collider"))


COLLIDES = PluginInfo(name="collider", api_version=PLUGIN_API_VERSION, register=_collides)

FUTURE = PluginInfo(name="future", api_version=(2, 0), register=register)
NEWER_MINOR = PluginInfo(name="newerminor", api_version=(1, PLUGIN_API_VERSION[1] + 1), register=register)


class Unnamespaced(CanonicalModel):
    schema_name: ClassVar[str] = "marketlens.Sneaky"

    x: int


def _bad_model(registry: Registry, ctx: PluginContext) -> None:
    registry.add_model(Unnamespaced)


BAD_MODEL = PluginInfo(name="badmodel", api_version=PLUGIN_API_VERSION, register=_bad_model)


def _redeclares(registry: Registry, ctx: PluginContext) -> None:
    registry.add_capability(CapabilitySpec("market", "Market", "Taken.", False))


REDECLARES = PluginInfo(name="redeclares", api_version=PLUGIN_API_VERSION, register=_redeclares)


class OpenArgs(BaseModel):
    x: int = 1


def _open_args(registry: Registry, ctx: PluginContext) -> None:
    import dataclasses

    registry.add_capability(CapabilitySpec("openargs", "Open", "Open args.", True))
    registry.add_model(type("T2", (Thing,), {"schema_name": "openargs.Thing"}))
    registry.add_tool(
        dataclasses.replace(
            THINGS,
            name="openargs_things",
            capability="openargs",
            input_model=OpenArgs,
            output_model="openargs.Thing",
        )
    )


OPEN_ARGS = PluginInfo(name="openargs", api_version=PLUGIN_API_VERSION, register=_open_args)


def _on_portfolio(registry: Registry, ctx: PluginContext) -> None:
    import dataclasses

    registry.add_model(type("T3", (Thing,), {"schema_name": "holdingsx.Thing"}))
    registry.add_tool(
        dataclasses.replace(
            THINGS, name="holdingsx_things", capability="portfolio", output_model="holdingsx.Thing"
        )
    )


ON_PORTFOLIO = PluginInfo(name="holdingsx", api_version=PLUGIN_API_VERSION, register=_on_portfolio)
