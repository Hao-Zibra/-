"""Command line entry point: `python -m farewatch scan`."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

import httpx

from farewatch import alerts, github, kiwi, rules, store
from farewatch import hunt as hunting
from farewatch.config import CABINS, ConfigError, Watch, load_watchlist, parse_watch
from farewatch.model import Itinerary, Leg, SourceError

log = logging.getLogger("farewatch")

Fetch = Callable[[Watch, str], list[Itinerary]]

# Every source is asked for every watch in every currency that has a price line.
SOURCES: Mapping[str, Fetch] = {kiwi.SOURCE: kiwi.fetch}

HISTORY_OFFERS = 5  # cheapest matching offers kept in each history record
OTHER_HITS = 5  # further hits listed under the best one in an alert


class Issues(Protocol):
    def is_open(self, number: int) -> bool: ...
    def create(self, title: str, body: str) -> int: ...
    def comment(self, number: int, body: str) -> None: ...


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="farewatch", description="Flight price watcher.")
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser(
        "scan", help="check every watch once and alert on fares under the price line"
    )
    scan_parser.add_argument("--watchlist", default="watchlist.yaml", help="watchlist file (default: %(default)s)")
    scan_parser.add_argument(
        "--data-dir", default="data", help="where history.jsonl and state.json are kept (default: %(default)s)"
    )
    scan_parser.add_argument(
        "--github-issues",
        action="store_true",
        help="deliver alerts as issues on $GITHUB_REPOSITORY instead of printing them",
    )
    hunt_parser = commands.add_parser(
        "hunt", help="search every source once, thoroughly, for the cheapest ways to fly a trip"
    )
    hunt_parser.add_argument(
        "--from", dest="origins", required=True, metavar="AIRPORTS", help="departure airports, e.g. HKG,PVG"
    )
    hunt_parser.add_argument(
        "--to", dest="destinations", required=True, metavar="AIRPORTS", help="arrival airports, e.g. LAX,SFO"
    )
    hunt_parser.add_argument(
        "--depart", required=True, metavar="DAY[:DAY]", help="day or window to fly out, e.g. 2027-03-10:2027-03-16"
    )
    hunt_parser.add_argument(
        "--return", dest="ret", required=True, metavar="DAY[:DAY]", help="day or window to fly back"
    )
    hunt_parser.add_argument("--cabin", default="business", choices=CABINS, help="default: %(default)s")
    hunt_parser.add_argument(
        "--connections", action="store_true", help="also accept connecting flights (default: nonstop only)"
    )
    hunt_parser.add_argument(
        "--same-airport",
        action="store_true",
        help="fly back to the airport the trip started from (default: any of the departure airports)",
    )
    hunt_parser.add_argument("--adults", type=int, default=1, help="default: %(default)s")
    hunt_parser.add_argument("--currency", default="USD", help="default: %(default)s")
    hunt_parser.add_argument("--top", type=int, default=15, help="how many finds to list (default: %(default)s)")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpx2", "mcp"):  # one INFO line per HTTP request otherwise
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.command == "hunt":
        return _hunt(args)
    try:
        watches = load_watchlist(args.watchlist)
        issues = github.from_environment() if args.github_issues else None
    except ConfigError as exc:
        print(f"farewatch: {exc}", file=sys.stderr)
        return 2
    mention = os.environ.get("GITHUB_REPOSITORY_OWNER") if issues else None
    return scan(watches, Path(args.data_dir), issues=issues, mention=mention)


def _hunt(args: argparse.Namespace) -> int:
    try:
        watch = parse_watch(
            {
                "name": "hunt",
                "origins": args.origins.split(","),
                "destinations": args.destinations.split(","),
                "depart": _window(args.depart),
                "return": _window(args.ret),
                "cabin": args.cabin,
                "nonstop": not args.connections,
                "open_jaw": not args.same_airport,
                "adults": args.adults,
            },
            need_price_line=False,
        )
        result = hunting.hunt(watch, args.currency.upper())
    except ConfigError as exc:
        print(f"farewatch: {exc}", file=sys.stderr)
        return 2
    print(hunting.render(result, watch, top=args.top))
    # Nothing found is a failure only when a source could not be searched.
    return 1 if result.failures and not result.finds else 0


def _window(text: str) -> dict[str, str]:
    """'2027-03-10:2027-03-16' or a single day, in the mapping form watchlist files use."""
    start, _, end = text.partition(":")
    return {"from": start, "to": end or start}


def scan(
    watches: Sequence[Watch],
    data_dir: Path,
    *,
    issues: Issues | None = None,
    mention: str | None = None,
    sources: Mapping[str, Fetch] = SOURCES,
    now: datetime | None = None,
    out: TextIO | None = None,
) -> int:
    """Checks every watch once, records what was found and alerts on fares under the price line.

    Alerts go to `issues` when given and are printed otherwise. Returns the process exit
    code: 1 when a source could not be queried or an alert could not be delivered.
    """
    now = now or datetime.now(timezone.utc)
    out = out or sys.stdout
    state = store.load_state(data_dir)
    failed = False

    for watch in watches:
        hits: list[Itinerary] = []
        complete = True
        for source, fetch in sources.items():
            for currency in watch.alert_below:
                stamp = {
                    "at": now.isoformat(timespec="seconds"),
                    "watch": watch.name,
                    "source": source,
                    "currency": currency,
                }
                try:
                    offers = fetch(watch, currency)
                except SourceError as exc:
                    failed, complete = True, False
                    log.error("%s [%s %s]: %s", watch.name, source, currency, exc)
                    store.append_history(data_dir, {**stamp, "error": str(exc)})
                    continue
                result = rules.evaluate(offers, watch)
                store.append_history(
                    data_dir,
                    {
                        **stamp,
                        "offers": len(offers),
                        "matching": len(result.matching),
                        "hits": len(result.hits),
                        "cheapest": [_compact(it) for it in result.matching[:HISTORY_OFFERS]],
                    },
                )
                print(_summary(watch, source, currency, len(offers), result), file=out)
                hits.extend(result.hits)

        entry = state.setdefault(watch.name, {})
        if not hits:
            if complete:
                entry.pop("last", None)  # the fare is gone, so its return would be news again
            continue
        ranked = sorted(hits, key=lambda it: it.price / watch.alert_below[it.currency])
        best = ranked[0]
        if not alerts.should_alert(best, entry.get("last"), now):
            continue
        title, body = alerts.render(watch, best, ranked[1 : 1 + OTHER_HITS], now=now, mention=mention)
        try:
            issue = _deliver(issues, entry.get("issue"), title, body, out)
        except httpx.HTTPError as exc:
            # Leave "last" untouched so the next scan tries again; the run log keeps the alert meanwhile.
            failed = True
            log.error("%s: could not deliver the alert: %s", watch.name, exc)
            print(f"\n# {title}\n\n{body}", file=out)
            continue
        if issue is not None:
            entry["issue"] = issue
        entry["last"] = {
            "price": best.price,
            "currency": best.currency,
            "flights": best.key,
            "alerted_at": now.isoformat(timespec="seconds"),
        }

    store.save_state(data_dir, state)
    return 1 if failed else 0


def _deliver(issues: Issues | None, number: int | None, title: str, body: str, out: TextIO) -> int | None:
    """Sends the alert and returns the issue it lives in. One open issue per watch collects its alerts."""
    if issues is None:
        print(f"\n# {title}\n\n{body}", file=out)
        return number
    if number is not None and issues.is_open(number):
        issues.comment(number, f"## {title}\n\n{body}")
        return number
    return issues.create(title, body)


def _summary(watch: Watch, source: str, currency: str, offers: int, result: rules.Evaluation) -> str:
    head = f"{watch.name} [{source} {currency}]: {offers} 个报价，{len(result.matching)} 个符合条件"
    if not result.matching:
        return head
    cheapest = result.matching[0]
    line = alerts.money(watch.alert_below[currency], currency)
    verdict = f"命中 {len(result.hits)} 个" if result.hits else "未命中"
    return (
        f"{head}，最低 {alerts.money(cheapest.price, cheapest.currency)}"
        f"（{_flights(cheapest.outbound)} / {_flights(cheapest.inbound)}），价格线 {line}，{verdict}"
    )


def _flights(leg: Leg) -> str:
    return f"{leg.flights} {leg.origin}-{leg.destination} {leg.departure[:10]}"


def _compact(itinerary: Itinerary) -> dict[str, Any]:
    return {
        "price": itinerary.price,
        "carriers": list(itinerary.carriers),
        "out": _compact_leg(itinerary.outbound),
        "in": _compact_leg(itinerary.inbound),
    }


def _compact_leg(leg: Leg) -> dict[str, Any]:
    return {"from": leg.origin, "to": leg.destination, "departs": leg.departure, "flights": leg.flights}
