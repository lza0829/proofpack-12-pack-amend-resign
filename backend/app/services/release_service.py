from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.engines.checksum import item_fingerprint, pack_fingerprint_from_digests
from app.repositories import checklist as check_repo
from app.repositories import packs as pack_repo
from app.repositories import releases as release_repo
from app.repositories import samples as sample_repo
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

        if pack["status"] == "released":
            raise HTTPException(status_code=409, detail="证据包已放行，不可重复决策")

        # 补证闸：补证进行中禁止放行；补证失败态未撤销前禁止放行
        if decision == "pass":
            if pack["amend_state"] == "in_progress":
                raise HTTPException(status_code=409, detail="补证进行中，禁止放行")
            if pack["amend_state"] == "failed" or pack["status"] == "amend_failed":
                raise HTTPException(
                    status_code=409,
                    detail="包处于补证失败态，须显式撤销补证或再次补证成功后才能放行",
                )

        results = check_repo.list_results(conn, pack_id=pack_id)
        if not results:
            raise HTTPException(status_code=400, detail="请先跑校验清单")

        all_ok = check_repo.all_passed(conn, pack_id, int(pack["version"]))
        require = settings_repo.get_setting(conn, "require_checklist_pass", "1") == "1"

        # 指纹互证：包指纹必须与包内条目重算结果一致，且每条摘要须与条目指纹吻合，
        # 防止改条目不改指纹（或只改摘要不重签）
        items = pack_repo.list_items(conn, pack_id)
        recomputed = pack_fingerprint_from_digests(
            [(it["business_key"], it["item_fingerprint"]) for it in items]
        )
        digests_ok = all(
            item_fingerprint(
                {
                    "business_key": it["business_key"],
                    **(it.get("summary") if isinstance(it.get("summary"), dict) else {}),
                }
            )
            == it["item_fingerprint"]
            for it in items
        )
        fingerprint_ok = (
            recomputed == pack["fingerprint"]
            and len(items) == pack["item_count"]
            and digests_ok
        )
        row_count = len(sample_repo.list_rows(conn, pack["batch_id"]))
        rows_ok = row_count == pack["item_count"]

        if decision == "pass":
            if not fingerprint_ok:
                raise HTTPException(
                    status_code=409,
                    detail="包指纹与条目不一致（条目被改动或未重签），禁止放行",
                )
            if not rows_ok:
                raise HTTPException(
                    status_code=409,
                    detail="包条目数与样本行不一致，禁止放行",
                )
            if require and not all_ok:
                raise HTTPException(
                    status_code=400,
                    detail="当前版本清单未全部通过，不能放行",
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
            "fingerprint_verified": fingerprint_ok,
            "pack_version": pack["version"],
        }
    finally:
        conn.close()
