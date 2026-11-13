from datetime import datetime, timedelta, timezone

from farewatch import alerts
from tests.helpers import make_watch, seg, trip

NOW = datetime(2026, 11, 13, 18, 40, tzinfo=timezone.utc)


def last_alert(price=3700.0, currency="USD", hours_ago=1.0):
    return {
        "price": price,
        "currency": currency,
        "flights": "MU583@2027-03-12 / MU586@2027-04-09",
        "alerted_at": (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
    }


# --- when to alert ---------------------------------------------------------------------


def test_the_first_hit_is_alerted():
    assert alerts.should_alert(trip(3700), None, NOW)
    assert alerts.should_alert(trip(3700), {}, NOW)


def test_the_same_fare_is_not_alerted_again_the_same_day():
    assert not alerts.should_alert(trip(3700), last_alert(), NOW)


def test_a_dip_of_less_than_one_percent_is_not_news():
    assert not alerts.should_alert(trip(3690), last_alert(), NOW)


def test_a_drop_of_more_than_one_percent_is_news():
    assert alerts.should_alert(trip(3650), last_alert(), NOW)


def test_a_rise_that_stays_under_the_line_is_not_news():
    assert not alerts.should_alert(trip(3900), last_alert(), NOW)


def test_a_fare_still_on_sale_is_repeated_after_a_day():
    assert not alerts.should_alert(trip(3700), last_alert(hours_ago=23.5), NOW)
    assert alerts.should_alert(trip(3700), last_alert(hours_ago=24), NOW)


def test_a_hit_in_another_currency_is_news():
    assert alerts.should_alert(trip(26000, currency="CNY"), last_alert(), NOW)


# --- what the alert says ---------------------------------------------------------------


def test_alert_gives_the_fare_the_flights_and_where_to_book():
    title, body = alerts.render(make_watch(), trip(3650), [], now=NOW)

    assert title == "[抢机票] china-lax: USD 3,650 PVG-LAX MU 商务舱"
    assert "**USD 3,650** · 往返 · 商务舱 · 直飞 · 1 人 · 价格线 USD 4,000" in body
    assert "| 去程 | MU583 | PVG → LAX | 2027-03-12 13:10 | 2027-03-12 13:10 |" in body
    assert "| 回程 | MU586 | LAX → PVG | 2027-04-09 11:30 | 2027-04-09 11:30 |" in body
    assert "- 报价来源：Kiwi.com，查询时间 2026-11-13 18:40 UTC" in body
    assert "- 下单链接：https://example.test/book" in body
    assert "@" not in body
    assert "其他低于价格线" not in body


def test_alert_mentions_the_owner_first_so_github_notifies_them():
    _, body = alerts.render(make_watch(), trip(3650), [], now=NOW, mention="octocat")

    assert body.startswith("@octocat 发现低于价格线的票。\n\n**USD 3,650**")


def test_alert_lists_the_other_hits():
    other = trip(
        26500,
        currency="CNY",
        out=[seg("CX880", "HKG", "LAX", "2027-03-11T00:30:00")],
        back=[seg("CX881", "LAX", "HKG", "2027-04-08T22:45:00")],
    )

    _, body = alerts.render(make_watch(), trip(3650), [other], now=NOW)

    assert "**这次扫描里其他低于价格线的选项**" in body
    assert "| CNY 26,500 | CX880 HKG → LAX 2027-03-11 | CX881 LAX → HKG 2027-04-08 |" in body


def test_alert_for_a_connecting_trip_on_two_airlines_shows_the_whole_route():
    connecting = trip(
        3100,
        out=[seg("PR307", "HKG", "MNL", "2027-03-12T19:05:00"), seg("PR102", "MNL", "LAX", "2027-03-12T22:15:00")],
        back=[seg("CX881", "LAX", "HKG", "2027-04-08T22:45:00")],
    )

    title, body = alerts.render(make_watch(nonstop=False), connecting, [], now=NOW)

    assert title == "[抢机票] china-lax: USD 3,100 HKG-LAX CX/PR 商务舱"
    assert "· 可转机 ·" in body
    assert "| 去程 | PR307+PR102 | HKG → MNL → LAX | 2027-03-12 19:05 | 2027-03-12 22:15 |" in body
