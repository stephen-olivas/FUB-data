from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def load_criteria(path: str | Path | None = None) -> dict:
    path = Path(path) if path else ROOT / "config" / "criteria.yaml"
    with path.open() as f:
        cfg = yaml.safe_load(f) or {}
    ids = [r.get("id") for r in cfg.get("unqualified", [])]
    if any(not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError("Every unqualified rule needs a unique `id`.")
    return cfg
