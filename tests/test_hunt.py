"""The hunt against a small made-up timetable served the way Skiplagged serves it."""

from datetime import date

import pytest

from farewatch import hunt as hunting
from farewatch.config import ConfigError, DateRange
from farewatch.model import SourceError
from tests.helpers import make_watch, seg, trip

# flight number: origin, destination, departure time, arrival time
TIMETABLE = {
    "DL88": ("HKG", "LAX", "10:45", "07:56"),
    "DL87": ("LAX", "HKG", "23:55", "07:25"),
    "UA153": ("HKG", "LAX", "11:25", "08:05"),
    "UA152": ("LAX", "HKG", "11:10", "19:15"),
    "MU583": ("PVG", "LAX", "13:10", "09:05"),
    "MU586": ("LAX", "PVG", "11:30", "17:45"),
}
MARCH = [f"2027-03-{day}" for day in range(10, 17)]
APRIL = [f"2027-04-{day:02d}" for day in range(7, 14)]


class FakeSkiplagged:
    """Answers flight searches from `one_way` prices and a `round_trip` price function.

    one_way: flight number -> {day: price}. A flight flies only on the days it has a price.
    round_trip(flight, day_out, day_back) -> price, or None when there is no such fare.
    """

    def __init__(self, one_way, round_trip, *, timetable=TIMETABLE, refuse=()):
        self.one_way, self.round_trip, self.timetable = one_way, round_trip, timetable
        self.refuse = set(refuse)  # (origin, departureDate, returnDate) of searches that get an error
        self.batches = []

    def __call__(self, queries):
        self.batches.append(list(queries))
        return [self._answer(query) for query in queries]

    def _answer(self, query):
        day_out, day_back = query["departureDate"], query.get("returnDate")
        if (query["origin"], day_out, day_back) in self.refuse:
            return SourceError("Skiplagged rejected the search: Request failed with status code 429")
        origins = [query["origin"], *query.get("additionalOrigins", [])]
        destinations = [query["destination"], *query.get("additionalDestinations", [])]
        cards = []
        for number, (origin, destination, departs, arrives) in self.timetable.items():
            if origin not in origins or destination not in destinations or day_out not in self.one_way.get(number, {}):
                continue
            price = self.round_trip(number, day_out, day_back) if day_back else self.one_way[number][day_out]
            if price is not None:
                cards.append(self._card(number, origin, destination, departs, arrives, day_out, day_back, price))
        return {"searchUrl": "https://skiplagged.test/search", "flights": sorted(cards, key=lambda c: c["price"]["amount"])}

    @staticmethod
    def _card(number, origin, destination, departs, arrives, day_out, day_back, price):
        # Like the real thing, a round-trip card names some return flight that the price is not for.
        trip_id, days = (f"{number},XX1", f"{day_out}-{day_back}") if day_back else (number, day_out)
        return {
            "id": f"{origin}-{destination}-{days}-trip={trip_id}",
            "departure": {"airport": origin, "dateTime": f"{day_out}T{departs}:00+08:00"},
            "arrival": {"airport": destination, "dateTime": f"{day_out}T{arrives}:00-08:00"},
            "layovers": 0,
            "price": {"amount": price, "currency": "USD"},
            "deepLink": f"https://skiplagged.test/{origin}/{destination}/{days}#trip={trip_id}",
        }


def every_day(days, price, **exceptions):
    return {day: exceptions.get(day[-5:].replace("-", "_"), price) for day in days}


def world(**options):
    """Delta's cheap one-way days are 03-12/13 out and 04-08/09/10 back, but its cheap
    round-trip fare exists only for 03-12/13 with 04-09/10: on 04-08 the one-way price misleads."""
    one_way = {
        "DL88": {day: 2100 if day[-2:] in ("12", "13") else 4900 for day in MARCH},
        "DL87": {day: 3000 if day[-2:] in ("08", "09", "10") else 3300 for day in APRIL},
        "UA153": {day: 3500 for day in MARCH},
        "UA152": {day: 3450 for day in APRIL},
        "MU583": {"2027-03-14": 3700},
        "MU586": {"2027-04-09": 3650},
    }

    def round_trip(flight, day_out, day_back):
        if flight == "DL88":
            return 3200 if day_out[-2:] in ("12", "13") and day_back[-2:] in ("09", "10") else 4300
        if flight == "UA153":
            return 5100
        if flight == "MU583":
            return 5400 if day_back == "2027-04-09" else None
        return None

    return FakeSkiplagged(one_way, round_trip, **options)


def run(skiplagged=None, watch=None, **options):
    skiplagged = skiplagged or world()
    options.setdefault("range_sources", {})
    return hunting.hunt(watch or make_watch(), "USD", search=skiplagged, **options), skiplagged


def summary(result):
    return [
        (find.itinerary.price, find.itinerary.outbound.flights, find.itinerary.inbound.flights, find.itinerary.separate_tickets)
        for find in result.finds
    ]


# --- what is found ---------------------------------------------------------------------


def test_finds_the_round_trip_fare_and_the_other_ways_to_fly():
    result, _ = run()

    assert summary(result) == [
        (3200, "DL88", "DL87", False),
        (5100, "UA153", "UA152", False),
        (5400, "MU583", "MU586", False),
        (5750, "DL88", "MU586", True),  # out of Hong Kong, back to Shanghai, on two tickets
        (6700, "MU583", "DL87", True),
    ]
    assert result.failures == ()


def test_the_cheapest_find_carries_its_dates_its_link_and_the_other_dates_at_that_price():
    result, _ = run()

    best = result.finds[0]
    assert (best.itinerary.outbound.departure, best.itinerary.inbound.departure) == (
        "2027-03-13T10:45:00",  # of the tied days, the ones nearest the middle of each window
        "2027-04-10T23:55:00",
    )
    assert best.itinerary.inbound_assumed and not best.itinerary.separate_tickets
    assert best.links == ("https://skiplagged.test/HKG/LAX/2027-03-13-2027-04-10#trip=DL88,XX1",)
    assert best.same_price == ((date(2027, 3, 12), date(2027, 4, 10)), (date(2027, 3, 13), date(2027, 4, 9)))
    assert best.cheap_days == ((date(2027, 3, 12), date(2027, 3, 13)), (date(2027, 4, 9), date(2027, 4, 10)))


def test_two_one_way_tickets_carry_both_links():
    result, _ = run()

    pair = next(find for find in result.finds if find.itinerary.separate_tickets)
    assert pair.links == (
        "https://skiplagged.test/HKG/LAX/2027-03-13#trip=DL88",
        "https://skiplagged.test/LAX/PVG/2027-04-09#trip=MU586",
    )
    assert pair.same_price == ()


def test_two_one_ways_between_the_same_airports_are_dropped_when_a_round_trip_is_as_cheap():
    result, _ = run()

    pairs = [(f.itinerary.outbound.origin, f.itinerary.inbound.destination) for f in result.finds if f.itinerary.separate_tickets]
    assert pairs == [("HKG", "PVG"), ("PVG", "HKG")]  # HKG-HKG and PVG-PVG have cheaper round trips


def test_two_one_ways_are_kept_when_they_beat_every_round_trip():
    cheap_one_ways = world()
    cheap_one_ways.one_way["UA153"] = {day: 1500 for day in MARCH}
    cheap_one_ways.one_way["UA152"] = {day: 1600 for day in APRIL}

    result, _ = run(cheap_one_ways)

    assert summary(result)[0] == (3100, "UA153", "UA152", True)


def test_a_misleading_cheap_one_way_day_is_corrected_by_searching_every_day_back():
    # The middle of this return window is 04-08, the day Delta's one-way price is low but
    # its cheap round-trip fare is missing, so the day every day out is searched against is a poor one.
    watch = make_watch(ret=DateRange(date(2027, 4, 6), date(2027, 4, 10)))
    skiplagged = world()
    skiplagged.one_way["DL87"] = {f"2027-04-{d:02d}": 3000 if d in (8, 9, 10) else 3300 for d in range(6, 11)}
    skiplagged.one_way["UA152"] = {f"2027-04-{d:02d}": 3450 for d in range(6, 11)}

    result, skiplagged = run(skiplagged, watch)

    swept = [(q["origin"], q["departureDate"], q["returnDate"]) for q in skiplagged.batches[1]]
    assert ("HKG", "2027-03-12", "2027-04-08") in swept
    assert summary(result)[0] == (3200, "DL88", "DL87", False)
    assert result.finds[0].itinerary.inbound.departure_date == date(2027, 4, 9)


def test_a_fare_cheapest_on_neither_searched_axis_is_predicted_and_then_confirmed():
    # Delta's fare is a price for the day out plus a price for the day back. Its one cheap
    # day out (03-11) and one cheap day back (04-12) are not the days its one-way prices
    # point to (03-13 and 04-10), so no first-round search pairs them.
    skiplagged = world()
    united = skiplagged.round_trip

    def round_trip(flight, day_out, day_back):
        if flight != "DL88":
            return united(flight, day_out, day_back)
        return (1500 if day_out == "2027-03-11" else 2500) + (1700 if day_back == "2027-04-12" else 2700)

    skiplagged.round_trip = round_trip

    result, skiplagged = run(skiplagged)

    asked = lambda batch: [(q["origin"], q["departureDate"], q["returnDate"]) for q in batch]
    assert ("HKG", "2027-03-11", "2027-04-12") not in asked(skiplagged.batches[1])
    assert asked(skiplagged.batches[2]) == [("HKG", "2027-03-11", "2027-04-12")]
    best = result.finds[0]
    assert (best.itinerary.price, best.itinerary.outbound.flights) == (3200, "DL88")
    assert (best.itinerary.outbound.departure_date, best.itinerary.inbound.departure_date) == (date(2027, 3, 11), date(2027, 4, 12))
    assert best.cheap_days == ((date(2027, 3, 11),), (date(2027, 4, 12),))
    assert best.same_price == ()


def test_when_confirmations_are_limited_the_cheapest_prediction_is_confirmed_first(monkeypatch):
    monkeypatch.setattr(hunting, "VERIFIED", 1)
    skiplagged = world()
    china_eastern = skiplagged.round_trip

    def round_trip(flight, day_out, day_back):
        if flight == "DL88":  # cheapest together: 03-11 with 04-12, at 3200
            return (1500 if day_out == "2027-03-11" else 2500) + (1700 if day_back == "2027-04-12" else 2700)
        if flight == "UA153":  # cheapest together: 03-16 with 04-07, at 4500
            return (2000 if day_out == "2027-03-16" else 2600) + (2500 if day_back == "2027-04-07" else 3100)
        return china_eastern(flight, day_out, day_back)

    skiplagged.round_trip = round_trip

    result, skiplagged = run(skiplagged)

    assert [(q["departureDate"], q["returnDate"]) for q in skiplagged.batches[2]] == [("2027-03-11", "2027-04-12")]
    assert summary(result)[0] == (3200, "DL88", "DL87", False)


def test_the_same_aircraft_under_another_flight_number_counts_once():
    codeshare = world(timetable={**TIMETABLE, "NH9153": TIMETABLE["UA153"], "NH9152": TIMETABLE["UA152"]})
    codeshare.one_way["NH9153"] = {day: 3700 for day in MARCH}
    codeshare.one_way["NH9152"] = {day: 3700 for day in APRIL}
    united = codeshare.round_trip
    codeshare.round_trip = lambda flight, out, back: 5150 if flight == "NH9153" else united(flight, out, back)

    result, _ = run(codeshare)

    eleven_twenty_five = [f.itinerary for f in result.finds if f.itinerary.outbound.departure[11:16] == "11:25" and not f.itinerary.separate_tickets]
    assert [(it.outbound.flights, it.price) for it in eleven_twenty_five] == [("UA153", 5100)]


def test_offers_from_range_sources_are_ranked_with_the_rest():
    kiwi_offer = trip(2900, out=[seg("CX880", "HKG", "LAX", "2027-03-12T00:30:00")], back=[seg("CX881", "LAX", "HKG", "2027-04-09T22:45:00")])
    asked = []

    def fetch(watch, currency):
        asked.append((watch.name, currency))
        return [kiwi_offer, trip(9000, out=[seg("PR307", "HKG", "MNL", "2027-03-12T19:05:00"), seg("PR102", "MNL", "LAX", "2027-03-12T22:15:00")])]

    result, _ = run(range_sources={"kiwi": fetch})

    assert asked == [("china-lax", "USD")]
    assert summary(result)[0] == (2900, "CX880", "CX881", False)
    assert result.finds[0].links == ("https://example.test/book",)
    assert "PR307+PR102" not in [f.itinerary.outbound.flights for f in result.finds]  # connecting: not in a nonstop watch


def test_a_round_trip_fare_is_kept_when_no_return_flight_of_that_airline_was_found():
    # The one-way searches show nothing back on United, but the round-trip fare is real.
    no_return = world()
    del no_return.one_way["UA152"]

    result, _ = run(no_return)

    united = next(find for find in result.finds if find.itinerary.outbound.flights == "UA153" and not find.itinerary.separate_tickets)
    back = united.itinerary.inbound
    assert (united.itinerary.price, back.flights, united.itinerary.inbound_assumed) == (5100, "UA?", True)
    assert (back.origin, back.destination, back.departure_date) == ("LAX", "HKG", date(2027, 4, 10))
    assert "| USD 5,100 | 往返 | UA153 HKG → LAX 03-13 周六 11:25 | UA? LAX → HKG 04-10 周六 ※ | skiplagged |" in hunting.render(result, make_watch())


# --- how it searches -------------------------------------------------------------------


def test_one_way_searches_cover_every_day_of_both_windows_in_groups_of_three_airports():
    watch = make_watch(origins=("HKG", "PVG", "PEK", "CAN"), destinations=("LAX", "SFO"))
    _, skiplagged = run(watch=watch)

    sweep = skiplagged.batches[0]
    out = [q for q in sweep if q["origin"] in ("HKG", "CAN")]
    back = [q for q in sweep if q["origin"] == "LAX"]
    assert len(sweep) == len(out) + len(back) == 7 * 2 + 7 * 2
    assert all("returnDate" not in q for q in sweep)
    assert sorted({q["departureDate"] for q in out}) == MARCH
    assert sorted({q["departureDate"] for q in back}) == APRIL
    assert out[0] == {**out[0], "origin": "HKG", "additionalOrigins": ["PVG", "PEK"], "destination": "LAX", "additionalDestinations": ["SFO"]}
    assert out[1]["origin"] == "CAN" and "additionalOrigins" not in out[1]
    assert back[0] == {**back[0], "origin": "LAX", "additionalOrigins": ["SFO"], "destination": "HKG", "additionalDestinations": ["PVG", "PEK"]}


def test_the_airport_pairs_with_the_cheapest_one_ways_are_searched_for_every_day_out_and_every_day_back():
    result, skiplagged = run()

    searches = [(q["origin"], q["destination"], q["departureDate"], q["returnDate"]) for q in skiplagged.batches[1]]
    hong_kong = [(out, back) for origin, _, out, back in searches if origin == "HKG"]
    # Delta's cheapest one-way days nearest the middle of each window are 03-13 and 04-10.
    assert sorted(out for out, back in hong_kong if back == "2027-04-10") == MARCH
    assert sorted(back for out, back in hong_kong if out == "2027-03-13") == APRIL
    assert len(hong_kong) == 7 + 7 - 1
    # China Eastern flies one day each way, so Shanghai's sweep is a single search.
    assert [s for s in searches if s[0] == "PVG"] == [("PVG", "LAX", "2027-03-14", "2027-04-09")]
    assert len(searches) == len(set(searches))
    assert all("additionalOrigins" not in q for q in skiplagged.batches[1])
    assert len(skiplagged.batches) == 2  # nothing predicted cheaper than what was searched
    assert result.searches == sum(len(batch) for batch in skiplagged.batches)


def test_the_other_airport_pairs_get_a_search_per_airline_on_its_cheapest_one_way_days(monkeypatch):
    monkeypatch.setattr(hunting, "SWEPT_AIRPORT_PAIRS", 1)
    skiplagged = world(timetable={**TIMETABLE, "UA199": ("PVG", "LAX", "20:55", "16:35"), "UA198": ("LAX", "PVG", "12:45", "18:55")})
    skiplagged.one_way["UA199"] = {day: 3800 if day == "2027-03-11" else 4500 for day in MARCH}
    skiplagged.one_way["UA198"] = {day: 3900 if day == "2027-04-12" else 4600 for day in APRIL}

    hunting.hunt(make_watch(), "USD", search=skiplagged, range_sources={})

    shanghai = [(q["departureDate"], q["returnDate"]) for q in skiplagged.batches[1] if q["origin"] == "PVG"]
    assert shanghai == [("2027-03-14", "2027-04-09"), ("2027-03-11", "2027-04-12")]  # China Eastern, then United

    monkeypatch.setattr(hunting, "PROBES_PER_OTHER_PAIR", 1)
    again = world(timetable=skiplagged.timetable)
    again.one_way.update({flight: skiplagged.one_way[flight] for flight in ("UA199", "UA198")})
    hunting.hunt(make_watch(), "USD", search=again, range_sources={})

    assert [(q["departureDate"], q["returnDate"]) for q in again.batches[1] if q["origin"] == "PVG"] == [("2027-03-14", "2027-04-09")]


def test_every_pair_of_airports_gets_its_one_ways_paired_however_many_cheaper_ones_another_has(monkeypatch):
    monkeypatch.setattr(hunting, "PAIRED_PER_AIRPORT", 1)

    result, _ = run()

    pairs = [(f.itinerary.outbound.flights, f.itinerary.inbound.flights) for f in result.finds if f.itinerary.separate_tickets]
    assert pairs == [("DL88", "MU586"), ("MU583", "DL87")]


def test_a_hunt_that_would_take_too_many_searches_is_refused_before_any_is_made():
    watch = make_watch(origins=tuple(f"A{a}{b}" for a in "ABCD" for b in "ABCDE"))  # 20 airports: 7 groups
    skiplagged = world()

    # one-way: 7 days x 7 groups x 2 directions = 98; sweeps: 2 x 13; other pairs: 18 x 3; confirmations: 6
    with pytest.raises(ConfigError, match="could take 184 searches and the limit is 120"):
        hunting.hunt(watch, "USD", search=skiplagged, range_sources={})

    assert skiplagged.batches == []


# --- when searches fail ----------------------------------------------------------------


def test_a_failed_search_is_reported_and_the_rest_still_count():
    refused = world(refuse={("HKG", "2027-03-13", "2027-04-10")})

    result, _ = run(refused)

    assert result.failures[0] == (
        "skiplagged HKG→LAX 2027-03-13/2027-04-10: Skiplagged rejected the search: Request failed with status code 429"
    )
    assert (5400, "MU583", "MU586", False) in summary(result)


def test_an_unreachable_endpoint_is_reported_and_range_sources_still_answer():
    def unreachable(queries):
        raise SourceError("Skiplagged could not be reached: ConnectError")

    result = hunting.hunt(make_watch(), "USD", search=unreachable, range_sources={"kiwi": lambda watch, currency: [trip(3900)]})

    assert result.failures == ("skiplagged: Skiplagged could not be reached: ConnectError",)
    assert summary(result) == [(3900, "MU583", "MU586", False)]
    assert result.searches == 1


def test_a_failing_range_source_is_reported():
    def fetch(watch, currency):
        raise SourceError("Kiwi search failed after 3 attempts")

    result, _ = run(range_sources={"kiwi": fetch})

    assert result.failures == ("kiwi: Kiwi search failed after 3 attempts",)
    assert summary(result)[0] == (3200, "DL88", "DL87", False)


# --- the report ------------------------------------------------------------------------


def test_report_lists_the_finds_the_best_of_each_origin_and_how_to_book():
    result, _ = run()

    report = hunting.render(result, make_watch())

    assert report.splitlines()[0] == "直飞商务舱往返 HKG/PVG/TFU → LAX，去 2027-03-10 至 2027-03-16，回 2027-04-07 至 2027-04-13，1 人"
    assert f"查了 {result.searches} 次，找到 5 种走法，最低 USD 3,200。" in report
    assert "| USD 3,200 | 往返 | DL88 HKG → LAX 03-13 周六 10:45 | DL87 LAX → HKG 04-10 周六 23:55 ※ | skiplagged |" in report
    assert "| USD 5,750 | 两张单程 | DL88 HKG → LAX 03-13 周六 10:45 | MU586 LAX → PVG 04-09 周五 11:30 | skiplagged |" in report
    assert "| HKG | USD 3,200 | 往返 | DL88 HKG → LAX 03-13 周六 10:45 |" in report
    assert "| PVG | USD 5,400 | 往返 | MU583 PVG → LAX 03-14 周日 13:10 |" in report
    assert "| TFU | 没有查到 | | | | |" in report
    assert "最低价这组航班的低价日：去程 03-12、03-13；回程 04-09、04-10。两个方向各自逐日实查过，任意搭配按规律应同价。" in report
    assert "实查同价的其他日期组合（去 / 回）：03-12 / 04-10，03-13 / 04-09" in report
    assert "最低价下单：https://skiplagged.test/HKG/LAX/2027-03-13-2027-04-10#trip=DL88,XX1" in report
    assert "※ 回程航班待确认" in report
    assert "没有查到结果" not in report


def test_report_shows_only_the_top_finds_but_counts_them_all():
    result, _ = run()

    report = hunting.render(result, make_watch(), top=2)

    assert "找到 5 种走法" in report
    assert "| USD 5,100 | 往返 | UA153" in report
    assert "| USD 5,750 |" not in report


def test_report_for_two_one_way_tickets_gives_both_links():
    cheap_one_ways = world()
    cheap_one_ways.one_way["UA153"] = {day: 1500 for day in MARCH}
    cheap_one_ways.one_way["UA152"] = {day: 1600 for day in APRIL}
    result, _ = run(cheap_one_ways)

    report = hunting.render(result, make_watch())

    assert "最低价下单：https://skiplagged.test/HKG/LAX/2027-03-13#trip=UA153 和 https://skiplagged.test/LAX/HKG/2027-04-10#trip=UA152" in report


def test_report_says_so_when_nothing_was_found():
    empty = FakeSkiplagged({}, lambda *args: None)
    result, _ = run(empty)

    report = hunting.render(result, make_watch())

    assert f"查了 {result.searches} 次，没有找到符合条件的票。" in report
    assert "|" not in report


def test_report_sums_up_failed_searches_and_explains_the_rate_limit():
    refused = world(refuse={("HKG", day, None) for day in MARCH})
    result, _ = run(refused)

    report = hunting.render(result, make_watch())

    assert "有 7 次搜索没有查到结果，上面的结果可能不全：" in report
    assert report.count("- skiplagged HKG+PVG+TFU→LAX") == 5
    assert "- 其余 2 次略" in report
    assert "Skiplagged 在限流（429）。等二三十分钟再查，不要连着做全面搜索。" in report


def test_report_marks_a_connection_the_source_did_not_name():
    connecting = FakeSkiplagged({"PR319": {"2027-03-12": 2171}, "PR318": {"2027-04-09": 2300}}, lambda *args: None, timetable={
        "PR319": ("HKG", "LAX", "13:30", "19:10"),
        "PR318": ("LAX", "HKG", "22:05", "10:05"),
    })
    one_stop = connecting._card

    def card_with_a_layover(number, *args):
        card = one_stop(number, *args)
        card["id"] = card["id"].replace(f"trip={number}", f"trip={number}-PR102")
        card["layovers"] = 1
        return card

    connecting._card = card_with_a_layover
    watch = make_watch(nonstop=False)
    result, _ = run(connecting, watch)

    report = hunting.render(result, watch)

    assert report.startswith("可转机商务舱往返")
    assert "| USD 4,471 | 两张单程 | PR319+PR102 HKG → ? → LAX 03-12 周五 13:30 | PR318+PR102 LAX → ? → HKG 04-09 周五 22:05 | skiplagged |" in report
