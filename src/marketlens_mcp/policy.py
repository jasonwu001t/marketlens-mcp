"""Which capabilities are on (contract 4.3).

For each declared capability: locked -> on; else the config's value if it
sets one; else the declaration's ``default_enabled``. A plugin's capability is
declared only when the owner named the plugin in ``plugins.enabled`` (that is
the owner's agreement to the plugin's defaults). A tool is registered iff its
capability is on. There is no write capability to turn on: write tools do not
exist in marketlens-mcp, so no configuration can enable one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from .config import Config, ConfigError
from .plugin_api import CapabilitySpec

LIVE_WARNING = "portfolio.environment is live: portfolio tools read your LIVE brokerage account."

Source = Literal["default", "config", "plugin default"]


@dataclass(frozen=True)
class CapabilityState:
    spec: CapabilitySpec
    enabled: bool
    source: Source


@dataclass(frozen=True)
class Policy:
    states: tuple[CapabilityState, ...]

    @property
    def enabled(self) -> frozenset[str]:
        return frozenset(s.spec.id for s in self.states if s.enabled)

    def state(self, capability: str) -> CapabilityState:
        for s in self.states:
            if s.spec.id == capability:
                return s
        raise KeyError(capability)

    def is_enabled(self, capability: str) -> bool:
        return any(s.spec.id == capability and s.enabled for s in self.states)


def resolve(config: Config, capabilities: Sequence[CapabilitySpec]) -> Policy:
    """Resolve every declared capability; refuse (R3) a configured id that no
    loaded declaration knows."""
    known = [c.id for c in capabilities]
    for cap in config.capabilities:
        if cap not in known:
            raise ConfigError(
                f"marketlens config {config.display_path}: unknown capability '{cap}'. Known: {', '.join(known)}. "
                "A plugin's capability is known only when the plugin is installed and listed in plugins.enabled."
            )
    states = []
    for spec in capabilities:
        if spec.locked:
            states.append(CapabilityState(spec, True, "default"))
        elif spec.id in config.capabilities:
            states.append(CapabilityState(spec, bool(config.capabilities[spec.id]), "config"))
        else:
            source: Source = "default" if spec.declared_by == "builtin" else "plugin default"
            states.append(CapabilityState(spec, spec.default_enabled, source))
    return Policy(tuple(states))
