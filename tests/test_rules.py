import pytest

from farewatch import rules
from tests.helpers import make_watch, seg, trip

OUT = "2027-03-12T13:10:00"
BACK = "2027-04-09T11:30:00"


def test_trip_inside_every_limit_has_no_violations():
    assert rules.violations(trip(3500), make_watch()) == []


def test_price_line_is_strict_and_set_per_currency():
    watch = make_watch()  # USD 4000, CNY 28000

    assert rules.is_under_price_line(trip(3999.99), watch)
    assert not rules.is_under_price_line(trip(4000), watch)
    assert rules.is_under_price_line(trip(27999, currency="CNY"), watch)
    assert not rules.is_under_price_line(trip(28000, currency="CNY"), watch)


def test_offer_in_a_currency_without_a_price_line_is_never_a_hit():
    assert not rules.is_under_price_line(trip(1, currency="EUR"), make_watch())


@pytest.mark.parametrize(
    ("itinerary", "problem"),
    [
        (
            trip(3500, out=[seg("PR307", "HKG", "MNL", OUT), seg("PR102", "MNL", "LAX", OUT)]),
            "has a connection",
        ),
        (
            trip(3500, back=[seg("PR103", "LAX", "MNL", BACK), seg("PR300", "MNL", "HKG", BACK)]),
            "has a connection",
        ),
        (
            trip(3500, back=[seg("MU586", "LAX", "PVG", BACK, cabin="premium_economy")]),
            "has a segment in premium_economy",
        ),
        (trip(3500, out=[seg("MU583", "PVG", "LAX", "2027-03-09T13:10:00")]), "departs on 2027-03-09"),
        (trip(3500, out=[seg("MU583", "PVG", "LAX", "2027-03-17T00:05:00")]), "departs on 2027-03-17"),
        (trip(3500, back=[seg("MU586", "LAX", "PVG", "2027-04-14T11:30:00")]), "returns on 2027-04-14"),
        (trip(3500, out=[seg("CZ327", "CAN", "LAX", OUT)]), "leaves from CAN"),
        (trip(3500, out=[seg("MU589", "PVG", "SFO", OUT)]), "arrives at SFO"),
        (trip(3500, back=[seg("MU590", "SFO", "PVG", BACK)]), "returns from SFO"),
        (trip(3500, back=[seg("CZ328", "LAX", "CAN", BACK)]), "returns to CAN"),
    ],
)
def test_trips_outside_the_watch_are_rejected(itinerary, problem):
    assert rules.violations(itinerary, make_watch()) == [problem]


def test_first_and_last_day_of_each_window_are_inside():
    watch = make_watch()

    first = trip(3500, out=[seg("MU583", "PVG", "LAX", "2027-03-10T00:05:00")], back=[seg("MU586", "LAX", "PVG", "2027-04-07T00:05:00")])
    last = trip(3500, out=[seg("MU583", "PVG", "LAX", "2027-03-16T23:55:00")], back=[seg("MU586", "LAX", "PVG", "2027-04-13T23:55:00")])

    assert rules.violations(first, watch) == []
    assert rules.violations(last, watch) == []


def test_connections_are_fine_when_the_watch_allows_them():
    connecting = trip(3500, out=[seg("PR307", "HKG", "MNL", OUT), seg("PR102", "MNL", "LAX", OUT)])

    assert rules.violations(connecting, make_watch(nonstop=False)) == []


def test_returning_to_another_watched_airport_needs_open_jaw():
    open_jaw = trip(3500, back=[seg("CX881", "LAX", "HKG", BACK)])  # left from PVG

    assert rules.violations(open_jaw, make_watch()) == []
    assert rules.violations(open_jaw, make_watch(open_jaw=False)) == ["returns to HKG, not PVG"]


def test_evaluate_keeps_matching_trips_cheapest_first_and_marks_hits():
    cheap, dear, over_line = trip(3600), trip(3900), trip(4100)
    connecting = trip(2000, out=[seg("PR307", "HKG", "MNL", OUT), seg("PR102", "MNL", "LAX", OUT)])

    result = rules.evaluate([dear, connecting, over_line, cheap], make_watch())

    assert result.matching == (cheap, dear, over_line)
    assert result.hits == (cheap, dear)


def test_evaluate_with_nothing_matching():
    result = rules.evaluate([trip(3500)], make_watch(destinations=("SEA",)))

    assert result.matching == ()
    assert result.hits == ()
