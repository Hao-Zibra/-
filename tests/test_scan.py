"""The whole scan: sources in, history, state and alerts out."""

import io
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from farewatch import cli, store
from farewatch.model import SourceError
from tests.helpers import make_watch, seg, trip

NOW = datetime(2026, 11, 13, 18, 40, tzinfo=timezone.utc)


class FakeIssues:
    def __init__(self):
        self.created = []  # (title, body)
        self.comments = []  # (number, body)
        self.closed = set()
        self.down = False

    def is_open(self, number):
        return number not in self.closed

    def create(self, title, body):
        if self.down:
            raise httpx.ConnectError("api.github.com unreachable")
        self.created.append((title, body))
        return len(self.created)

    def comment(self, number, body):
        self.comments.append((number, body))


def run_scan(data_dir, offers, *, issues=None, now=NOW, watch=None, mention=None):
    """Scans one watch against a fake source. `offers` maps currency -> itineraries or an error to raise."""

    def fetch(watch, currency):
        found = offers.get(currency, [])
        if isinstance(found, Exception):
            raise found
        return found

    out = io.StringIO()
    code = cli.scan(
        [watch or make_watch()], data_dir, issues=issues, mention=mention, sources={"fake": fetch}, now=now, out=out
    )
    return code, out.getvalue()


def history(data_dir):
    return [json.loads(line) for line in (data_dir / store.HISTORY_FILE).read_text(encoding="utf-8").splitlines()]


def test_fares_over_the_line_are_recorded_but_not_alerted(tmp_path):
    issues = FakeIssues()

    code, printed = run_scan(tmp_path, {"USD": [trip(7098), trip(7720)], "CNY": [trip(47526, currency="CNY")]}, issues=issues)

    assert code == 0
    assert issues.created == [] and issues.comments == []
    assert store.load_state(tmp_path) == {"china-lax": {}}
    usd, cny = history(tmp_path)
    assert usd == {
        "at": "2026-11-13T18:40:00+00:00",
        "watch": "china-lax",
        "source": "fake",
        "currency": "USD",
        "offers": 2,
        "matching": 2,
        "hits": 0,
        "cheapest": [
            {
                "price": 7098,
                "carriers": ["MU"],
                "out": {"from": "PVG", "to": "LAX", "departs": "2027-03-12T13:10:00", "flights": "MU583"},
                "in": {"from": "LAX", "to": "PVG", "departs": "2027-04-09T11:30:00", "flights": "MU586"},
            },
            {
                "price": 7720,
                "carriers": ["MU"],
                "out": {"from": "PVG", "to": "LAX", "departs": "2027-03-12T13:10:00", "flights": "MU583"},
                "in": {"from": "LAX", "to": "PVG", "departs": "2027-04-09T11:30:00", "flights": "MU586"},
            },
        ],
    }
    assert (cny["currency"], cny["hits"], cny["cheapest"][0]["price"]) == ("CNY", 0, 47526)
    assert "china-lax [fake USD]: 2 个报价，2 个符合条件，最低 USD 7,098" in printed
    assert "价格线 USD 4,000，未命中" in printed


def test_a_fare_under_the_line_opens_an_issue(tmp_path):
    issues = FakeIssues()

    code, printed = run_scan(tmp_path, {"USD": [trip(3650), trip(7000)]}, issues=issues, mention="octocat")

    assert code == 0
    ((title, body),) = issues.created
    assert title == "[抢机票] china-lax: USD 3,650 PVG-LAX MU 商务舱"
    assert body.startswith("@octocat ")
    assert store.load_state(tmp_path) == {
        "china-lax": {
            "issue": 1,
            "last": {
                "price": 3650,
                "currency": "USD",
                "flights": "MU583@2027-03-12 / MU586@2027-04-09",
                "alerted_at": "2026-11-13T18:40:00+00:00",
            },
        }
    }
    assert "命中 1 个" in printed
    assert "[抢机票]" not in printed  # delivered as an issue, not printed


def test_the_same_fare_is_not_alerted_on_the_next_scan(tmp_path):
    issues = FakeIssues()
    run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues)

    run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues, now=NOW + timedelta(minutes=30))

    assert len(issues.created) == 1 and issues.comments == []


def test_a_lower_fare_is_added_to_the_open_issue(tmp_path):
    issues = FakeIssues()
    run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues)

    run_scan(tmp_path, {"USD": [trip(3400)]}, issues=issues, now=NOW + timedelta(minutes=30))

    assert len(issues.created) == 1
    ((number, body),) = issues.comments
    assert number == 1
    assert body.startswith("## [抢机票] china-lax: USD 3,400 ")
    assert store.load_state(tmp_path)["china-lax"]["last"]["price"] == 3400


def test_a_new_issue_is_opened_once_the_old_one_is_closed(tmp_path):
    issues = FakeIssues()
    run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues)
    issues.closed.add(1)

    run_scan(tmp_path, {"USD": [trip(3400)]}, issues=issues, now=NOW + timedelta(minutes=30))

    assert len(issues.created) == 2 and issues.comments == []
    assert store.load_state(tmp_path)["china-lax"]["issue"] == 2


def test_a_fare_that_disappears_and_comes_back_is_alerted_again(tmp_path):
    issues = FakeIssues()
    run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues)

    run_scan(tmp_path, {"USD": [trip(7000)]}, issues=issues, now=NOW + timedelta(minutes=30))
    assert store.load_state(tmp_path) == {"china-lax": {"issue": 1}}

    run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues, now=NOW + timedelta(minutes=60))
    assert len(issues.comments) == 1


def test_the_best_hit_is_the_one_furthest_under_its_own_price_line(tmp_path):
    issues = FakeIssues()
    usd = trip(3900)  # 97.5% of the USD 4,000 line
    cny = trip(  # 92.9% of the CNY 28,000 line
        26000,
        currency="CNY",
        out=[seg("CX880", "HKG", "LAX", "2027-03-11T00:30:00")],
        back=[seg("CX881", "LAX", "HKG", "2027-04-08T22:45:00")],
    )

    run_scan(tmp_path, {"USD": [usd], "CNY": [cny]}, issues=issues)

    ((title, body),) = issues.created
    assert title == "[抢机票] china-lax: CNY 26,000 HKG-LAX CX 商务舱"
    assert "| USD 3,900 | MU583 PVG → LAX 2027-03-12 | MU586 LAX → PVG 2027-04-09 |" in body


def test_offers_that_break_the_watch_never_alert(tmp_path):
    issues = FakeIssues()
    connecting = trip(2000, out=[seg("PR307", "HKG", "MNL", "2027-03-12T19:05:00"), seg("PR102", "MNL", "LAX", "2027-03-12T22:15:00")])

    code, printed = run_scan(tmp_path, {"USD": [connecting]}, issues=issues)

    assert code == 0 and issues.created == []
    assert "1 个报价，0 个符合条件" in printed
    assert history(tmp_path)[0]["cheapest"] == []


def test_a_source_failure_fails_the_run_and_keeps_what_was_alerted(tmp_path):
    issues = FakeIssues()
    run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues)
    before = store.load_state(tmp_path)

    code, _ = run_scan(
        tmp_path,
        {"USD": SourceError("Kiwi search failed after 3 attempts"), "CNY": []},
        issues=issues,
        now=NOW + timedelta(minutes=30),
    )

    assert code == 1
    assert store.load_state(tmp_path) == before  # not treated as "the fare is gone"
    assert history(tmp_path)[-2] == {
        "at": "2026-11-13T19:10:00+00:00",
        "watch": "china-lax",
        "source": "fake",
        "currency": "USD",
        "error": "Kiwi search failed after 3 attempts",
    }


def test_an_alert_that_cannot_be_delivered_is_printed_and_retried(tmp_path):
    issues = FakeIssues()
    issues.down = True

    code, printed = run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues)

    assert code == 1
    assert "# [抢机票] china-lax: USD 3,650 PVG-LAX MU 商务舱" in printed
    assert store.load_state(tmp_path) == {"china-lax": {}}

    issues.down = False
    code, _ = run_scan(tmp_path, {"USD": [trip(3650)]}, issues=issues, now=NOW + timedelta(minutes=30))
    assert code == 0 and len(issues.created) == 1


def test_without_an_issue_tracker_the_alert_is_printed(tmp_path):
    code, printed = run_scan(tmp_path, {"USD": [trip(3650)]})

    assert code == 0
    assert "# [抢机票] china-lax: USD 3,650 PVG-LAX MU 商务舱" in printed
    assert "下单链接：https://example.test/book" in printed
    assert "issue" not in store.load_state(tmp_path)["china-lax"]
    assert store.load_state(tmp_path)["china-lax"]["last"]["price"] == 3650


# --- command line ----------------------------------------------------------------------

WATCHLIST = """
watches:
  - name: china-lax
    origins: [PVG]
    destinations: [LAX]
    depart: {from: 2027-03-10, to: 2027-03-16}
    return: {from: 2027-04-07, to: 2027-04-13}
    alert_below: {USD: 4000}
"""


def test_command_line_scans_the_watchlist(tmp_path, monkeypatch, capsys):
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(WATCHLIST, encoding="utf-8")
    asked = []

    def fetch(watch, currency):
        asked.append((watch.name, currency))
        return [trip(3650)]

    monkeypatch.setitem(cli.SOURCES, "kiwi", fetch)

    code = cli.main(["scan", "--watchlist", str(watchlist), "--data-dir", str(tmp_path / "data")])

    assert code == 0
    assert asked == [("china-lax", "USD")]
    assert "# [抢机票] china-lax: USD 3,650 PVG-LAX MU 商务舱" in capsys.readouterr().out
    assert history(tmp_path / "data")[0]["hits"] == 1


def test_command_line_reports_a_missing_watchlist(tmp_path, capsys):
    code = cli.main(["scan", "--watchlist", str(tmp_path / "nope.yaml"), "--data-dir", str(tmp_path / "data")])

    assert code == 2
    assert "watchlist file not found" in capsys.readouterr().err
    assert not (tmp_path / "data").exists()


def test_command_line_refuses_github_issues_outside_actions(tmp_path, monkeypatch, capsys):
    watchlist = tmp_path / "watchlist.yaml"
    watchlist.write_text(WATCHLIST, encoding="utf-8")
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)

    code = cli.main(["scan", "--watchlist", str(watchlist), "--data-dir", str(tmp_path / "data"), "--github-issues"])

    assert code == 2
    assert "GITHUB_REPOSITORY and GITHUB_TOKEN" in capsys.readouterr().err


def test_command_line_needs_a_command():
    with pytest.raises(SystemExit) as exit_info:
        cli.main([])

    assert exit_info.value.code == 2
