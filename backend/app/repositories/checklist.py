from __future__ import annotations

import sqlite3
from typing import Any


def clear_for_pack(conn: sqlite3.Connection, pack_id: int) -> None:
    conn.execute("DELETE FROM checklist_results WHERE pack_id=?", (pack_id,))


def delete_version(conn: sqlite3.Connection, pack_id: int, pack_version: int) -> None:
    conn.execute(
        "DELETE FROM checklist_results WHERE pack_id=? AND pack_version=?",
        (pack_id, pack_version),
    )


def set_voided_version(
    conn: sqlite3.Connection,
    pack_id: int,
    pack_version: int,
    voided: bool,
) -> None:
    conn.execute(
        "UPDATE checklist_results SET voided=? WHERE pack_id=? AND pack_version=?",
        (1 if voided else 0, pack_id, pack_version),
    )


def insert_result(
    conn: sqlite3.Connection,
    *,
    pack_id: int,
    pack_version: int,
    check_code: str,
    check_title: str,
    passed: bool,
    detail: str,
    created_at: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO checklist_results(
            pack_id, pack_version, check_code, check_title, passed, detail, created_at
        ) VALUES (?,?,?,?,?,?,?)
        """,
        (pack_id, pack_version, check_code, check_title, 1 if passed else 0, detail, created_at),
    )
    return int(cur.lastrowid)


def list_results(
    conn: sqlite3.Connection,
    pack_id: int | None = None,
    limit: int = 100,
    pack_version: int | None = None,
) -> list[dict[str, Any]]:
    if pack_id is not None:
        if pack_version is not None:
            rows = conn.execute(
                """
                SELECT id, pack_id, pack_version, check_code, check_title, passed, voided,
                       detail, created_at
                FROM checklist_results
                WHERE pack_id=? AND pack_version=?
                ORDER BY id
                """,
                (pack_id, pack_version),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, pack_id, pack_version, check_code, check_title, passed, voided,
                       detail, created_at
                FROM checklist_results
                WHERE pack_id=?
                ORDER BY id
                """,
                (pack_id,),
            ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, pack_id, pack_version, check_code, check_title, passed, voided,
                   detail, created_at
            FROM checklist_results
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_version_for_pack(conn: sqlite3.Connection, pack_id: int) -> int | None:
    row = conn.execute(
        "SELECT MAX(pack_version) v FROM checklist_results WHERE pack_id=?",
        (pack_id,),
    ).fetchone()
    return int(row["v"]) if row and row["v"] is not None else None


def all_passed(
    conn: sqlite3.Connection,
    pack_id: int,
    pack_version: int | None = None,
) -> bool:
    """Only the checklist for the pack's current version counts as a pass."""
    if pack_version is None:
        pack_version = latest_version_for_pack(conn, pack_id)
    if pack_version is None:
        return False
    rows = conn.execute(
        "SELECT passed FROM checklist_results WHERE pack_id=? AND pack_version=?",
        (pack_id, pack_version),
    ).fetchall()
    if not rows:
        return False
    return all(int(r["passed"]) == 1 for r in rows)
