import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from farewatch import kiwi
from farewatch.model import SourceError
from tests.helpers import make_watch

FIXTURE = Path(__file__).parent / "fixtures" / "kiwi_search_flight.json"


def raw_segment(flight="MU583", origin="PVG", destination="LAX", departs="2027-03-12T13:10:00", cabin="Business"):
    return {
        "from": origin,
        "to": destination,
        "departureTime": departs,
        "arrivalTime": departs,
        "carrier": flight[:2],
        "flightNumber": flight,
        "cabinClass": cabin,
    }


def raw_itinerary(price=3500.0, **overrides):
    itinerary = {
        "tripType": "return",
        "price": price,
        "bookingUrl": "https://kiwi.com/u/example",
        "outbound": {"segments": [raw_segment()]},
        "inbound": {"segments": [raw_segment("MU586", "LAX", "PVG", "2027-04-09T11:30:00")]},
    }
    itinerary.update(overrides)
    return itinerary


def response(*itineraries, currency="USD", error=None):
    return {"currency": currency, "itineraries": list(itineraries), "error": error}


def tool_result(text="", *, structured=None, is_error=False):
    return SimpleNamespace(content=[SimpleNamespace(text=text)], structured_content=structured, is_error=is_error)


# --- building the search ---------------------------------------------------------------


def test_arguments_cover_the_whole_watch_in_one_search():
    assert kiwi.build_arguments(make_watch(), "CNY") == [
        {
            "flyFrom": "HKG,PVG,TFU",
            "flyTo": "LAX",
            "departureDate": "10/03/2027",
            "departureDateTo": "16/03/2027",
            "returnDate": "07/04/2027",
            "returnDateTo": "13/04/2027",
            "adults": 1,
            "cabinClass": "C",
            "currency": "CNY",
            "locale": "en",
            "sort": "price",
            "allow_self_transfer": False,
            "max_sector_stopovers": 0,
        }
    ]


def test_connections_are_not_ruled_out_unless_the_watch_is_nonstop():
    (arguments,) = kiwi.build_arguments(make_watch(nonstop=False, cabin="premium_economy", adults=2), "USD")

    assert "max_sector_stopovers" not in arguments
    assert (arguments["cabinClass"], arguments["adults"]) == ("W", 2)


def test_more_airports_than_fit_in_one_request_are_split():
    origins = tuple(f"A{a}{b}" for a in "ABCDEF" for b in "ABCDE")  # 30 codes: 119 characters joined

    searches = kiwi.build_arguments(make_watch(origins=origins), "USD")

    assert len(searches) == 2
    assert all(len(search["flyFrom"]) <= kiwi.MAX_LOCATIONS_LENGTH for search in searches)
    assert ",".join(search["flyFrom"] for search in searches).split(",") == list(origins)


# --- reading the answer ----------------------------------------------------------------


def test_reads_a_real_response():
    (itinerary,) = kiwi.parse_payload(json.loads(FIXTURE.read_text(encoding="utf-8")))

    assert (itinerary.source, itinerary.price, itinerary.currency) == ("kiwi", 3736.0, "USD")
    assert itinerary.booking_url == "https://kiwi.com/u/example"
    assert itinerary.carriers == ("PR",)
    assert (itinerary.outbound.origin, itinerary.outbound.destination) == ("HKG", "LAX")
    assert (itinerary.outbound.flights, itinerary.outbound.stops) == ("PR307+PR102", 1)
    assert (itinerary.inbound.flights, itinerary.inbound.destination) == ("PR103+PR300", "HKG")
    assert str(itinerary.outbound.departure_date) == "2027-02-08"
    assert {segment.cabin for leg in itinerary.legs for segment in leg.segments} == {"business"}
    assert itinerary.key == "PR307@2027-02-08+PR102@2027-02-08 / PR103@2027-02-22+PR300@2027-02-24"


def test_one_way_offers_are_dropped():
    one_way = raw_itinerary(tripType="oneway", inbound=None)

    assert kiwi.parse_payload(response(one_way)) == []


def test_flight_numbers_always_carry_the_airline_code():
    bare = {**raw_segment(), "flightNumber": "583"}

    (itinerary,) = kiwi.parse_payload(response(raw_itinerary(outbound={"segments": [bare]})))

    assert itinerary.outbound.flights == "MU583"


def test_cabin_names_are_normalised():
    premium = raw_segment(cabin="Premium Economy")

    (itinerary,) = kiwi.parse_payload(response(raw_itinerary(outbound={"segments": [premium]})))

    assert itinerary.outbound.segments[0].cabin == "premium_economy"


def test_an_error_reported_by_kiwi_is_raised():
    with pytest.raises(SourceError, match="rate limited"):
        kiwi.parse_payload(response(error="rate limited"))


@pytest.mark.parametrize("payload", [{}, {"currency": "USD"}, {"itineraries": []}, {"currency": "USD", "itineraries": "none"}])
def test_a_response_in_an_unknown_shape_is_raised(payload):
    with pytest.raises(SourceError, match="missing"):
        kiwi.parse_payload(payload)


def test_no_offers_is_a_valid_answer():
    assert kiwi.parse_payload(response()) == []


def test_unreadable_itineraries_are_skipped_while_others_can_be_read():
    broken = raw_itinerary(price="n/a")

    (itinerary,) = kiwi.parse_payload(response(broken, raw_itinerary(price=3600.0)))

    assert itinerary.price == 3600.0


def test_a_response_where_nothing_can_be_read_is_raised():
    without_segments = raw_itinerary(outbound={"route": ["PVG", "LAX"]})

    with pytest.raises(SourceError, match="format may have changed"):
        kiwi.parse_payload(response(without_segments, "not even a mapping"))


def test_tool_result_prefers_structured_content():
    assert kiwi._payload(tool_result("ignored", structured={"currency": "USD"})) == {"currency": "USD"}


def test_tool_result_falls_back_to_json_text():
    assert kiwi._payload(tool_result('{"currency": "USD"}')) == {"currency": "USD"}


@pytest.mark.parametrize(
    ("result", "message"),
    [
        (tool_result("departureDate must be in the future", is_error=True), "rejected the search: departureDate"),
        (tool_result("<html>busy</html>"), "not JSON"),
        (tool_result("[1, 2]"), "not a JSON object"),
    ],
)
def test_tool_results_that_are_not_an_answer_are_raised(result, message):
    with pytest.raises(SourceError, match=message):
        kiwi._payload(result)


# --- retries ---------------------------------------------------------------------------


def stub_search(monkeypatch, outcomes):
    """Makes each call to Kiwi return (or raise) the next item of `outcomes`."""
    calls = []

    async def search(argument_sets, *, url, pause):
        calls.append(argument_sets)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(kiwi, "_search", search)
    monkeypatch.setattr(kiwi.time, "sleep", lambda seconds: None)
    return calls


def test_fetch_retries_a_failed_search(monkeypatch):
    calls = stub_search(monkeypatch, [ConnectionError("reset"), [response(raw_itinerary(price=3600.0))]])

    (itinerary,) = kiwi.fetch(make_watch(), "USD")

    assert itinerary.price == 3600.0
    assert len(calls) == 2


def test_fetch_gives_up_after_the_last_attempt(monkeypatch):
    nested = ExceptionGroup("task group", [ExceptionGroup("inner", [TimeoutError("no answer")])])
    calls = stub_search(monkeypatch, [ConnectionError("reset"), ConnectionError("reset"), nested])

    with pytest.raises(SourceError, match="after 3 attempts: TimeoutError: no answer"):
        kiwi.fetch(make_watch(), "USD")

    assert len(calls) == 3
