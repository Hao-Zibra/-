"""Prices from Kiwi.com's public flight-search MCP endpoint. It needs no API key.

Kiwi is an online travel agency, so these are its own selling prices. Checked against
Expedia in November 2026 they ran 5% to 60% higher on the same China-US business-class
flights, and Kiwi returned nothing for United or Delta. Treat a Kiwi price as an upper
bound and verify on the airline's site before buying.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import date
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from farewatch.config import Watch
from farewatch.mcptool import describe, payload
from farewatch.model import Itinerary, Leg, Segment, SourceError

log = logging.getLogger(__name__)

SOURCE = "kiwi"
URL = "https://mcp.kiwi.com"
TOOL = "search-flight"
CABIN_CODES = {"economy": "M", "premium_economy": "W", "business": "C", "first": "F"}
# The tool rejects flyFrom / flyTo strings longer than this.
MAX_LOCATIONS_LENGTH = 100


def fetch(
    watch: Watch,
    currency: str,
    *,
    url: str = URL,
    attempts: int = 3,
    pause: float = 2.0,
    timeout: float = 180.0,
) -> list[Itinerary]:
    """Round trips Kiwi currently sells for the watch, priced in `currency`.

    One search returns at most 15 offers, cheapest first, across every origin, destination
    and date in the watch, so the cheapest offer is reliable but the list is not exhaustive.
    """
    argument_sets = build_arguments(watch, currency)
    for attempt in range(1, attempts + 1):
        try:
            payloads = asyncio.run(asyncio.wait_for(_search(argument_sets, url=url, pause=pause), timeout))
            return [itinerary for answer in payloads for itinerary in parse_payload(answer)]
        except Exception as exc:  # network, protocol and format errors all get the same retry
            if attempt == attempts:
                raise SourceError(f"Kiwi search failed after {attempts} attempts: {describe(exc)}") from exc
            log.warning("Kiwi search attempt %d failed (%s), retrying", attempt, describe(exc))
            time.sleep(5 * attempt)
    raise AssertionError("unreachable")


def build_arguments(watch: Watch, currency: str) -> list[dict[str, Any]]:
    """The tool arguments for every search needed to cover the watch.

    Airports are sent as one comma-separated list. A watch with more airports than fit in
    one request is split, and an open-jaw trip whose two ends land in different chunks is
    then not found.
    """
    base: dict[str, Any] = {
        "departureDate": _date(watch.depart.start),
        "departureDateTo": _date(watch.depart.end),
        "returnDate": _date(watch.ret.start),
        "returnDateTo": _date(watch.ret.end),
        "adults": watch.adults,
        "cabinClass": CABIN_CODES[watch.cabin],
        "currency": currency,
        "locale": "en",
        "sort": "price",
        # Self-transfer joins separate tickets, with no protection when the first flight is late.
        "allow_self_transfer": False,
    }
    if watch.nonstop:
        base["max_sector_stopovers"] = 0
    return [
        {**base, "flyFrom": origins, "flyTo": destinations}
        for origins in _chunks(watch.origins)
        for destinations in _chunks(watch.destinations)
    ]


def parse_payload(payload: dict[str, Any]) -> list[Itinerary]:
    """Round trips from one search-flight response. One-way offers are dropped."""
    if payload.get("error"):
        raise SourceError(f"Kiwi reported an error: {payload['error']}")
    raw_itineraries = payload.get("itineraries")
    currency = payload.get("currency")
    if not isinstance(raw_itineraries, list) or not isinstance(currency, str):
        raise SourceError(f"Kiwi response is missing 'itineraries' or 'currency' (keys: {sorted(payload)})")

    itineraries: list[Itinerary] = []
    unreadable = 0
    for raw in raw_itineraries:
        try:
            if not raw.get("inbound"):
                continue
            itineraries.append(
                Itinerary(
                    source=SOURCE,
                    price=float(raw["price"]),
                    currency=currency,
                    outbound=_leg(raw["outbound"]),
                    inbound=_leg(raw["inbound"]),
                    booking_url=str(raw.get("bookingUrl") or ""),
                )
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            unreadable += 1
            log.warning("skipping a Kiwi itinerary that could not be read: %r", exc)
    if unreadable and not itineraries:
        raise SourceError(
            f"none of Kiwi's {unreadable} itineraries could be read; its response format may have changed"
        )
    return itineraries


async def _search(argument_sets: Sequence[dict[str, Any]], *, url: str, pause: float) -> list[dict[str, Any]]:
    payloads = []
    async with streamable_http_client(url) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            for index, arguments in enumerate(argument_sets):
                if index:
                    await asyncio.sleep(pause)
                payloads.append(payload(await session.call_tool(TOOL, arguments), "Kiwi"))
    return payloads


def _leg(raw: dict[str, Any]) -> Leg:
    return Leg(tuple(_segment(segment) for segment in raw["segments"]))


def _segment(raw: dict[str, Any]) -> Segment:
    carrier = str(raw["carrier"])
    number = str(raw["flightNumber"])
    return Segment(
        carrier=carrier,
        flight_number=number if number.startswith(carrier) else carrier + number,
        origin=str(raw["from"]),
        destination=str(raw["to"]),
        departure=str(raw["departureTime"]),
        arrival=str(raw["arrivalTime"]),
        cabin=str(raw["cabinClass"]).strip().lower().replace(" ", "_"),
    )


def _chunks(codes: Sequence[str]) -> list[str]:
    chunks, current = [], ""
    for code in codes:
        candidate = f"{current},{code}" if current else code
        if len(candidate) > MAX_LOCATIONS_LENGTH:
            chunks.append(current)
            candidate = code
        current = candidate
    chunks.append(current)
    return chunks


def _date(day: date) -> str:
    return day.strftime("%d/%m/%Y")
