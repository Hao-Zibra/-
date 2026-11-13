"""A one-off search: the cheapest ways to fly a trip right now, across every source.

`scan` asks a little and often. A hunt asks thoroughly once: it covers every day of both
date windows on Skiplagged, which has the airlines Kiwi lacks, and adds Kiwi's own offers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from typing import Any

from farewatch import alerts, kiwi, rules, skiplagged
from farewatch.config import ConfigError, DateRange, Watch
from farewatch.model import Itinerary, Leg, Segment, SourceError
from farewatch.skiplagged import Offer

Search = Callable[[Sequence[dict[str, Any]]], list[dict[str, Any] | SourceError]]
Fetch = Callable[[Watch, str], list[Itinerary]]
Priced = tuple[Itinerary, tuple[str, ...]]  # an itinerary and the links to book it
Airports = tuple[str, str]  # where the trip starts and where it goes
Dates = tuple[str, str, date, date]  # origin, destination, day out, day back
# For each round-trip search made: outbound flight number -> cheapest round trip starting with it.
Quotes = dict[Dates, dict[str, float]]

# Sources that answer for a whole date window in one go.
RANGE_SOURCES: Mapping[str, Fetch] = {kiwi.SOURCE: kiwi.fetch}

SWEPT_AIRPORT_PAIRS = 2  # airport pairs searched for every day out and every day back
PROBES_PER_OTHER_PAIR = 3  # round-trip searches for each of the remaining airport pairs
VERIFIED = 6  # date pairs predicted to be cheapest that get a search to confirm it
MAX_SEARCHES = 120  # the endpoint is free, shared and rate-limited, so a bigger hunt is refused
PAIRED_PER_AIRPORT = 6  # cheapest one-way offers between each two airports that get paired into trips
FAILURES_SHOWN = 5

WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class Find:
    """One way to fly the trip: particular flights, on their cheapest dates."""

    itinerary: Itinerary
    links: tuple[str, ...]  # where to book; two links for two one-way tickets
    # Other (day out, day back) combinations searched on which the same flights cost the same.
    same_price: tuple[tuple[date, date], ...] = ()
    # For a fare from a swept airport pair: the days out and the days back on which it is
    # at its lowest. Each day was searched against one day of the other direction.
    cheap_days: tuple[tuple[date, ...], tuple[date, ...]] | None = None


@dataclass(frozen=True)
class Hunt:
    finds: tuple[Find, ...]  # cheapest first
    searches: int
    failures: tuple[str, ...]  # searches that got no answer, so finds may be missing


def hunt(
    watch: Watch,
    currency: str = "USD",
    *,
    search: Search = skiplagged.search_many,
    range_sources: Mapping[str, Fetch] = RANGE_SOURCES,
) -> Hunt:
    """Searches every source for the trip `watch` describes; its price lines play no part.

    A Skiplagged search covers a single day, so that source is worked in steps:

    1. One-way searches for every day of both windows. They show who flies when, and the
       cheapest of them are paired into trips on two one-way tickets.
    2. Round-trip searches, since a round-trip fare is usually far below two one-ways. The
       airport pairs with the cheapest one-ways are swept (see _Sweep); the others get a
       few searches on their airlines' cheapest one-way days.
    3. A search of each date pair the sweeps predict to be cheapest, to confirm it.
    """
    out_days, back_days = _days(watch.depart), _days(watch.ret)
    origin_groups, destination_groups = _groups(watch.origins), _groups(watch.destinations)
    out_queries = [
        skiplagged.one_way_query(origins, destinations, day, watch, currency)
        for day in out_days
        for origins in origin_groups
        for destinations in destination_groups
    ]
    back_queries = [
        skiplagged.one_way_query(destinations, origins, day, watch, currency)
        for day in back_days
        for destinations in destination_groups
        for origins in origin_groups
    ]
    airport_pairs = len(watch.origins) * len(watch.destinations)
    planned = (
        len(out_queries)
        + len(back_queries)
        + min(SWEPT_AIRPORT_PAIRS, airport_pairs) * (len(out_days) + len(back_days) - 1)
        + max(0, airport_pairs - SWEPT_AIRPORT_PAIRS) * PROBES_PER_OTHER_PAIR
        + VERIFIED
    )
    if planned > MAX_SEARCHES:
        raise ConfigError(
            f"this hunt could take {planned} searches and the limit is {MAX_SEARCHES}: "
            "name fewer airports or narrow the date windows"
        )

    failures: list[str] = []
    candidates: list[Priced] = []
    sweeps: list[_Sweep] = []
    quotes: Quotes = {}
    searches = 0
    try:
        answers = search(out_queries + back_queries)
        searches += len(answers)
        outs = _offers(out_queries, answers[: len(out_queries)], watch, outbound=True, failures=failures)
        backs = _offers(back_queries, answers[len(out_queries) :], watch, outbound=False, failures=failures)
        candidates += _pairs(outs, backs)

        prober = _Prober(backs, watch, currency, search, failures, quotes)
        try:
            sweeps, probes = _round_trip_plan(outs, backs, watch)
            prober.search([dates for sweep in sweeps for dates in sweep.searches()] + probes)
            prober.search(_worth_verifying(sweeps, quotes, watch))
        finally:
            searches += prober.searches
            candidates += prober.fares
    except SourceError as exc:  # the endpoint could not be reached at all
        failures.append(f"{skiplagged.SOURCE}: {exc}")

    for name, fetch in range_sources.items():
        try:
            candidates += [(itinerary, (itinerary.booking_url,)) for itinerary in fetch(watch, currency)]
            searches += 1
        except SourceError as exc:
            failures.append(f"{name}: {exc}")

    finds = tuple(_with_cheap_days(find, sweeps, quotes) for find in _rank(candidates, watch))
    return Hunt(finds=finds, searches=searches, failures=tuple(failures))


def render(result: Hunt, watch: Watch, *, top: int = 10) -> str:
    """The hunt as Markdown: the cheapest finds, the best from each origin, and what failed."""
    lines = [
        f"{'直飞' if watch.nonstop else '可转机'}{alerts.CABIN_NAMES[watch.cabin]}往返 "
        f"{'/'.join(watch.origins)} → {'/'.join(watch.destinations)}，"
        f"去 {watch.depart.start} 至 {watch.depart.end}，回 {watch.ret.start} 至 {watch.ret.end}，{watch.adults} 人",
        "",
    ]
    if not result.finds:
        lines.append(f"查了 {result.searches} 次，没有找到符合条件的票。")
    else:
        best = result.finds[0]
        shown = result.finds[:top]
        lines += [
            f"查了 {result.searches} 次，找到 {len(result.finds)} 种走法，"
            f"最低 {alerts.money(best.itinerary.price, best.itinerary.currency)}。",
            "",
            "| 价格 | 票型 | 去程 | 回程 | 来源 |",
            "|---|---|---|---|---|",
            *(_row(find) for find in shown),
            "",
            "各出发地最低价：",
            "",
            "| 出发地 | 价格 | 票型 | 去程 | 回程 | 来源 |",
            "|---|---|---|---|---|---|",
        ]
        per_origin = [
            next((find for find in result.finds if find.itinerary.outbound.origin == origin), None)
            for origin in watch.origins
        ]
        lines += [
            f"| {origin} {_row(find)}" if find else f"| {origin} | 没有查到 | | | | |"
            for origin, find in zip(watch.origins, per_origin)
        ]
        if best.cheap_days:
            days_out, days_back = (
                "、".join(f"{day:%m-%d}" for day in days) for days in best.cheap_days
            )
            lines += [
                "",
                f"最低价这组航班的低价日：去程 {days_out}；回程 {days_back}。"
                "两个方向各自逐日实查过，任意搭配按规律应同价。",
            ]
        if best.same_price:
            lines += [
                "",
                "实查同价的其他日期组合（去 / 回）："
                + "，".join(f"{out:%m-%d} / {back:%m-%d}" for out, back in best.same_price),
            ]
        lines += ["", "最低价下单：" + " 和 ".join(link for link in best.links if link)]
        if any(find and find.itinerary.inbound_assumed for find in [*shown, *per_origin]):
            lines += [
                "",
                "※ 回程航班待确认：这个来源的往返价只标明去程航班，表里列的是同一航司当天最便宜的回程航班；"
                "带问号的表示当天没查到该航司的回程航班号。",
            ]
    if result.failures:
        lines += ["", f"有 {len(result.failures)} 次搜索没有查到结果，上面的结果可能不全："]
        lines += [f"- {failure}" for failure in result.failures[:FAILURES_SHOWN]]
        if len(result.failures) > FAILURES_SHOWN:
            lines.append(f"- 其余 {len(result.failures) - FAILURES_SHOWN} 次略")
        if any("429" in failure for failure in result.failures):
            lines += ["", "Skiplagged 在限流（429）。等二三十分钟再查，不要连着做全面搜索。"]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class _Sweep:
    """Round-trip searches along both date axes of one airport pair.

    Every day out is searched against one day back, and every day back against one day
    out; those two fixed days are the pivots. A round-trip fare is close to a price for
    its day out plus a price for its day back, so the two axes show which days are cheap
    in each direction. That gives the cheapest combination even when it lies on neither
    axis, at a search per day instead of a search per pair of days.
    """

    origin: str
    destination: str
    out_days: tuple[date, ...]  # the days with a flight out
    back_days: tuple[date, ...]
    pivot_out: date
    pivot_back: date

    def searches(self) -> list[Dates]:
        along_out = [(self.origin, self.destination, day, self.pivot_back) for day in self.out_days]
        along_back = [(self.origin, self.destination, self.pivot_out, day) for day in self.back_days]
        return along_out + along_back  # the pivots appear on both axes; the prober searches them once

    def axes(self, quotes: Quotes) -> dict[str, tuple[dict[date, float], dict[date, float], float]]:
        """For each outbound flight quoted at the pivots: its fare by day out, by day back, and at the pivots."""
        found = {}
        at_pivots = quotes.get((self.origin, self.destination, self.pivot_out, self.pivot_back), {})
        for flight, fare in at_pivots.items():
            by_out = {
                day: quotes[dates][flight]
                for day in self.out_days
                if flight in quotes.get(dates := (self.origin, self.destination, day, self.pivot_back), {})
            }
            by_back = {
                day: quotes[dates][flight]
                for day in self.back_days
                if flight in quotes.get(dates := (self.origin, self.destination, self.pivot_out, day), {})
            }
            found[flight] = (by_out, by_back, fare)
        return found


class _Prober:
    """Runs round-trip searches on Skiplagged, never the same airports and dates twice."""

    def __init__(
        self,
        backs: Sequence[Offer],
        watch: Watch,
        currency: str,
        search: Search,
        failures: list[str],
        quotes: Quotes,
    ):
        self.fares: list[Priced] = []
        self.searches = 0
        self._quotes = quotes  # filled in as searches are made; an empty entry means nothing was quoted
        self._backs, self._watch, self._currency = backs, watch, currency
        self._search, self._failures = search, failures

    def search(self, plan: Iterable[Dates]) -> None:
        todo = [dates for dates in dict.fromkeys(plan) if dates not in self._quotes]
        if not todo:
            return
        queries = [skiplagged.round_trip_query(*dates, self._watch, self._currency) for dates in todo]
        for dates in todo:
            self._quotes[dates] = {}
        answers = self._search(queries)
        self.searches += len(answers)
        for dates, query, answer in zip(todo, queries, answers):
            for offer in _parse(query, answer, self._watch, self._failures):
                if not _fits(offer.leg, self._watch, outbound=True):
                    continue
                flight = offer.leg.flights
                self._quotes[dates][flight] = min(offer.price, self._quotes[dates].get(flight, offer.price))
                self.fares.append(_round_trip(offer, dates[3], self._backs))


def _offers(
    queries: Sequence[dict[str, Any]],
    answers: Sequence[dict[str, Any] | SourceError],
    watch: Watch,
    *,
    outbound: bool,
    failures: list[str],
) -> list[Offer]:
    """The one-way offers that fit the watch, one (the cheapest) per flight and day."""
    best: dict[tuple[str, str], Offer] = {}
    for query, answer in zip(queries, answers):
        for offer in _parse(query, answer, watch, failures):
            flight = (offer.leg.flights, offer.leg.departure)
            if _fits(offer.leg, watch, outbound=outbound) and (
                flight not in best or offer.price < best[flight].price
            ):
                best[flight] = offer
    return list(best.values())


def _pairs(outs: Sequence[Offer], backs: Sequence[Offer]) -> list[Priced]:
    """Trips made of two separate one-way tickets.

    Only the cheapest few offers between each two airports are paired. The rest cannot make
    a cheaper trip, and this way no airport is crowded out by another one's cheaper flights.
    """

    def cheapest(offers: Sequence[Offer]) -> list[Offer]:
        kept: dict[tuple[str, str], list[Offer]] = defaultdict(list)
        for offer in sorted(offers, key=lambda offer: offer.price):
            airports = (offer.leg.origin, offer.leg.destination)
            if len(kept[airports]) < PAIRED_PER_AIRPORT:
                kept[airports].append(offer)
        return [offer for offers_of_pair in kept.values() for offer in offers_of_pair]

    return [
        (
            Itinerary(
                source=skiplagged.SOURCE,
                price=out.price + back.price,
                currency=out.currency,
                outbound=out.leg,
                inbound=back.leg,
                booking_url=out.url,
                separate_tickets=True,
            ),
            (out.url, back.url),
        )
        for out in cheapest(outs)
        for back in cheapest(backs)
        if out.currency == back.currency
    ]


def _round_trip_plan(
    outs: Sequence[Offer], backs: Sequence[Offer], watch: Watch
) -> tuple[list[_Sweep], list[Dates]]:
    """Which airport pairs to sweep, and the single searches for the rest.

    The pairs with the cheapest one-way totals are swept, as cheap one-ways mark the
    routes where airlines are undercutting each other. Every other pair gets a search per
    airline on that airline's cheapest one-way day out and day back: a weaker guess, since
    a cheap one-way day is usually, not always, a cheap round-trip day.
    """
    centre_out, centre_back = _middle(watch.depart), _middle(watch.ret)
    flights_out: dict[Airports, list[Offer]] = defaultdict(list)
    flights_back: dict[Airports, list[Offer]] = defaultdict(list)
    for offer in outs:
        flights_out[(offer.leg.origin, offer.leg.destination)].append(offer)
    for offer in backs:  # filed under the trip it serves: a flight LAX-HKG belongs to (HKG, LAX)
        flights_back[(offer.leg.destination, offer.leg.origin)].append(offer)

    def one_way_total(pair: Airports) -> float:
        return min(o.price for o in flights_out[pair]) + min(o.price for o in flights_back[pair])

    served = sorted(flights_out.keys() & flights_back.keys(), key=lambda pair: (one_way_total(pair), pair))
    sweeps = [
        _Sweep(
            origin=pair[0],
            destination=pair[1],
            out_days=tuple(sorted({offer.leg.departure_date for offer in flights_out[pair]})),
            back_days=tuple(sorted({offer.leg.departure_date for offer in flights_back[pair]})),
            pivot_out=_cheapest_day(flights_out[pair], centre_out),
            pivot_back=_cheapest_day(flights_back[pair], centre_back),
        )
        for pair in served[:SWEPT_AIRPORT_PAIRS]
    ]
    probes: list[Dates] = []
    for pair in served[SWEPT_AIRPORT_PAIRS:]:
        by_airline_out: dict[str, list[Offer]] = defaultdict(list)
        by_airline_back: dict[str, list[Offer]] = defaultdict(list)
        for offer in flights_out[pair]:
            by_airline_out[_airline(offer)].append(offer)
        for offer in flights_back[pair]:
            by_airline_back[_airline(offer)].append(offer)
        both_ways = sorted(
            by_airline_out.keys() & by_airline_back.keys(),
            key=lambda airline: (
                min(o.price for o in by_airline_out[airline]) + min(o.price for o in by_airline_back[airline]),
                airline,
            ),
        )
        of_pair = [
            (*pair, _cheapest_day(by_airline_out[airline], centre_out), _cheapest_day(by_airline_back[airline], centre_back))
            for airline in both_ways
        ]
        probes += list(dict.fromkeys(of_pair))[:PROBES_PER_OTHER_PAIR]
    return sweeps, probes


def _worth_verifying(sweeps: Sequence[_Sweep], quotes: Quotes, watch: Watch) -> list[Dates]:
    """The date pairs the sweeps predict to be cheapest that no search has covered, best first.

    For each flight, the cheapest day out and the cheapest day back are known from the two
    axes. Their fare together is predicted as the sum of the two axis fares less the fare
    at the pivots, which both of them include.
    """
    predicted: list[tuple[float, Dates]] = []
    for sweep in sweeps:
        for by_out, by_back, at_pivots in sweep.axes(quotes).values():
            if not by_out or not by_back:
                continue
            day_out = _cheapest_key(by_out, _middle(watch.depart))
            day_back = _cheapest_key(by_back, _middle(watch.ret))
            dates = (sweep.origin, sweep.destination, day_out, day_back)
            if dates not in quotes:
                predicted.append((by_out[day_out] + by_back[day_back] - at_pivots, dates))
    return list(dict.fromkeys(dates for _, dates in sorted(predicted)))[:VERIFIED]


def _with_cheap_days(find: Find, sweeps: Sequence[_Sweep], quotes: Quotes) -> Find:
    """Adds, to a round-trip fare from a swept airport pair, the days on which it is at its lowest."""
    itinerary = find.itinerary
    if itinerary.source != skiplagged.SOURCE or itinerary.separate_tickets:
        return find
    for sweep in sweeps:
        if (sweep.origin, sweep.destination) != (itinerary.outbound.origin, itinerary.outbound.destination):
            continue
        axes = sweep.axes(quotes).get(itinerary.outbound.flights)
        if axes and axes[0] and axes[1]:
            by_out, by_back, _ = axes
            return replace(
                find,
                cheap_days=(
                    tuple(sorted(day for day, fare in by_out.items() if fare == min(by_out.values()))),
                    tuple(sorted(day for day, fare in by_back.items() if fare == min(by_back.values()))),
                ),
            )
    return find


def _round_trip(offer: Offer, back_day: date, backs: Sequence[Offer]) -> Priced:
    """A round-trip fare with the return flight it most likely includes.

    That is the same airline's cheapest one-way flight back that day. When the one-way
    searches found none (a gap in their data, or a codeshare number sold only one way) the
    fare is still real, so it is kept with the return flight left open, e.g. "UA?".
    """
    airline = _airline(offer)
    returns = [
        back.leg
        for back in sorted(backs, key=lambda candidate: (candidate.price, candidate.leg.departure))
        if back.leg.origin == offer.leg.destination
        and back.leg.destination == offer.leg.origin
        and back.leg.departure_date == back_day
        and _airline(back) == airline
    ]
    open_return = Leg(
        (
            Segment(
                carrier=airline,
                flight_number=f"{airline}?",
                origin=offer.leg.destination,
                destination=offer.leg.origin,
                departure=back_day.isoformat(),  # the day is known, the time is not
                arrival=skiplagged.UNKNOWN,
                cabin=offer.leg.segments[0].cabin,
            ),
        )
    )
    itinerary = Itinerary(
        source=skiplagged.SOURCE,
        price=offer.price,
        currency=offer.currency,
        outbound=offer.leg,
        inbound=returns[0] if returns else open_return,
        booking_url=offer.url,
        inbound_assumed=True,
    )
    return itinerary, (offer.url,)


def _rank(candidates: Sequence[Priced], watch: Watch) -> tuple[Find, ...]:
    """Finds that satisfy the watch, cheapest first, without the repeats.

    The same aircraft sold under several flight numbers, and the same flights on several
    dates, count once. Two one-way tickets are listed once per pair of airports, and only
    where no round-trip ticket between those airports is as cheap.
    """
    alike: dict[tuple[Any, ...], list[Priced]] = {}
    for itinerary, links in candidates:
        if rules.violations(itinerary, watch):
            continue
        back = itinerary.inbound
        if itinerary.separate_tickets:
            kind: tuple[Any, ...] = (itinerary.outbound.origin, back.destination)
        elif itinerary.inbound_assumed:  # the return flight is a guess, so it cannot tell fares apart
            kind = (_aircraft(itinerary.outbound), back.destination)
        else:
            kind = (_aircraft(itinerary.outbound), _aircraft(back))
        alike.setdefault((itinerary.source, itinerary.separate_tickets, kind), []).append((itinerary, links))

    def off_centre(itinerary: Itinerary) -> int:
        return abs((itinerary.outbound.departure_date - _middle(watch.depart)).days) + abs(
            (itinerary.inbound.departure_date - _middle(watch.ret)).days
        )

    finds = []
    for options in alike.values():
        options.sort(key=lambda option: (option[0].price, off_centre(option[0]), option[0].key))
        best, links = options[0]
        dates = (best.outbound.departure_date, best.inbound.departure_date)
        same_price: tuple[tuple[date, date], ...] = ()
        if not best.separate_tickets:
            same_price = tuple(
                sorted(
                    {(it.outbound.departure_date, it.inbound.departure_date) for it, _ in options if it.price == best.price}
                    - {dates}
                )
            )
        finds.append(Find(best, links, same_price))

    round_trips = [find.itinerary for find in finds if not find.itinerary.separate_tickets]

    def beaten(pair: Itinerary) -> bool:
        return any(
            ticket.price <= pair.price
            and ticket.outbound.origin == pair.outbound.origin
            and ticket.inbound.destination == pair.inbound.destination
            for ticket in round_trips
        )

    finds = [find for find in finds if not (find.itinerary.separate_tickets and beaten(find.itinerary))]
    return tuple(sorted(finds, key=lambda find: (find.itinerary.price, find.itinerary.key)))


def _parse(
    query: dict[str, Any], answer: dict[str, Any] | SourceError, watch: Watch, failures: list[str]
) -> list[Offer]:
    """The offers in one answer; a search that failed is noted in `failures` and gives none."""
    try:
        if isinstance(answer, SourceError):
            raise answer
        return skiplagged.parse_offers(answer, watch.cabin)
    except SourceError as exc:
        origins = "+".join([query["origin"], *query.get("additionalOrigins", [])])
        destinations = "+".join([query["destination"], *query.get("additionalDestinations", [])])
        days = "/".join(filter(None, [query["departureDate"], query.get("returnDate")]))
        failures.append(f"{skiplagged.SOURCE} {origins}→{destinations} {days}: {exc}")
        return []


def _fits(leg: Leg, watch: Watch, *, outbound: bool) -> bool:
    """Whether one direction of travel is inside the watch. Whole trips are checked by rules.violations."""
    if outbound:
        origins, destinations, window = watch.origins, watch.destinations, watch.depart
    else:
        origins, destinations, window = watch.destinations, watch.origins, watch.ret
    return (
        leg.origin in origins
        and leg.destination in destinations
        and leg.departure_date in window
        and not (watch.nonstop and leg.stops)
    )


def _cheapest_day(offers: Sequence[Offer], centre: date) -> date:
    """The day of the cheapest offer; between equal prices, the day nearest `centre`."""
    best = min(offers, key=lambda offer: (offer.price, abs((offer.leg.departure_date - centre).days), offer.leg.departure))
    return best.leg.departure_date


def _cheapest_key(fares: Mapping[date, float], centre: date) -> date:
    """The day with the lowest fare; between equal fares, the day nearest `centre`."""
    return min(fares, key=lambda day: (fares[day], abs((day - centre).days), day))


def _row(find: Find) -> str:
    it = find.itinerary
    return (
        f"| {alerts.money(it.price, it.currency)} | {'两张单程' if it.separate_tickets else '往返'} "
        f"| {_describe(it.outbound)} | {_describe(it.inbound)}{' ※' if it.inbound_assumed else ''} | {it.source} |"
    )


def _describe(leg: Leg) -> str:
    day = leg.departure_date
    time = leg.departure[11:16]  # empty for a return flight left open
    return f"{leg.flights} {alerts.route(leg)} {day:%m-%d} 周{WEEKDAYS[day.weekday()]} {time}".rstrip()


def _aircraft(leg: Leg) -> tuple[str, str, str, int]:
    """What identifies the flight actually flown, whichever airline's number it is sold under."""
    return (leg.origin, leg.destination, leg.departure[11:16], leg.stops)


def _airline(offer: Offer) -> str:
    return offer.leg.segments[0].carrier


def _days(window: DateRange) -> list[date]:
    return [window.start + timedelta(days=offset) for offset in range((window.end - window.start).days + 1)]


def _middle(window: DateRange) -> date:
    return window.start + (window.end - window.start) / 2


def _groups(airports: Sequence[str]) -> list[tuple[str, ...]]:
    size = skiplagged.AIRPORTS_PER_SEARCH
    return [tuple(airports[start : start + size]) for start in range(0, len(airports), size)]
