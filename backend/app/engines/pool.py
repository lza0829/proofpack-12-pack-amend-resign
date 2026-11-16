from __future__ import annotations

import json
from typing import Any

from app.config import FIXTURES_DIR


def load_fixture_rows(name: str) -> list[dict[str, Any]]:
    """Load the full source pool a subject's batches are allowed to draw from."""
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"fixture {name} must be a JSON array")
    return data


def allowed_pool(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map business_key -> source row. The batch's permitted sample universe."""
    return {str(r.get("business_key", "")): r for r in rows}


def source_item(row: dict[str, Any]) -> dict[str, Any]:
    """Normalise a source row to the field set covered by item fingerprints."""
    return {
        "business_key": str(row.get("business_key", "")),
        "title": row.get("title"),
        "amount": row.get("amount"),
        "qty": row.get("qty"),
        "event_ts": row.get("event_ts"),
        "revision": row.get("revision"),
    }
