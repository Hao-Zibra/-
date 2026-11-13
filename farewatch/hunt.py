"""A one-off search: the cheapest ways to fly a trip right now, across every source.

`scan` asks a little and often. A hunt asks thoroughly once: it covers every day of both
date windows on Skiplagged, which has the airlines Kiwi lacks, and adds Kiwi's own offers.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from farewatch import alerts, kiwi, rules, skiplagged
from farewatch.config import ConfigError, DateRange, Watch
from farewatch.model import Itinerary, Leg, SourceError
from farewatch.skiplagged import Offer

Search = Callable[[Sequence[dict[str, Any]]], list[dict[str, Any] | SourceError]]
Fetch = Callable[[Watch, str], list[Itinerary]]
Priced = tuple[Itinerary, tuple[str, ...]]  # an itinerary and the links to book it
Route = tuple[str, str, str]  # origin, destination, airline
Dates = tuple[str, str, date, date]  # origin, destination, day out, day back

# Sources that answer for a whole date window in one go.
RANGE_SOURCES: Mapping[str, Fetch] = {kiwi.SOURCE: kiwi.fetch}

ROUND_TRIP_PROBES = 12  # first round of round-trip searches, one per airline and airport pair
REFINED_AIRLINES = 2  # airlines whose lowest fare gets its other likely dates searched too
TIED_DAYS = 4  # at most this many other days searched per direction for each of those
MAX_SEARCHES = 120  # the endpoint is free, shared and rate-limited, so a bigger hunt is refused
PAIRED_PER_AIRPORT = 6  # cheapest one-way offers between each two airports that get paired into trips
FAILURES_SHOWN = 5

WEEKDAYS = "一二三四五六日"


@dataclass(frozen=True)
class Find:
    """One way to fly the trip: particular flights, on their cheapest dates."""

    itinerary: Itinerary
    links: tuple[str, ...]  # where to book; two links for two one-way tickets
    # Other (day out, day back) combinations on which the same flights cost the same.
    same_price: tuple[tuple[date, date], ...] = ()


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

    A Skiplagged search covers a single day, so that source is worked in steps: one-way
    searches for every day of both windows, every pairing of the cheapest one-ways, then
    round-trip searches on the dates where round-trip fares look cheapest.
    """
    origin_groups, destination_groups = _groups(watch.origins), _groups(watch.destinations)
    out_queries = [
        skiplagged.one_way_query(origins, destinations, day, watch, currency)
        for day in _days(watch.depart)
        for origins in origin_groups
        for destinations in destination_groups
    ]
    back_queries = [
        skiplagged.one_way_query(destinations, origins, day, watch, currency)
        for day in _days(watch.ret)
        for destinations in destination_groups
        for origins in origin_groups
    ]
    planned = len(out_queries) + len(back_queries) + ROUND_TRIP_PROBES + 2 * REFINED_AIRLINES * TIED_DAYS
    if planned > MAX_SEARCHES:
        raise ConfigError(
            f"this hunt could take {planned} searches and the limit is {MAX_SEARCHES}: "
            "name fewer airports or narrow the date windows"
        )

    failures: list[str] = []
    candidates: list[Priced] = []
    searches = 0
    try:
        answers = search(out_queries + back_queries)
        searches += len(answers)
        outs = _offers(out_queries, answers[: len(out_queries)], watch, outbound=True, failures=failures)
        backs = _offers(back_queries, answers[len(out_queries) :], watch, outbound=False, failures=failures)
        candidates += _pairs(outs, backs)

        prober = _Prober(backs, watch, currency, search, failures)
        try:
            prober.search(_first_probes(outs, backs, watch))
            _refine(prober, outs, backs, watch)
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

    return Hunt(finds=_rank(candidates, watch), searches=searches, failures=tuple(failures))


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
        if best.same_price:
            lines += [
                "",
                "最低价的同一组航班，下面这些日期实查也是同样价格（去 / 回）："
                + "，".join(f"{out:%m-%d} / {back:%m-%d}" for out, back in best.same_price),
            ]
        lines += ["", "最低价下单：" + " 和 ".join(link for link in best.links if link)]
        if any(find and find.itinerary.inbound_assumed for find in [*shown, *per_origin]):
            lines += [
                "",
                "※ 回程航班待确认：这个来源的往返价只标明去程航班，表里列的是同一航司当天最便宜的回程航班。",
            ]
    if result.failures:
        lines += ["", f"有 {len(result.failures)} 次搜索没有查到结果，上面的结果可能不全："]
        lines += [f"- {failure}" for failure in result.failures[:FAILURES_SHOWN]]
        if len(result.failures) > FAILURES_SHOWN:
            lines.append(f"- 其余 {len(result.failures) - FAILURES_SHOWN} 次略")
        if any("429" in failure for failure in result.failures):
            lines += ["", "Skiplagged 在限流（429）。等二三十分钟再查，不要连着做全面搜索。"]
    return "\n".join(lines) + "\n"


class _Prober:
    """Runs round-trip searches on Skiplagged, never the same airports and dates twice."""

    def __init__(self, backs: Sequence[Offer], watch: Watch, currency: str, search: Search, failures: list[str]):
        self.fares: list[Priced] = []
        self.searches = 0
        self._searched: set[Dates] = set()
        self._backs, self._watch, self._currency = backs, watch, currency
        self._search, self._failures = search, failures

    def search(self, plan: Iterable[Dates]) -> None:
        todo = [dates for dates in dict.fromkeys(plan) if dates not in self._searched]
        if not todo:
            return
        self._searched.update(todo)
        queries = [skiplagged.round_trip_query(*dates, self._watch, self._currency) for dates in todo]
        answers = self._search(queries)
        self.searches += len(answers)
        for (_, _, _, back_day), query, answer in zip(todo, queries, answers):
            for offer in _parse(query, answer, self._watch, self._failures):
                fare = _round_trip(offer, back_day, self._backs, self._watch)
                if fare:
                    self.fares.append(fare)


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


def _first_probes(outs: Sequence[Offer], backs: Sequence[Offer], watch: Watch) -> list[Dates]:
    """Where to look for round-trip fares first.

    A round-trip fare is usually far below two one-ways, but pricing every date pair would
    take a search each. An airline's cheapest one-way days are mostly the days its cheapest
    round-trip fares are sold, so each airline flying both directions between two airports
    gets one search, on its cheapest one-way day out and its cheapest one-way day back.
    Every pair of airports gets its most promising airline searched before any pair gets
    a second one, so an expensive origin is not crowded out by a cheap one.
    """
    best_out = _cheapest_per_route(outs, outbound=True, centre=_middle(watch.depart))
    best_back = _cheapest_per_route(backs, outbound=False, centre=_middle(watch.ret))
    by_airports: dict[tuple[str, str], list[Route]] = defaultdict(list)
    for route in sorted(
        best_out.keys() & best_back.keys(), key=lambda route: (best_out[route].price + best_back[route].price, route)
    ):
        by_airports[route[:2]].append(route)
    in_turn = sorted(
        (turn, best_out[route].price + best_back[route].price, route)
        for routes in by_airports.values()
        for turn, route in enumerate(routes)
    )
    plan = [
        (route[0], route[1], best_out[route].leg.departure_date, best_back[route].leg.departure_date)
        for _, _, route in in_turn
    ]
    return list(dict.fromkeys(plan))[:ROUND_TRIP_PROBES]


def _refine(prober: _Prober, outs: Sequence[Offer], backs: Sequence[Offer], watch: Watch) -> None:
    """Searches the other likely dates of the airlines with the lowest round-trip fares.

    "Mostly" in _first_probes is not "always": a day can be among an airline's cheapest
    one-way days and still lack its cheapest round-trip fare. So for the leading airlines
    every day tied for cheapest one-way is searched: first the days back against the day
    out already found, then the days out against the best day back. That corrects a first
    probe that landed on such a day, and shows on which other dates the fare exists.
    """
    for sweep_back in (True, False):
        plan: list[Dates] = []
        for route, best in _leaders(prober.fares):
            origin, destination, _ = route
            if sweep_back:
                days = _tied_days(backs, route, outbound=False, centre=_middle(watch.ret))
                plan += [(origin, destination, best.outbound.departure_date, day) for day in days]
            else:
                days = _tied_days(outs, route, outbound=True, centre=_middle(watch.depart))
                plan += [(origin, destination, day, best.inbound.departure_date) for day in days]
        prober.search(plan)


def _leaders(fares: Sequence[Priced]) -> list[tuple[Route, Itinerary]]:
    """The routes with the lowest round-trip fares so far, each with its cheapest itinerary."""
    best: dict[Route, Itinerary] = {}
    for itinerary, _ in fares:
        leg = itinerary.outbound
        route = (leg.origin, leg.destination, leg.segments[0].carrier)
        if route not in best or itinerary.price < best[route].price:
            best[route] = itinerary
    return sorted(best.items(), key=lambda entry: (entry[1].price, entry[0]))[:REFINED_AIRLINES]


def _round_trip(offer: Offer, back_day: date, backs: Sequence[Offer], watch: Watch) -> Priced | None:
    """A round-trip fare with the return flight it most likely includes, or None if that cannot be told."""
    if not _fits(offer.leg, watch, outbound=True):
        return None
    returns = [
        back
        for back in backs
        if back.leg.origin == offer.leg.destination
        and back.leg.destination == offer.leg.origin
        and back.leg.departure_date == back_day
        and _airline(back) == _airline(offer)
    ]
    if not returns:  # e.g. a codeshare flight number that is only sold one way
        return None
    back = min(returns, key=lambda candidate: (candidate.price, candidate.leg.departure))
    itinerary = Itinerary(
        source=skiplagged.SOURCE,
        price=offer.price,
        currency=offer.currency,
        outbound=offer.leg,
        inbound=back.leg,
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


def _route_of(offer: Offer, *, outbound: bool) -> Route:
    """The route an offer serves, named from the trip's point of view: a flight back from
    LAX to HKG belongs to the route (HKG, LAX, airline)."""
    leg = offer.leg
    ends = (leg.origin, leg.destination) if outbound else (leg.destination, leg.origin)
    return (*ends, _airline(offer))


def _cheapest_per_route(offers: Sequence[Offer], *, outbound: bool, centre: date) -> dict[Route, Offer]:
    """The cheapest offer on each route; between equal prices, the day nearest `centre`."""
    best: dict[Route, tuple[tuple[float, int, str], Offer]] = {}
    for offer in offers:
        route = _route_of(offer, outbound=outbound)
        rank = (offer.price, abs((offer.leg.departure_date - centre).days), offer.leg.departure)
        if route not in best or rank < best[route][0]:
            best[route] = (rank, offer)
    return {route: offer for route, (_, offer) in best.items()}


def _tied_days(offers: Sequence[Offer], route: Route, *, outbound: bool, centre: date) -> list[date]:
    """The days on which the route's one-way price is at its lowest, nearest `centre` first."""
    cheapest: dict[date, float] = {}
    for offer in offers:
        if _route_of(offer, outbound=outbound) == route:
            day = offer.leg.departure_date
            cheapest[day] = min(offer.price, cheapest.get(day, offer.price))
    if not cheapest:
        return []
    floor = min(cheapest.values())
    tied = [day for day, price in cheapest.items() if price == floor]
    return sorted(tied, key=lambda day: (abs((day - centre).days), day))[: TIED_DAYS + 1]


def _row(find: Find) -> str:
    it = find.itinerary
    return (
        f"| {alerts.money(it.price, it.currency)} | {'两张单程' if it.separate_tickets else '往返'} "
        f"| {_describe(it.outbound)} | {_describe(it.inbound)}{' ※' if it.inbound_assumed else ''} | {it.source} |"
    )


def _describe(leg: Leg) -> str:
    day = leg.departure_date
    return f"{leg.flights} {alerts.route(leg)} {day:%m-%d} 周{WEEKDAYS[day.weekday()]} {leg.departure[11:16]}"


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
