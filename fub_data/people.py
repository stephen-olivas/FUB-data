"""Fetch people created in a window, plus optional per-person activity."""

from __future__ import annotations

import sys
from typing import Any

from .client import FUBClient, FUBError
from .window import Window, parse_ts, people_in_window


def fetch_people_in_window(
    client: FUBClient,
    window: Window,
    include_trash: bool = True,
    fields: str = "allFields",
    stop_after_older: int = 200,
) -> list[dict]:
    """Walk /people newest-first and keep those created inside the window.

    FUB's /people has no created-date filter. We ask for `sort=-created`; if the
    account rejects that we fall back to the default order, detect its
    direction from the first page, and (if ascending) scan everything.
    """
    base = {"fields": fields, "includeTrash": str(include_trash).lower()}
    try:
        first = client.get("people", {**base, "sort": "-created", "limit": 100})
        params = {**base, "sort": "-created"}
    except FUBError as exc:
        print(f"  sort=-created rejected ({exc}); using default order", file=sys.stderr)
        first = client.get("people", {**base, "limit": 100})
        params = dict(base)

    rows = first.get("people", []) or []
    dates = [parse_ts(p.get("created")) for p in rows if p.get("created")]
    ascending = len(dates) >= 2 and dates[0] < dates[-1]

    stream = client.paginate("people", params, "people")
    if ascending:
        print("  /people returned oldest-first; scanning all people (slower).", file=sys.stderr)
        return [p for p in stream if (ts := parse_ts(p.get("created"))) and window.contains(ts)]
    return list(people_in_window(stream, window, stop_after_older=stop_after_older))


# Per-person activity endpoints. Each is fetched only when a rule needs it.
ACTIVITY_ENDPOINTS = {
    "events": ("events", "events"),
    "notes": ("notes", "notes"),
    "texts": ("textMessages", "textmessages"),
    "calls": ("calls", "calls"),
    "appointments": ("appointments", "appointments"),
    "deals": ("deals", "deals"),
}


def _collection_from(data: dict, fallback: str) -> list[dict]:
    meta = data.get("_metadata", {}) or {}
    key = meta.get("collection")
    if key and key in data:
        return data[key] or []
    # tolerate casing differences (textMessages vs textmessages)
    for k, v in data.items():
        if k.lower() == fallback.lower() and isinstance(v, list):
            return v
    return []


def fetch_activity(
    client: FUBClient,
    person_id: int | str,
    kinds: list[str],
    max_per_kind: int = 100,
) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """Return ({kind: records}, {kind: error}) for one person."""
    out: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for kind in kinds:
        path, coll = ACTIVITY_ENDPOINTS[kind]
        try:
            data = client.get(path, {"personId": person_id, "limit": max_per_kind})
            out[kind] = _collection_from(data, coll)
        except FUBError as exc:
            out[kind] = []
            errors[kind] = str(exc)
    return out, errors
