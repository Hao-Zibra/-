from datetime import date
from pathlib import Path

import pytest

from farewatch.config import ConfigError, DateRange, load_watchlist

VALID = """
watches:
  - name: china-lax
    origins: [hkg, PVG, PVG]
    destinations: [LAX]
    depart: {from: 2027-03-10, to: 2027-03-16}
    return: {from: "2027-04-07", to: 2027-04-13}
    alert_below: {usd: 4000, CNY: 28000.5}
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "watchlist.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_a_watch_with_defaults(tmp_path):
    (watch,) = load_watchlist(write(tmp_path, VALID))

    assert watch.name == "china-lax"
    assert watch.origins == ("HKG", "PVG")  # upper-cased, duplicates dropped
    assert watch.destinations == ("LAX",)
    assert watch.depart == DateRange(date(2027, 3, 10), date(2027, 3, 16))
    assert watch.ret == DateRange(date(2027, 4, 7), date(2027, 4, 13))
    assert watch.alert_below == {"USD": 4000.0, "CNY": 28000.5}
    assert (watch.cabin, watch.nonstop, watch.adults, watch.open_jaw) == ("business", True, 1, True)


def test_date_range_includes_both_ends():
    window = DateRange(date(2027, 3, 10), date(2027, 3, 16))

    assert date(2027, 3, 10) in window
    assert date(2027, 3, 16) in window
    assert date(2027, 3, 9) not in window
    assert date(2027, 3, 17) not in window


def test_the_shipped_examples_load():
    examples = Path(__file__).parent.parent / "examples"

    assert load_watchlist(examples / "watchlist.yaml")
    assert load_watchlist(examples / "smoke.yaml")


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_watchlist(tmp_path / "nope.yaml")


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("origins: [hkg, PVG, PVG]", "origins: []"), "origins must be a non-empty list"),
        (("origins: [hkg, PVG, PVG]", "origins: [Shanghai]"), "not a 3-letter airport code"),
        (("to: 2027-03-16", "to: 2027-03-01"), "is before 'from'"),
        (("from: 2027-03-10", "from: next week"), "is not a date"),
        (('return: {from: "2027-04-07", to: 2027-04-13}', "return: {from: 2027-02-01, to: 2027-02-02}"), "return window ends before"),
        (("alert_below: {usd: 4000, CNY: 28000.5}", "alert_below: {}"), "at least one price line"),
        (("alert_below: {usd: 4000, CNY: 28000.5}", "alert_below: {USD: -1}"), "positive number"),
        (("alert_below: {usd: 4000, CNY: 28000.5}", "alert_below: {dollars: 4000}"), "3-letter currency code"),
        (("    alert_below", "    cabin: suite\n    alert_below"), "cabin must be one of"),
        (("    alert_below", "    adults: 0\n    alert_below"), "adults must be a whole number"),
        (("    alert_below", "    nonstop: maybe\n    alert_below"), "nonstop must be true or false"),
        (("  - name: china-lax", "  - name: ''"), "needs a 'name'"),
    ],
)
def test_impossible_watches_are_rejected(tmp_path, change, message):
    old, new = change
    assert old in VALID

    with pytest.raises(ConfigError, match=message):
        load_watchlist(write(tmp_path, VALID.replace(old, new)))


def test_watch_names_must_be_unique(tmp_path):
    doubled = VALID + VALID.split("watches:\n", 1)[1]

    with pytest.raises(ConfigError, match="unique"):
        load_watchlist(write(tmp_path, doubled))


@pytest.mark.parametrize("text", ["", "watches: []", "- just a list", "watches: [oops", "watches: [not-a-mapping]"])
def test_files_without_usable_watches_are_rejected(tmp_path, text):
    with pytest.raises(ConfigError):
        load_watchlist(write(tmp_path, text))
