"""Service-area zip lookup and zip extraction from FUB records."""

from __future__ import annotations

import csv
import re
from pathlib import Path

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def load_service_zips(paths: list[str | Path], root: Path) -> dict[str, dict]:
    """Return {zip: row} from one or more CSVs with at least a `zip` column."""
    zips: dict[str, dict] = {}
    for p in paths:
        path = Path(p)
        if not path.is_absolute():
            path = root / path
        with path.open(newline="") as f:
            for row in csv.DictReader(f):
                z = normalize_zip(row.get("zip"))
                if z:
                    zips.setdefault(z, {**row, "_list": path.stem})
    return zips


def normalize_zip(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if s.isdigit() and len(s) < 5:
        s = s.zfill(5)
    m = ZIP_RE.search(s)
    return m.group(1) if m else None


def _addr_zip(addr: dict) -> str | None:
    for key in ("code", "zip", "zipCode", "postalCode", "postal_code"):
        z = normalize_zip(addr.get(key))
        if z:
            return z
    return None


def extract_zips(person: dict, events: list[dict] | None = None) -> list[tuple[str, str]]:
    """Return [(zip, origin)] in priority order: the seller's property (from
    lead events) first, then the person's own addresses."""
    found: list[tuple[str, str]] = []
    for ev in events or []:
        prop = ev.get("property") or {}
        if isinstance(prop, dict):
            z = _addr_zip(prop)
            if z:
                found.append((z, "event_property"))
    for addr in person.get("addresses") or []:
        if isinstance(addr, dict):
            z = _addr_zip(addr)
            if z:
                found.append((z, f"person_address:{addr.get('type') or 'unknown'}"))
    seen, out = set(), []
    for z, origin in found:
        if z not in seen:
            seen.add(z)
            out.append((z, origin))
    return out
