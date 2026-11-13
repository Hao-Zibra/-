import json
from datetime import date
from pathlib import Path

import pytest

from farewatch import skiplagged
from farewatch.model import SourceError
from tests.helpers import make_watch

FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def card(**overrides):
    one = {
        "id": "HKG-LAX-2027-03-12-trip=UA821",
        "departure": {"airport": "HKG", "dateTime": "2027-03-12T22:45:00+08:00"},
        "arrival": {"airport": "LAX", "dateTime": "2027-03-12T19:30:00-08:00"},
        "layovers": 0,
        "price": {"amount": 3532, "currency": "USD"},
        "deepLink": "https://skiplagged.test/trip",
    }
    one.update(overrides)
    return one


# --- building searches -----------------------------------------------------------------


def test_one_way_search_names_up_to_three_airports_each_side():
    query = skiplagged.one_way_query(("HKG", "PVG", "PEK"), ("LAX", "SFO"), date(2027, 3, 12), make_watch(), "USD")

    assert query == {
        "origin": "HKG",
        "additionalOrigins": ["PVG", "PEK"],
        "destination": "LAX",
        "additionalDestinations": ["SFO"],
        "departureDate": "2027-03-12",
        "fareClass": "business",
        "adults": 1,
        "currency": "USD",
        "sort": "price",
        "limit": 100,
        "includeHiddenCity": False,
        "includeVirtualInterlining": False,
        "maxStops": "none",
    }


def test_one_way_search_for_a_single_airport_pair_has_no_additional_airports():
    query = skiplagged.one_way_query(("LAX",), ("HKG",), date(2027, 4, 9), make_watch(), "USD")

    assert (query["origin"], query["destination"]) == ("LAX", "HKG")
    assert "additionalOrigins" not in query and "additionalDestinations" not in query


def test_round_trip_search_names_both_days():
    query = skiplagged.round_trip_query("HKG", "LAX", date(2027, 3, 12), date(2027, 4, 9), make_watch(), "CNY")

    assert (query["origin"], query["destination"]) == ("HKG", "LAX")
    assert (query["departureDate"], query["returnDate"], query["currency"]) == ("2027-03-12", "2027-04-09", "CNY")


def test_connections_are_only_ruled_out_for_a_nonstop_watch():
    watch = make_watch(nonstop=False, cabin="premium_economy", adults=2)

    query = skiplagged.one_way_query(("HKG",), ("LAX",), date(2027, 3, 12), watch, "USD")

    assert "maxStops" not in query
    assert (query["fareClass"], query["adults"]) == ("premium", 2)


# --- reading answers -------------------------------------------------------------------


def test_reads_a_real_one_way_answer():
    offers = skiplagged.parse_offers(fixture("skiplagged_one_way.json"), "business")

    assert len(offers) == 11
    cheapest = offers[0]
    assert (cheapest.price, cheapest.currency) == (3532.0, "USD")
    assert cheapest.url.startswith("https://skiplagged.com/flights/HKG,PVG,PEK/LAX/2027-03-12?")
    assert cheapest.url.endswith("#trip=UA821")
    leg = cheapest.leg
    assert (leg.flights, leg.origin, leg.destination, leg.stops) == ("UA821", "HKG", "LAX", 0)
    assert (leg.departure, leg.arrival) == ("2027-03-12T22:45:00", "2027-03-12T19:30:00")  # local times
    assert (leg.segments[0].carrier, leg.segments[0].cabin) == ("UA", "business")
    assert {offer.leg.origin for offer in offers} == {"HKG", "PVG"}  # one search, several origins


def test_a_round_trip_answer_gives_each_outbound_flight_its_round_trip_total():
    offers = skiplagged.parse_offers(fixture("skiplagged_round_trip.json"), "business")

    assert [(offer.leg.flights, offer.price) for offer in offers[:3]] == [
        ("UA821", 5349.0),
        ("UA153", 5533.0),
        ("CA153", 5781.0),
    ]
    # The card's id reads "trip=UA821,DL87", but DL87 is only the cheapest one-way back
    # that day, so the return flight is deliberately not read.
    assert all(offer.leg.destination == "LAX" for offer in offers)


def test_a_connecting_flight_keeps_its_flight_numbers_but_not_where_it_connects():
    (first, *_) = skiplagged.parse_offers(fixture("skiplagged_one_way_connecting.json"), "business")

    leg = first.leg
    assert (leg.flights, leg.stops) == ("PR319+PR102", 1)
    assert (leg.origin, leg.destination) == ("HKG", "LAX")
    assert (leg.departure, leg.arrival) == ("2027-03-12T13:30:00", "2027-03-12T19:10:00")
    assert leg.segments[0].destination == skiplagged.UNKNOWN == leg.segments[1].origin


def test_no_flights_is_a_valid_answer():
    assert skiplagged.parse_offers({"flights": [], "searchUrl": "https://skiplagged.test"}, "business") == []


def test_a_card_without_its_own_link_uses_the_search_link():
    (offer,) = skiplagged.parse_offers({"flights": [card(deepLink=None)], "searchUrl": "https://skiplagged.test/search"}, "business")

    assert offer.url == "https://skiplagged.test/search"


def test_an_answer_in_an_unknown_shape_is_raised():
    with pytest.raises(SourceError, match="no 'flights' list"):
        skiplagged.parse_offers({"results": []}, "business")


@pytest.mark.parametrize(
    "broken",
    [
        card(id="HKG-LAX-2027-03-12"),  # no flight numbers
        card(layovers=1),  # one flight number cannot have a layover
        card(price={"currency": "USD"}),
        card(departure={"airport": "HKG", "dateTime": "soon"}),
        "not a card",
    ],
)
def test_cards_that_cannot_be_read_are_skipped_or_raised(broken):
    good = card()

    assert [offer.leg.flights for offer in skiplagged.parse_offers({"flights": [broken, good]}, "business")] == ["UA821"]
    with pytest.raises(SourceError, match="format may have changed"):
        skiplagged.parse_offers({"flights": [broken]}, "business")


# --- the rate limit --------------------------------------------------------------------

REFUSED = SourceError("Skiplagged rejected the search: Failed to fetch from search: Request failed with status code 429")


def stub_endpoint(monkeypatch, batches):
    """Makes each run against the endpoint return the next item of `batches`."""
    runs = []

    async def call_many(url, tool, argument_sets, *, who, concurrency):
        runs.append((list(argument_sets), concurrency))
        return list(batches[len(runs) - 1])

    waits = []
    monkeypatch.setattr(skiplagged.mcptool, "call_many", call_many)
    monkeypatch.setattr(skiplagged.time, "sleep", waits.append)
    return runs, waits


def test_searches_run_in_one_go_when_none_is_refused(monkeypatch):
    runs, waits = stub_endpoint(monkeypatch, [[{"flights": []}, {"flights": []}]])

    answers = skiplagged.search_many([{"origin": "HKG"}, {"origin": "PVG"}])

    assert answers == [{"flights": []}, {"flights": []}]
    assert runs == [([{"origin": "HKG"}, {"origin": "PVG"}], 6)]
    assert waits == []


def test_refused_searches_are_tried_once_more_slowly_after_a_pause(monkeypatch):
    other_error = SourceError("Skiplagged search failed: TimeoutError")
    runs, waits = stub_endpoint(
        monkeypatch,
        [[{"n": 0}, REFUSED, other_error, REFUSED], [{"n": 1}, REFUSED]],
    )

    answers = skiplagged.search_many([{"q": 0}, {"q": 1}, {"q": 2}, {"q": 3}])

    assert answers == [{"n": 0}, {"n": 1}, other_error, REFUSED]
    assert runs[1] == ([{"q": 1}, {"q": 3}], 2)  # only the refused ones, two at a time
    assert waits == [30.0]


def test_nothing_is_retried_when_every_search_is_refused(monkeypatch):
    runs, waits = stub_endpoint(monkeypatch, [[REFUSED, REFUSED]])

    assert skiplagged.search_many([{"q": 0}, {"q": 1}]) == [REFUSED, REFUSED]
    assert len(runs) == 1 and waits == []


def test_no_searches_means_no_connection(monkeypatch):
    runs, _ = stub_endpoint(monkeypatch, [])

    assert skiplagged.search_many([]) == []
    assert runs == []
