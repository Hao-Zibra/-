from datetime import date

from farewatch import cli
from farewatch import hunt as hunting
from farewatch.config import ConfigError, DateRange
from farewatch.hunt import Find, Hunt
from tests.helpers import trip

ARGS = ["hunt", "--from", "hkg,PVG", "--to", "LAX", "--depart", "2027-03-10:2027-03-16", "--return", "2027-04-07:2027-04-13"]


def stub_hunt(monkeypatch, result=None, error=None):
    """Replaces the search itself; returns the (watch, currency) pairs it was asked for."""
    asked = []

    def hunt(watch, currency):
        asked.append((watch, currency))
        if error:
            raise error
        return result

    monkeypatch.setattr(hunting, "hunt", hunt)
    return asked


def found(*prices):
    return Hunt(finds=tuple(Find(trip(price), ("https://example.test/book",)) for price in prices), searches=7, failures=())


def test_hunt_turns_its_options_into_a_watch_and_prints_the_report(monkeypatch, capsys):
    asked = stub_hunt(monkeypatch, found(3200, 5100))

    code = cli.main(ARGS)

    ((watch, currency),) = asked
    assert code == 0
    assert currency == "USD"
    assert (watch.origins, watch.destinations) == (("HKG", "PVG"), ("LAX",))
    assert watch.depart == DateRange(date(2027, 3, 10), date(2027, 3, 16))
    assert watch.ret == DateRange(date(2027, 4, 7), date(2027, 4, 13))
    assert (watch.cabin, watch.nonstop, watch.open_jaw, watch.adults) == ("business", True, True, 1)
    assert watch.alert_below == {}
    printed = capsys.readouterr().out
    assert "查了 7 次，找到 2 种走法，最低 USD 3,200。" in printed
    assert "| USD 5,100 | 往返 | MU583 PVG → LAX 03-12 周五 13:10 |" in printed


def test_hunt_options_change_the_watch(monkeypatch, capsys):
    asked = stub_hunt(monkeypatch, found(900))

    cli.main(
        ["hunt", "--from", "PVG", "--to", "LAX,SFO", "--depart", "2027-03-12", "--return", "2027-04-09",
         "--cabin", "economy", "--connections", "--same-airport", "--adults", "2", "--currency", "cny", "--top", "1"]
    )

    ((watch, currency),) = asked
    assert currency == "CNY"
    assert watch.destinations == ("LAX", "SFO")
    assert watch.depart == DateRange(date(2027, 3, 12), date(2027, 3, 12))  # a single day is a one-day window
    assert watch.ret == DateRange(date(2027, 4, 9), date(2027, 4, 9))
    assert (watch.cabin, watch.nonstop, watch.open_jaw, watch.adults) == ("economy", False, False, 2)
    assert capsys.readouterr().out.startswith("可转机经济舱往返 PVG → LAX/SFO，")


def test_hunt_rejects_options_that_make_no_trip(monkeypatch, capsys):
    asked = stub_hunt(monkeypatch, found(900))

    code = cli.main(["hunt", "--from", "Shanghai", "--to", "LAX", "--depart", "2027-03-12", "--return", "2027-04-09"])

    assert code == 2
    assert "'Shanghai' is not a 3-letter airport code" in capsys.readouterr().err
    assert asked == []


def test_hunt_reports_a_search_too_big_to_run(monkeypatch, capsys):
    stub_hunt(monkeypatch, error=ConfigError("this hunt could take 184 searches and the limit is 120"))

    assert cli.main(ARGS) == 2
    assert "could take 184 searches" in capsys.readouterr().err


def test_hunt_fails_only_when_nothing_was_found_because_searches_failed(monkeypatch, capsys):
    stub_hunt(monkeypatch, Hunt(finds=(), searches=3, failures=("skiplagged: could not be reached",)))
    assert cli.main(ARGS) == 1
    assert "有 1 次搜索没有查到结果" in capsys.readouterr().out

    stub_hunt(monkeypatch, Hunt(finds=(), searches=30, failures=()))
    assert cli.main(ARGS) == 0  # everything answered: there just is no such flight

    partly = found(3200)
    stub_hunt(monkeypatch, Hunt(finds=partly.finds, searches=30, failures=("skiplagged HKG→LAX: refused",)))
    assert cli.main(ARGS) == 0
