"""One tool call, as the server runs it: validate the arguments against the
spec's input model, run the handler, then offload (inline or stored).
``marketlens_mcp.testing.call_tool`` runs exactly this code without MCP.

Every failure leaves as a readable ToolError whose texts are scrubbed of
filesystem paths; an unexpected exception becomes ``internal_error`` and its
traceback goes to the server log (stderr), never to the model.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .offload import finalize
from .plugin_api import ToolContext, ToolError, ToolOutput, ToolSpec
from .results.evict import StoreLockTimeout
from .results.guard import scrub
from .results_api import OutputRisk, QueryRefused, ResultMarker, ResultNotFound, ToolResponse

log = logging.getLogger("marketlens")

INTERNAL_ERROR = "The tool failed unexpectedly; see the server log."


@dataclass(frozen=True)
class Executed:
    response: ToolResponse
    risk: OutputRisk


def _known_paths(ctx: ToolContext) -> list[str]:
    paths: list[str] = []
    try:
        root = getattr(ctx.results, "root", None)
    except Exception:
        root = None
    if root is not None:
        paths.append(str(root))
        with contextlib.suppress(OSError):
            paths.append(str(root.resolve()))
    return paths


def _ttl(ctx: ToolContext) -> float:
    try:
        limits = getattr(ctx.results, "limits", None)
    except Exception:
        limits = None
    return getattr(limits, "ttl_hours", 24)


def scrubbed(err: ToolError, known_paths: list[str]) -> ToolError:
    return ToolError(
        err.code,
        scrub(err.message, *known_paths),
        hint=scrub(err.hint, *known_paths) if err.hint else None,
        retryable=err.retryable,
    )


def validation_message(tool: str, exc: ValidationError) -> str:
    parts = []
    for e in exc.errors(include_url=False, include_input=False)[:3]:
        loc = ".".join(str(p) for p in e.get("loc", ())) or "arguments"
        parts.append(f"{loc}: {e.get('msg', 'invalid')}")
    more = exc.error_count() - len(parts)
    suffix = f" (and {more} more)" if more > 0 else ""
    return f"Invalid arguments for {tool}: " + "; ".join(parts) + suffix + "."


def to_tool_error(exc: BaseException, ctx: ToolContext, tool: str) -> ToolError:
    known = _known_paths(ctx)
    if isinstance(exc, ToolError):
        return scrubbed(exc, known)
    if isinstance(exc, ResultNotFound):
        return ToolError(
            f"result_{exc.reason}",
            f"Result {exc.result_id} is {exc.reason}. Results live {_ttl(ctx):g} hours, only in the session "
            "that created them; fetch the data again.",
        )
    if isinstance(exc, QueryRefused):
        return ToolError(exc.code, scrub(exc.message, *known))
    if isinstance(exc, StoreLockTimeout):
        return ToolError(
            "store_busy",
            "The result store is locked by another marketlens process.",
            hint="Try again in a few seconds.",
            retryable=True,
        )
    log.error("tool %s failed", tool, exc_info=exc)
    return ToolError("internal_error", INTERNAL_ERROR)


async def execute(spec: ToolSpec, ctx: ToolContext, arguments: Mapping[str, Any] | None) -> Executed:
    """validate -> handler -> offload. Raises ToolError (already scrubbed)."""
    try:
        try:
            args = spec.input_model.model_validate(dict(arguments or {}))
        except ValidationError as exc:
            raise ToolError("invalid_arguments", validation_message(spec.name, exc)) from None
        out = await spec.handler(ctx, args)
        if not isinstance(out, ToolOutput):
            raise TypeError(f"handler of {spec.name} returned {type(out).__name__}, not ToolOutput")
        response = finalize(
            out,
            tool=spec.name,
            default_risk=spec.output_risk,
            store=ctx.results,
            limits=ctx.limits,
            models=getattr(ctx.results, "models", None),
        )
    except Exception as exc:
        raise to_tool_error(exc, ctx, spec.name) from None
    risk: OutputRisk = response.risk if isinstance(response, ResultMarker) else (out.risk or spec.output_risk)
    return Executed(response=response, risk=risk)
