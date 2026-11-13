"""Prices from Skiplagged's public flight-search MCP endpoint. It needs no API key.

Unlike Kiwi it covers the US carriers, and in November 2026 its round-trip prices were
usually within a dollar of Expedia's. Three things shape how it is used:

- A search covers one departure date (and one return date), so a date window takes one
  search per day.
- A round-trip result names the outbound flight and the cheapest round-trip total that
  starts with it, but not the return flight that total is for. The return flight printed
  on the card is only the cheapest one-way flight that day.
- Its answers fluctuate. Within a few hours the same dates were quoted at one fare, then
  65% higher, then at the first fare again, while Expedia's price did not move. A day's
  one-way list can also be missing an airline that flies that day.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from farewatch import mcptool
from farewatch.config import Watch
from farewatch.model import Leg, Segment, SourceError

log = logging.getLogger(__name__)

SOURCE = "skiplagged"
URL = "https://mcp.skiplagged.com/mcp"
TOOL = "sk_flights_search"
FARE_CLASSES = {"economy": "economy", "premium_economy": "premium", "business": "business", "first": "first"}
# A search takes one origin plus two "additional" ones, and the same for destinations.
AIRPORTS_PER_SEARCH = 3
# What a connecting flight's card leaves out: where and when it connects.
UNKNOWN = ""


@dataclass(frozen=True)
class Offer:
    """One flight card: a direction of travel and a price.

    From a one-way search the price buys `leg` alone. From a round-trip search `leg` is the
    outbound and the price is the cheapest round trip that starts with it.
    """

    leg: Leg
    price: float
    currency: str
    url: str


def search_many(
    queries: Sequence[dict[str, Any]], *, url: str = URL, concurrency: int = 6, cool_off: float = 30.0
) -> list[dict[str, Any] | SourceError]:
    """Runs the searches, several at a time. One entry per query: its answer or its error.

    Skiplagged limits how many searches a client may make: in November 2026 it began
    refusing (HTTP 429) after roughly 150 searches in half an hour. Searches refused that
    way are tried once more, slowly, after `cool_off` seconds. When every search is
    refused the limit is not about to lift, so nothing is retried.
    """
    if not queries:
        return []

    def run(batch: Sequence[dict[str, Any]], at_once: int) -> list[dict[str, Any] | SourceError]:
        return asyncio.run(mcptool.call_many(url, TOOL, batch, who="Skiplagged", concurrency=at_once))

    answers = run(queries, concurrency)
    refused = [index for index, answer in enumerate(answers) if is_rate_limited(answer)]
    if refused and len(refused) < len(queries):
        log.warning("Skiplagged refused %d of %d searches (rate limit); retrying them in %.0fs",
                    len(refused), len(queries), cool_off)
        time.sleep(cool_off)
        for index, answer in zip(refused, run([queries[index] for index in refused], 2), strict=True):
            answers[index] = answer
    return answers


def is_rate_limited(answer: Any) -> bool:
    """Whether a search was refused because too many were made recently."""
    return isinstance(answer, SourceError) and "429" in str(answer)


def one_way_query(
    origins: Sequence[str], destinations: Sequence[str], day: date, watch: Watch, currency: str
) -> dict[str, Any]:
    """One-way flights on `day` from any of `origins` to any of `destinations` (up to 3 each)."""
    query = _common(watch, currency)
    query.update(origin=origins[0], destination=destinations[0], departureDate=day.isoformat())
    if origins[1:]:
        query["additionalOrigins"] = list(origins[1:])
    if destinations[1:]:
        query["additionalDestinations"] = list(destinations[1:])
    return query


def round_trip_query(
    origin: str, destination: str, out_day: date, back_day: date, watch: Watch, currency: str
) -> dict[str, Any]:
    """Round trips between one airport pair on one pair of dates."""
    query = _common(watch, currency)
    query.update(
        origin=origin,
        destination=destination,
        departureDate=out_day.isoformat(),
        returnDate=back_day.isoformat(),
    )
    return query


def parse_offers(payload: dict[str, Any], cabin: str) -> list[Offer]:
    """The flight cards of one search answer. `cabin` is the cabin that was searched for."""
    cards = payload.get("flights")
    if not isinstance(cards, list):
        raise SourceError(f"Skiplagged response has no 'flights' list (keys: {sorted(payload)})")
    offers: list[Offer] = []
    unreadable = 0
    for card in cards:
        try:
            offers.append(
                Offer(
                    leg=_leg(card, cabin),
                    price=float(card["price"]["amount"]),
                    currency=str(card["price"]["currency"]),
                    url=str(card.get("deepLink") or payload.get("searchUrl") or ""),
                )
            )
        except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
            unreadable += 1
            log.warning("skipping a Skiplagged flight card that could not be read: %r", exc)
    if unreadable and not offers:
        raise SourceError(
            f"none of Skiplagged's {unreadable} flight cards could be read; its response format may have changed"
        )
    return offers


def _common(watch: Watch, currency: str) -> dict[str, Any]:
    query: dict[str, Any] = {
        "fareClass": FARE_CLASSES[watch.cabin],
        "adults": watch.adults,
        "currency": currency,
        "sort": "price",
        "limit": 100,
        # Neither is a ticket to the place asked for: hidden-city means leaving at a layover,
        # virtual interlining joins separate tickets with no protection between them.
        "includeHiddenCity": False,
        "includeVirtualInterlining": False,
    }
    if watch.nonstop:
        query["maxStops"] = "none"
    return query


def _leg(card: dict[str, Any], cabin: str) -> Leg:
    # The id ends in "trip=<outbound>" or "trip=<outbound>,<return>"; a connecting direction
    # joins its flight numbers with "-", e.g. "trip=PR319-PR102".
    flights = card["id"].rsplit("trip=", 1)[1].split(",")[0].split("-")
    if len(flights) != card["layovers"] + 1 or not all(flights):
        raise ValueError(f"flight numbers {flights} do not fit {card['layovers']} layovers")
    last = len(flights) - 1
    return Leg(
        tuple(
            Segment(
                carrier=number[:2],
                flight_number=number,
                origin=str(card["departure"]["airport"]) if index == 0 else UNKNOWN,
                destination=str(card["arrival"]["airport"]) if index == last else UNKNOWN,
                departure=_local(card["departure"]["dateTime"]) if index == 0 else UNKNOWN,
                arrival=_local(card["arrival"]["dateTime"]) if index == last else UNKNOWN,
                cabin=cabin,  # cards do not state a cabin; the search was limited to this one
            )
            for index, number in enumerate(flights)
        )
    )


def _local(timestamp: str) -> str:
    """'2027-03-12T22:45:00+08:00' -> '2027-03-12T22:45:00': local time, as the other sources give it."""
    if len(timestamp) < 19:
        raise ValueError(f"not a timestamp: {timestamp!r}")
    return timestamp[:19]
