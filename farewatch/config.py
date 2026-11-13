"""The watchlist: which trips to watch and the price line for each."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

CABINS = ("economy", "premium_economy", "business", "first")

_AIRPORT = re.compile(r"^[A-Z]{3}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class ConfigError(ValueError):
    """The watchlist or the environment is missing something, or says something impossible."""


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    def __contains__(self, day: date) -> bool:
        return self.start <= day <= self.end


@dataclass(frozen=True)
class Watch:
    """One round trip to watch.

    The traveller leaves from any of `origins`, flies to any of `destinations`
    inside the `depart` window, and flies back inside the `ret` window.
    """

    name: str
    origins: tuple[str, ...]
    destinations: tuple[str, ...]
    depart: DateRange
    ret: DateRange
    alert_below: Mapping[str, float]  # currency -> price line, e.g. {"USD": 4000, "CNY": 28000}
    cabin: str = "business"
    nonstop: bool = True
    adults: int = 1
    # True: the return may land at a different origin airport than the trip started from.
    open_jaw: bool = True


def load_watchlist(path: str | Path) -> list[Watch]:
    path = Path(path)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"watchlist file not found: {path}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if not isinstance(raw, dict) or not isinstance(raw.get("watches"), list) or not raw["watches"]:
        raise ConfigError(f"{path} needs a non-empty 'watches' list")
    watches = [_watch(entry, index) for index, entry in enumerate(raw["watches"])]
    names = [watch.name for watch in watches]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ConfigError(f"watch names must be unique, repeated: {', '.join(duplicates)}")
    return watches


def _watch(entry: Any, index: int) -> Watch:
    if not isinstance(entry, dict):
        raise ConfigError(f"watch #{index + 1} must be a mapping")
    name = entry.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"watch #{index + 1} needs a 'name'")
    where = f"watch '{name}'"

    cabin = entry.get("cabin", "business")
    if cabin not in CABINS:
        raise ConfigError(f"{where}: cabin must be one of {', '.join(CABINS)}, got {cabin!r}")

    adults = entry.get("adults", 1)
    if not isinstance(adults, int) or isinstance(adults, bool) or not 1 <= adults <= 9:
        raise ConfigError(f"{where}: adults must be a whole number from 1 to 9, got {adults!r}")

    depart = _date_range(entry.get("depart"), f"{where}: depart")
    ret = _date_range(entry.get("return"), f"{where}: return")
    if ret.end < depart.start:
        raise ConfigError(f"{where}: the return window ends before the depart window starts")

    return Watch(
        name=name.strip(),
        origins=_airports(entry.get("origins"), f"{where}: origins"),
        destinations=_airports(entry.get("destinations"), f"{where}: destinations"),
        depart=depart,
        ret=ret,
        alert_below=_price_lines(entry.get("alert_below"), where),
        cabin=cabin,
        nonstop=_flag(entry, "nonstop", True, where),
        adults=adults,
        open_jaw=_flag(entry, "open_jaw", True, where),
    )


def _airports(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{where} must be a non-empty list of airport codes")
    codes = []
    for item in value:
        code = str(item).strip().upper()
        if not _AIRPORT.match(code):
            raise ConfigError(f"{where}: {item!r} is not a 3-letter airport code")
        if code not in codes:
            codes.append(code)
    return tuple(codes)


def _date_range(value: Any, where: str) -> DateRange:
    if not isinstance(value, dict) or "from" not in value or "to" not in value:
        raise ConfigError(f"{where} needs 'from' and 'to' dates")
    start, end = _date(value["from"], where), _date(value["to"], where)
    if end < start:
        raise ConfigError(f"{where}: 'to' ({end}) is before 'from' ({start})")
    return DateRange(start, end)


def _date(value: Any, where: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ConfigError(f"{where}: {value!r} is not a date like 2027-01-05") from None


def _price_lines(value: Any, where: str) -> dict[str, float]:
    if not isinstance(value, dict) or not value:
        raise ConfigError(f"{where}: alert_below needs at least one price line, e.g. {{USD: 4000}}")
    lines = {}
    for currency, amount in value.items():
        code = str(currency).strip().upper()
        if not _CURRENCY.match(code):
            raise ConfigError(f"{where}: alert_below key {currency!r} is not a 3-letter currency code")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            raise ConfigError(f"{where}: alert_below[{code}] must be a positive number, got {amount!r}")
        lines[code] = float(amount)
    return lines


def _flag(entry: dict, key: str, default: bool, where: str) -> bool:
    value = entry.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: {key} must be true or false, got {value!r}")
    return value
