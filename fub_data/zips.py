"""Service-area zip lookup and zip extraction from FUB records."""

from __future__ import annotations

import csv
import re
from pathlib import Path

ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")


def _read_zip_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return [row for row in csv.DictReader(f) if normalize_zip(row.get("zip"))]


def load_service_zips(criteria_or_paths, root: Path) -> dict[str, dict]:
    """Return {zip: row} for every serviceable zip, each tagged with its geo tier.

    Accepts the criteria dict (uses `geo_tiers`, falling back to
    `service_zip_files`) or a plain list of CSV paths. Tiers are checked in
    order and a zip takes the first tier it matches. Each tier has
    `id`, `label`, `files`, and optionally `markets` (only rows whose `market`
    column is in that list).
    """
    if isinstance(criteria_or_paths, dict):
        tiers = criteria_or_paths.get("geo_tiers") or [
            {"id": Path(p).stem.removesuffix("_zips"), "files": [p]}
            for p in criteria_or_paths.get("service_zip_files", [])
        ]
    else:
        tiers = [{"id": Path(p).stem.removesuffix("_zips"), "files": [p]} for p in criteria_or_paths]

    zips: dict[str, dict] = {}
    for tier in tiers:
        markets = {m.lower() for m in tier.get("markets") or []}
        for p in tier.get("files", []):
            path = Path(p) if Path(p).is_absolute() else root / p
            for row in _read_zip_csv(path):
                if markets and str(row.get("market", "")).lower() not in markets:
                    continue
                z = normalize_zip(row["zip"])
                zips.setdefault(z, {**row, "_tier": tier["id"], "_tier_label": tier.get("label", tier["id"])})
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
