"""Builders for the objects the tests keep needing."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any

from farewatch.config import DateRange, Watch
from farewatch.model import Itinerary, Leg, Segment


def make_watch(**overrides: Any) -> Watch:
    values: dict[str, Any] = dict(
        name="china-lax",
        origins=("HKG", "PVG", "TFU"),
        destinations=("LAX",),
        depart=DateRange(date(2027, 3, 10), date(2027, 3, 16)),
        ret=DateRange(date(2027, 4, 7), date(2027, 4, 13)),
        alert_below={"USD": 4000.0, "CNY": 28000.0},
    )
    values.update(overrides)
    return Watch(**values)


def seg(flight: str, origin: str, destination: str, departs: str, cabin: str = "business") -> Segment:
    return Segment(
        carrier=flight[:2],
        flight_number=flight,
        origin=origin,
        destination=destination,
        departure=departs,
        arrival=departs,
        cabin=cabin,
    )


def trip(
    price: float,
    *,
    currency: str = "USD",
    out: Sequence[Segment] | None = None,
    back: Sequence[Segment] | None = None,
) -> Itinerary:
    """A nonstop business round trip PVG-LAX inside make_watch()'s windows, unless legs are given."""
    out = out or [seg("MU583", "PVG", "LAX", "2027-03-12T13:10:00")]
    back = back or [seg("MU586", "LAX", "PVG", "2027-04-09T11:30:00")]
    return Itinerary(
        source="kiwi",
        price=price,
        currency=currency,
        outbound=Leg(tuple(out)),
        inbound=Leg(tuple(back)),
        booking_url="https://example.test/book",
    )
