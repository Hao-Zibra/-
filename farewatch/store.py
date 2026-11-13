"""Files kept between scans: the price history and what was last alerted."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

HISTORY_FILE = "history.jsonl"  # one JSON object per watch, currency and scan
STATE_FILE = "state.json"  # per watch: the open alert issue and the last alerted fare


def append_history(data_dir: Path, record: dict[str, Any]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    with (data_dir / HISTORY_FILE).open("a", encoding="utf-8") as history:
        history.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def load_state(data_dir: Path) -> dict[str, dict[str, Any]]:
    """State from the previous scan. An unreadable file counts as no state: alerts may repeat once."""
    path = data_dir / STATE_FILE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as exc:
        log.warning("ignoring unreadable %s (%s); fares already alerted may be alerted again", path, exc)
        return {}
    if not isinstance(state, dict) or not all(isinstance(entry, dict) for entry in state.values()):
        log.warning("ignoring %s: not a mapping of watch name to state", path)
        return {}
    return state


def save_state(data_dir: Path, state: dict[str, dict[str, Any]]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / STATE_FILE).write_text(
        json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
