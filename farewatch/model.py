"""Itineraries as every price source reports them, whatever the source's own format."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


class SourceError(RuntimeError):
    """A price source could not be reached, or answered in a shape we cannot read."""


@dataclass(frozen=True)
class Segment:
    """One flight: a single takeoff and landing."""

    carrier: str  # IATA airline code, e.g. "MU"
    flight_number: str  # with the carrier prefix, e.g. "MU583"
    origin: str
    destination: str
    departure: str  # local ISO timestamp, e.g. "2027-01-05T13:10:00"
    arrival: str
    cabin: str  # lower case with underscores, e.g. "business", "premium_economy"


@dataclass(frozen=True)
class Leg:
    """One direction of travel: one segment when nonstop, more when connecting."""

    segments: tuple[Segment, ...]

    def __post_init__(self) -> None:
        if not self.segments:
            raise ValueError("a leg needs at least one segment")

    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination

    @property
    def departure(self) -> str:
        return self.segments[0].departure

    @property
    def arrival(self) -> str:
        return self.segments[-1].arrival

    @property
    def departure_date(self) -> date:
        return date.fromisoformat(self.departure[:10])

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def flights(self) -> str:
        return "+".join(segment.flight_number for segment in self.segments)


@dataclass(frozen=True)
class Itinerary:
    """A priced round trip offered by one source."""

    source: str
    price: float  # total for all passengers, both directions
    currency: str
    outbound: Leg
    inbound: Leg
    booking_url: str

    @property
    def legs(self) -> tuple[Leg, Leg]:
        return (self.outbound, self.inbound)

    @property
    def carriers(self) -> tuple[str, ...]:
        return tuple(sorted({segment.carrier for leg in self.legs for segment in leg.segments}))

    @property
    def key(self) -> str:
        """Which flights on which days, independent of price and seller."""
        return " / ".join(
            "+".join(f"{segment.flight_number}@{segment.departure[:10]}" for segment in leg.segments)
            for leg in self.legs
        )
