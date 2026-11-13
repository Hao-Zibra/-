"""Decides which offers satisfy a watch and which of those are under its price line.

Sources are asked for the right cabin, dates and stops, but their filters are not trusted:
every offer is checked again here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from farewatch.config import Watch
from farewatch.model import Itinerary


@dataclass(frozen=True)
class Evaluation:
    matching: tuple[Itinerary, ...]  # satisfy the watch, cheapest first
    hits: tuple[Itinerary, ...]  # matching and under the price line, cheapest first


def evaluate(itineraries: Iterable[Itinerary], watch: Watch) -> Evaluation:
    matching = tuple(sorted((it for it in itineraries if not violations(it, watch)), key=lambda it: it.price))
    return Evaluation(matching=matching, hits=tuple(it for it in matching if is_under_price_line(it, watch)))


def is_under_price_line(itinerary: Itinerary, watch: Watch) -> bool:
    """True when the price is strictly below the line set for the offer's own currency."""
    line = watch.alert_below.get(itinerary.currency)
    return line is not None and itinerary.price < line


def violations(itinerary: Itinerary, watch: Watch) -> list[str]:
    """Why the itinerary does not satisfy the watch. Empty when it does."""
    outbound, inbound = itinerary.outbound, itinerary.inbound
    problems = []

    if outbound.origin not in watch.origins:
        problems.append(f"leaves from {outbound.origin}")
    if outbound.destination not in watch.destinations:
        problems.append(f"arrives at {outbound.destination}")
    if inbound.origin not in watch.destinations:
        problems.append(f"returns from {inbound.origin}")
    if inbound.destination not in watch.origins:
        problems.append(f"returns to {inbound.destination}")
    elif not watch.open_jaw and inbound.destination != outbound.origin:
        problems.append(f"returns to {inbound.destination}, not {outbound.origin}")

    if outbound.departure_date not in watch.depart:
        problems.append(f"departs on {outbound.departure_date}")
    if inbound.departure_date not in watch.ret:
        problems.append(f"returns on {inbound.departure_date}")

    if watch.nonstop and (outbound.stops or inbound.stops):
        problems.append("has a connection")

    other_cabins = sorted(
        {segment.cabin for leg in itinerary.legs for segment in leg.segments if segment.cabin != watch.cabin}
    )
    if other_cabins:
        problems.append(f"has a segment in {', '.join(other_cabins)}")
    return problems
