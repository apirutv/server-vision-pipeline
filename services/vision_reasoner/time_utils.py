# services/vision_reasoner/time_utils.py
from __future__ import annotations
from datetime import datetime
from typing import Optional

from zoneinfo import ZoneInfo

from common.logging import get_logger

log = get_logger("vision_reasoner")


def _parse_iso(iso_str: str, timezone_str: str) -> Optional[float]:
    """
    Parse ISO-8601 string to epoch seconds, normalizing to the given timezone.
    Does NOT interpret natural language; it only parses what the LLM has decided.
    """
    if not iso_str:
        return None

    try:
        dt = datetime.fromisoformat(iso_str)
    except Exception:
        log.warning("Failed to parse from_iso/to_iso; expected ISO-8601", extra={"value": iso_str})
        return None

    tz = ZoneInfo(timezone_str)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    else:
        dt = dt.astimezone(tz)

    return dt.timestamp()


def fill_timestamps_from_iso(plan, timezone_str: str) -> None:
    """
    If the plan has time_window.from_iso/to_iso, derive from_ts/to_ts.
    This mutates the plan in-place; no semantic rules, just parsing.
    """
    tw = plan.time_window

    if tw.from_iso and not tw.from_ts:
        tw.from_ts = _parse_iso(tw.from_iso, timezone_str)

    if tw.to_iso and not tw.to_ts:
        tw.to_ts = _parse_iso(tw.to_iso, timezone_str)
