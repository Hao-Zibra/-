"""Calling a tool on a public MCP server, which is how every price source here is reached."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from farewatch.model import SourceError


async def call_many(
    url: str,
    tool: str,
    argument_sets: Sequence[dict[str, Any]],
    *,
    who: str,
    concurrency: int = 1,
    attempts: int = 2,
    timeout: float = 90.0,
) -> list[dict[str, Any] | SourceError]:
    """Calls `tool` once per argument set over one session, at most `concurrency` at a time.

    Returns one entry per argument set, in the same order: the tool's answer, or the
    SourceError that call ended in. Raises SourceError when the server cannot be reached.
    """
    results: list[dict[str, Any] | SourceError] = [SourceError(f"{who} was not asked")] * len(argument_sets)
    gate = asyncio.Semaphore(concurrency)

    async def call(index: int, arguments: dict[str, Any], session: ClientSession) -> None:
        async with gate:
            for attempt in range(1, attempts + 1):
                try:
                    result = await asyncio.wait_for(session.call_tool(tool, arguments), timeout)
                    results[index] = payload(result, who)
                    return
                except SourceError as exc:  # the server answered; asking again would not help
                    results[index] = exc
                    return
                except Exception as exc:
                    results[index] = SourceError(f"{who} search failed: {describe(exc)}")
                    if attempt < attempts:
                        await asyncio.sleep(2 * attempt)

    try:
        async with streamable_http_client(url) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                await asyncio.gather(*(call(i, arguments, session) for i, arguments in enumerate(argument_sets)))
    except Exception as exc:
        raise SourceError(f"{who} could not be reached: {describe(exc)}") from exc
    return results


def payload(result: Any, who: str) -> dict[str, Any]:
    """The JSON object a tool call answered with."""
    text = "".join(getattr(block, "text", "") or "" for block in result.content)
    if result.is_error:
        raise SourceError(f"{who} rejected the search: {text[:300]}")
    if isinstance(result.structured_content, dict):
        return result.structured_content
    try:
        data = json.loads(text)
    except ValueError:
        raise SourceError(f"{who} response is not JSON: {text[:200]!r}") from None
    if not isinstance(data, dict):
        raise SourceError(f"{who} response is not a JSON object: {text[:200]!r}")
    return data


def describe(exc: BaseException) -> str:
    """The first concrete error inside (possibly nested) exception groups."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return f"{type(exc).__name__}: {exc}"
