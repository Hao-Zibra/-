"""When to raise an alert for a fare under the price line, and what the alert says."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from farewatch.config import Watch
from farewatch.model import Itinerary, Leg

# A new alert needs the price to fall at least this far below the last alerted price...
MIN_DROP = 0.01
# ...unless the last alert is older than this, in which case a fare still on sale is repeated.
REPEAT_AFTER = timedelta(hours=24)

CABIN_NAMES = {
    "economy": "经济舱",
    "premium_economy": "超级经济舱",
    "business": "商务舱",
    "first": "头等舱",
}
SOURCE_NAMES = {"kiwi": "Kiwi.com"}


def should_alert(best: Itinerary, last: Mapping[str, Any] | None, now: datetime) -> bool:
    """Whether `best` is news compared with the last alert sent for the same watch."""
    if not last or last.get("currency") != best.currency:
        return True
    if best.price <= last["price"] * (1 - MIN_DROP):
        return True
    return now - datetime.fromisoformat(last["alerted_at"]) >= REPEAT_AFTER


def render(
    watch: Watch,
    best: Itinerary,
    others: Sequence[Itinerary],
    *,
    now: datetime,
    mention: str | None = None,
) -> tuple[str, str]:
    """Title and Markdown body of the alert for `best`; `others` are further hits to list."""
    cabin = CABIN_NAMES[watch.cabin]
    source = SOURCE_NAMES.get(best.source, best.source)
    title = (
        f"[抢机票] {watch.name}: {money(best.price, best.currency)} "
        f"{best.outbound.origin}-{best.outbound.destination} {'/'.join(best.carriers)} {cabin}"
    )

    lines = []
    if mention:
        lines += [f"@{mention} 发现低于价格线的票。", ""]
    lines += [
        f"**{money(best.price, best.currency)}** · 往返 · {cabin} · "
        f"{'直飞' if watch.nonstop else '可转机'} · {watch.adults} 人 · "
        f"价格线 {money(watch.alert_below[best.currency], best.currency)}",
        "",
        "| | 航班 | 航线 | 起飞（当地时间） | 到达（当地时间） |",
        "|---|---|---|---|---|",
        _leg_row("去程", best.outbound),
        _leg_row("回程", best.inbound),
        "",
        f"- 报价来源：{source}，查询时间 {now:%Y-%m-%d %H:%M} UTC",
        f"- 下单链接：{best.booking_url or '（来源没有给链接）'}",
        "",
        "**下单前**",
        "",
        f"1. 去航司官网、携程和 Expedia 搜同一组航班，哪边便宜在哪边买。{source} 的报价经常和航司直销价不一样。",
        "2. 直接向航司买、且离起飞还有 7 天以上时，美国 DOT 规定 24 小时内可以免费取消。"
        "先买下来就等于锁价，之后出现更低的再退。",
    ]
    if others:
        lines += [
            "",
            "**这次扫描里其他低于价格线的选项**",
            "",
            "| 价格 | 去程 | 回程 |",
            "|---|---|---|",
        ]
        lines += [
            f"| {money(it.price, it.currency)} | {_leg_summary(it.outbound)} | {_leg_summary(it.inbound)} |"
            for it in others
        ]
    return title, "\n".join(lines) + "\n"


def money(amount: float, currency: str) -> str:
    return f"{currency} {amount:,.0f}"


def _leg_row(label: str, leg: Leg) -> str:
    return f"| {label} | {leg.flights} | {route(leg)} | {_time(leg.departure)} | {_time(leg.arrival)} |"


def _leg_summary(leg: Leg) -> str:
    return f"{leg.flights} {route(leg)} {leg.departure[:10]}"


def route(leg: Leg) -> str:
    """'HKG → MNL → LAX'; a connection the source did not name shows as '?'."""
    return " → ".join(airport or "?" for airport in [leg.origin, *(segment.destination for segment in leg.segments)])


def _time(timestamp: str) -> str:
    return timestamp[:16].replace("T", " ")
