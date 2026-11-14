from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.repositories import checklist as check_repo
from app.repositories import packs as pack_repo
from app.repositories import releases as release_repo
from app.repositories import settings as settings_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def list_releases(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = release_repo.list_decisions(conn, limit=limit)
        packs = {p["id"]: p for p in pack_repo.list_packs(conn, limit=500)}
        for r in rows:
            p = packs.get(r["pack_id"])
            r["pack_no"] = p["pack_no"] if p else None
            r["pack_status"] = p["status"] if p else None
        return rows
    finally:
        conn.close()


def list_settings() -> dict[str, str]:
    conn = connect()
    try:
        return settings_repo.list_settings(conn)
    finally:
        conn.close()


def decide(pack_id: int, decision: str, reason: str | None = None) -> dict[str, Any]:
    decision = (decision or "").strip().lower()
    if decision not in ("pass", "reject"):
        raise HTTPException(status_code=400, detail="决策须为 pass 或 reject")

    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")

        results = check_repo.list_results(conn, pack_id=pack_id)
        if not results:
            raise HTTPException(status_code=400, detail="请先跑校验清单")

        all_ok = check_repo.all_passed(conn, pack_id)
        require = settings_repo.get_setting(conn, "require_checklist_pass", "1") == "1"

        if decision == "pass":
            if require and not all_ok:
                raise HTTPException(
                    status_code=400,
                    detail="清单未全部通过，不能放行",
                )
            if not reason:
                reason = "清单全部通过，予以放行"
            status = "released"
        else:
            if not reason or not str(reason).strip():
                raise HTTPException(status_code=400, detail="驳回须填写原因")
            status = "rejected"

        now = _now()
        rid = release_repo.create_decision(
            conn,
            pack_id=pack_id,
            decision=decision,
            reason=reason,
            created_at=now,
        )
        pack_repo.update_status(conn, pack_id, status)
        conn.commit()

        return {
            "id": rid,
            "pack_id": pack_id,
            "pack_no": pack["pack_no"],
            "decision": decision,
            "reason": reason,
            "created_at": now,
            "checklist_all_passed": all_ok,
        }
    finally:
        conn.close()
