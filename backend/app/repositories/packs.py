from __future__ import annotations

import json
import sqlite3
from typing import Any


def create_pack(
    conn: sqlite3.Connection,
    *,
    pack_no: str,
    batch_id: int,
    subject_id: int,
    item_count: int,
    fingerprint: str,
    status: str,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO evidence_packs(
            pack_no, batch_id, subject_id, item_count, fingerprint, status, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (pack_no, batch_id, subject_id, item_count, fingerprint, status, created_at),
    )
    return int(cur.lastrowid)


def insert_items(
    conn: sqlite3.Connection,
    pack_id: int,
    items: list[dict[str, Any]],
) -> None:
    for it in items:
        conn.execute(
            """
            INSERT INTO pack_items(pack_id, business_key, summary_json, item_fingerprint)
            VALUES (?,?,?,?)
            """,
            (
                pack_id,
                str(it["business_key"]),
                json.dumps(it["summary"], ensure_ascii=False),
                it["item_fingerprint"],
            ),
        )


def list_packs(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, pack_no, batch_id, subject_id, item_count, fingerprint, status,
               version, amend_state, created_at
        FROM evidence_packs
        ORDER BY id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_pack(conn: sqlite3.Connection, pack_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id, pack_no, batch_id, subject_id, item_count, fingerprint, status,
               version, amend_state, created_at
        FROM evidence_packs WHERE id=?
        """,
        (pack_id,),
    ).fetchone()
    return dict(row) if row else None


def list_items(conn: sqlite3.Connection, pack_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, pack_id, business_key, summary_json, item_fingerprint
        FROM pack_items WHERE pack_id=? ORDER BY business_key
        """,
        (pack_id,),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["summary"] = json.loads(d.pop("summary_json"))
        except json.JSONDecodeError:
            d["summary"] = d.pop("summary_json")
        out.append(d)
    return out


def update_status(conn: sqlite3.Connection, pack_id: int, status: str) -> None:
    conn.execute("UPDATE evidence_packs SET status=? WHERE id=?", (status, pack_id))


def replace_items(
    conn: sqlite3.Connection,
    pack_id: int,
    items: list[dict[str, Any]],
) -> None:
    conn.execute("DELETE FROM pack_items WHERE pack_id=?", (pack_id,))
    insert_items(conn, pack_id, items)


def delete_items_by_key(
    conn: sqlite3.Connection,
    pack_id: int,
    business_keys: list[str],
) -> None:
    for key in business_keys:
        conn.execute(
            "DELETE FROM pack_items WHERE pack_id=? AND business_key=?",
            (pack_id, str(key)),
        )


def update_snapshot(
    conn: sqlite3.Connection,
    pack_id: int,
    *,
    item_count: int,
    fingerprint: str,
    status: str,
    version: int,
    amend_state: str,
) -> None:
    conn.execute(
        """
        UPDATE evidence_packs
        SET item_count=?, fingerprint=?, status=?, version=?, amend_state=?
        WHERE id=?
        """,
        (item_count, fingerprint, status, version, amend_state, pack_id),
    )


def list_pack_ids_for_batch(conn: sqlite3.Connection, batch_id: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM evidence_packs WHERE batch_id=? ORDER BY id",
        (batch_id,),
    ).fetchall()
    return [int(r["id"]) for r in rows]


# ---- 指纹历史 ----


def add_fingerprint_history(
    conn: sqlite3.Connection,
    *,
    pack_id: int,
    version: int,
    fingerprint: str,
    item_count: int,
    state: str,
    source: str,
    amendment_id: int | None,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO pack_fingerprint_history(
            pack_id, version, fingerprint, item_count, state, source, amendment_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?)
        """,
        (pack_id, version, fingerprint, item_count, state, source, amendment_id, created_at),
    )
    return int(cur.lastrowid)


def list_fingerprint_history(
    conn: sqlite3.Connection,
    pack_id: int,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, pack_id, version, fingerprint, item_count, state, source, amendment_id, created_at
        FROM pack_fingerprint_history
        WHERE pack_id=?
        ORDER BY id
        """,
        (pack_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def mark_fingerprint_state(
    conn: sqlite3.Connection,
    pack_id: int,
    version: int,
    state: str,
) -> None:
    conn.execute(
        "UPDATE pack_fingerprint_history SET state=? WHERE pack_id=? AND version=?",
        (state, pack_id, version),
    )


# ---- 补证单 ----


def create_amendment(
    conn: sqlite3.Connection,
    *,
    pack_id: int,
    strategy: str,
    failure_policy: str,
    state: str,
    ops_json: str,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO pack_amendments(
            pack_id, strategy, failure_policy, state, ops_json,
            created_at, updated_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (pack_id, strategy, failure_policy, state, ops_json, created_at, created_at),
    )
    return int(cur.lastrowid)


def get_amendment(conn: sqlite3.Connection, amendment_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM pack_amendments WHERE id=?",
        (amendment_id,),
    ).fetchone()
    return dict(row) if row else None


def get_open_amendment(conn: sqlite3.Connection, pack_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM pack_amendments
        WHERE pack_id=? AND state IN ('draft','submitting')
        ORDER BY id DESC
        LIMIT 1
        """,
        (pack_id,),
    ).fetchone()
    return dict(row) if row else None


def list_amendments(
    conn: sqlite3.Connection,
    pack_id: int | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if pack_id is not None:
        rows = conn.execute(
            "SELECT * FROM pack_amendments WHERE pack_id=? ORDER BY id DESC",
            (pack_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM pack_amendments ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_amendment(conn: sqlite3.Connection, amendment_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    conn.execute(
        f"UPDATE pack_amendments SET {cols} WHERE id=?",
        (*fields.values(), amendment_id),
    )
