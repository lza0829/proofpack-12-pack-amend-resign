from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException

from app.db import connect
from app.engines.checksum import item_fingerprint, pack_fingerprint_from_digests
from app.engines.pool import allowed_pool, load_fixture_rows, source_item
from app.repositories import checklist as check_repo
from app.repositories import packs as pack_repo
from app.repositories import samples as sample_repo
from app.repositories import subjects as subject_repo
from app.services import checklist_service

STRATEGIES = {
    "incremental": "增量补证",
    "resign": "整包重签",
}
FAILURE_POLICIES = {
    "rollback": "失败整笔回滚",
    "failed_state": "失败进入补证失败态",
}

# 增量补证：旧指纹进历史；整包重签：旧版本作废，仅审计可查
OLD_FINGERPRINT_STATE = {
    "incremental": "superseded",
    "resign": "voided",
}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _validate_choice(strategy: str, failure_policy: str) -> tuple[str, str]:
    strategy = (strategy or "").strip().lower()
    failure_policy = (failure_policy or "").strip().lower()
    if strategy not in STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail="补证提交策略须为 incremental（增量补证）或 resign（整包重签），二者只能择一",
        )
    if failure_policy not in FAILURE_POLICIES:
        raise HTTPException(
            status_code=400,
            detail="补证失败命运须为 rollback（整笔回滚）或 failed_state（补证失败态），二者只能择一",
        )
    return strategy, failure_policy


def _normalize_ops(ops: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not ops:
        raise HTTPException(status_code=400, detail="补证至少需要一条追加或替换操作")
    normalized: list[dict[str, Any]] = []
    for raw in ops:
        if not isinstance(raw, dict):
            raise HTTPException(status_code=400, detail="补证操作格式不合法")
        op = str(raw.get("op", "")).strip().lower()
        key = str(raw.get("business_key", "")).strip()
        if op not in ("add", "replace"):
            raise HTTPException(
                status_code=400,
                detail="补证操作只支持 add（追加）或 replace（替换）",
            )
        if not key:
            raise HTTPException(status_code=400, detail="补证操作缺少业务键")
        if op == "add":
            normalized.append({"op": "add", "business_key": key})
        else:
            from_key = str(raw.get("from_business_key", "")).strip()
            if not from_key:
                raise HTTPException(status_code=400, detail="替换操作须给出被替换的业务键")
            if from_key == key:
                raise HTTPException(status_code=400, detail="替换前后不能是同一业务键")
            normalized.append(
                {
                    "op": "replace",
                    "from_business_key": from_key,
                    "business_key": key,
                }
            )
    targets = [o["business_key"] for o in normalized]
    if len(targets) != len(set(targets)):
        raise HTTPException(status_code=400, detail="补证操作的目标业务键重复")
    return normalized


def _serialize_amendment(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["ops"] = _loads(out.pop("ops_json", None), [])
    out.pop("snapshot_json", None)
    out["strategy_label"] = STRATEGIES.get(out.get("strategy", ""), out.get("strategy"))
    out["failure_policy_label"] = FAILURE_POLICIES.get(
        out.get("failure_policy", ""), out.get("failure_policy")
    )
    return out


# ---- 查询 ----


def list_amendments(pack_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = pack_repo.list_amendments(conn, pack_id=pack_id)
        return [_serialize_amendment(r) for r in rows]
    finally:
        conn.close()


def get_amendment(amendment_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        row = pack_repo.get_amendment(conn, amendment_id)
        if not row:
            raise HTTPException(status_code=404, detail="补证单不存在")
        out = _serialize_amendment(row)
        pack = pack_repo.get_pack(conn, row["pack_id"])
        out["pack"] = pack
        return out
    finally:
        conn.close()


def pool_view(pack_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        subject = subject_repo.get_subject(conn, pack["subject_id"])
        if not subject:
            raise HTTPException(status_code=404, detail="举证对象不存在")
        source = load_fixture_rows(subject["fixture"])
        pool = allowed_pool(source)
        items = pack_repo.list_items(conn, pack_id)
        in_pack = {it["business_key"] for it in items}
        allowed = []
        for key, row in sorted(pool.items()):
            allowed.append(
                {
                    "business_key": key,
                    "title": row.get("title"),
                    "amount": row.get("amount"),
                    "qty": row.get("qty"),
                    "event_ts": row.get("event_ts"),
                    "revision": row.get("revision"),
                    "in_pack": key in in_pack,
                }
            )
        return {
            "pack_id": pack_id,
            "batch_id": pack["batch_id"],
            "subject_code": subject["code"],
            "subject_title": subject["title"],
            "allowed_pool": allowed,
            "current_items": [
                {"business_key": it["business_key"], "summary": it["summary"]} for it in items
            ],
        }
    finally:
        conn.close()


# ---- 发起 / 撤销 ----


def create_amendment(
    *,
    pack_id: int,
    strategy: str,
    failure_policy: str,
    ops: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    strategy, failure_policy = _validate_choice(strategy, failure_policy)
    normalized = _normalize_ops(ops)

    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        if pack["status"] == "released":
            raise HTTPException(status_code=409, detail="已放行包禁止补证")
        if pack["amend_state"] == "in_progress":
            raise HTTPException(status_code=409, detail="补证进行中，不能重复发起")
        if pack_repo.get_open_amendment(conn, pack_id):
            raise HTTPException(status_code=409, detail="该包已有未结补证单")
        siblings = [
            pid
            for pid in pack_repo.list_pack_ids_for_batch(conn, pack["batch_id"])
            if pid != pack_id
        ]
        if siblings:
            raise HTTPException(
                status_code=409,
                detail="同一采样批次已生成多个证据包，补证会使其它包条数互证失效，禁止补证",
            )

        now = _now()
        amendment_id = pack_repo.create_amendment(
            conn,
            pack_id=pack_id,
            strategy=strategy,
            failure_policy=failure_policy,
            state="draft",
            ops_json=_dumps(normalized),
            created_at=now,
        )
        pack_repo.update_amendment(
            conn,
            amendment_id,
            before_version=pack["version"],
            before_count=pack["item_count"],
            before_fingerprint=pack["fingerprint"],
            before_status=pack["status"],
            before_amend_state=pack["amend_state"],
        )
        # 补证进行中：禁止放行
        conn.execute(
            "UPDATE evidence_packs SET amend_state='in_progress' WHERE id=?",
            (pack_id,),
        )
        conn.commit()
        row = pack_repo.get_amendment(conn, amendment_id)
        return _serialize_amendment(row)
    finally:
        conn.close()


def _cancel_response(conn, amendment_id: int) -> dict[str, Any]:
    amendment = pack_repo.get_amendment(conn, amendment_id)
    out = _serialize_amendment(amendment)
    pack_id = amendment["pack_id"]
    out["pack"] = pack_repo.get_pack(conn, pack_id)
    out["fingerprint_history"] = pack_repo.list_fingerprint_history(conn, pack_id)
    out["checklist"] = check_repo.list_results(conn, pack_id=pack_id)
    return out


def _prior_amend_state(amendment: dict[str, Any]) -> str:
    return amendment.get("before_amend_state") or "none"


def cancel_amendment(amendment_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        amendment = pack_repo.get_amendment(conn, amendment_id)
        if not amendment:
            raise HTTPException(status_code=404, detail="补证单不存在")
        pack = pack_repo.get_pack(conn, amendment["pack_id"])
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")

        state = amendment["state"]
        if state == "draft":
            pack_repo.update_amendment(
                conn,
                amendment_id,
                state="cancelled",
                updated_at=_now(),
            )
            conn.execute(
                "UPDATE evidence_packs SET amend_state=? WHERE id=?",
                (_prior_amend_state(amendment), pack["id"]),
            )
            conn.commit()
            return _cancel_response(conn, amendment_id)

        if state != "failed":
            raise HTTPException(
                status_code=409,
                detail="只有进行中的草稿或补证失败态可以撤销",
            )

        snapshot = _loads(amendment.get("snapshot_json"), None)
        if amendment.get("applied") and snapshot:
            # 恢复到补证前：包指纹、条目、清单与指纹历史一并复原
            _restore_snapshot(conn, pack["id"], snapshot)
        # 恢复后解除本单造成的补证态，回到发起补证之前的补证态
        conn.execute(
            "UPDATE evidence_packs SET amend_state=? WHERE id=?",
            (_prior_amend_state(amendment), pack["id"]),
        )
        pack_repo.update_amendment(
            conn,
            amendment_id,
            state="cancelled",
            updated_at=_now(),
        )
        conn.commit()
        return _cancel_response(conn, amendment_id)
    finally:
        conn.close()


def _snapshot_pack(conn, pack: dict[str, Any]) -> dict[str, Any]:
    items = pack_repo.list_items(conn, pack["id"])
    rows = sample_repo.list_rows(conn, pack["batch_id"])
    checklist = conn.execute(
        "SELECT * FROM checklist_results WHERE pack_id=? ORDER BY id",
        (pack["id"],),
    ).fetchall()
    history = conn.execute(
        "SELECT * FROM pack_fingerprint_history WHERE pack_id=? ORDER BY id",
        (pack["id"],),
    ).fetchall()
    return {
        "pack": {
            "fingerprint": pack["fingerprint"],
            "item_count": pack["item_count"],
            "version": pack["version"],
            "status": pack["status"],
            "amend_state": pack["amend_state"],
        },
        "items": [
            {
                "id": it["id"],
                "business_key": it["business_key"],
                "summary": it["summary"],
                "item_fingerprint": it["item_fingerprint"],
            }
            for it in items
        ],
        "rows": [
            {
                "id": r["id"],
                "business_key": r["business_key"],
                "payload": r.get("payload"),
                "event_ts": r.get("event_ts"),
                "revision": r.get("revision"),
                "origin": r.get("origin", "sampled"),
            }
            for r in rows
        ],
        "checklist": [dict(r) for r in checklist],
        "history": [dict(r) for r in history],
    }


def _restore_snapshot(conn, pack_id: int, snapshot: dict[str, Any]) -> None:
    """Restore pack fields, items, sample rows, checklist and fingerprint history exactly."""
    p = snapshot.get("pack", {})
    conn.execute(
        """
        UPDATE evidence_packs
        SET fingerprint=?, item_count=?, version=?, status=?, amend_state=?
        WHERE id=?
        """,
        (
            p.get("fingerprint"),
            p.get("item_count"),
            p.get("version", 1),
            p.get("status"),
            p.get("amend_state", "none"),
            pack_id,
        ),
    )

    conn.execute("DELETE FROM pack_items WHERE pack_id=?", (pack_id,))
    for it in snapshot.get("items", []):
        conn.execute(
            """
            INSERT INTO pack_items(id, pack_id, business_key, summary_json, item_fingerprint)
            VALUES (?,?,?,?,?)
            """,
            (
                it.get("id"),
                pack_id,
                it.get("business_key"),
                _dumps(it.get("summary") or {}),
                it.get("item_fingerprint"),
            ),
        )

    batch_id = conn.execute(
        "SELECT batch_id FROM evidence_packs WHERE id=?", (pack_id,)
    ).fetchone()["batch_id"]
    conn.execute("DELETE FROM sample_rows WHERE batch_id=?", (batch_id,))
    for r in snapshot.get("rows", []):
        conn.execute(
            """
            INSERT INTO sample_rows(id, batch_id, business_key, payload_json, event_ts, revision, origin)
            VALUES (?,?,?,?,?,?,?)
            """,
            (
                r.get("id"),
                batch_id,
                r.get("business_key"),
                _dumps(r.get("payload") or {}),
                r.get("event_ts"),
                r.get("revision"),
                r.get("origin", "sampled"),
            ),
        )
    conn.execute(
        "UPDATE sample_batches SET sample_count=(SELECT COUNT(*) FROM sample_rows WHERE batch_id=?) WHERE id=?",
        (batch_id, batch_id),
    )

    conn.execute("DELETE FROM checklist_results WHERE pack_id=?", (pack_id,))
    for c in snapshot.get("checklist", []):
        conn.execute(
            """
            INSERT INTO checklist_results(
                id, pack_id, pack_version, check_code, check_title, passed, voided, detail, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                c.get("id"),
                pack_id,
                c.get("pack_version", 1),
                c.get("check_code"),
                c.get("check_title"),
                c.get("passed"),
                c.get("voided", 0),
                c.get("detail"),
                c.get("created_at"),
            ),
        )

    conn.execute("DELETE FROM pack_fingerprint_history WHERE pack_id=?", (pack_id,))
    for h in snapshot.get("history", []):
        conn.execute(
            """
            INSERT INTO pack_fingerprint_history(
                id, pack_id, version, fingerprint, item_count, state, source, amendment_id, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                h.get("id"),
                pack_id,
                h.get("version"),
                h.get("fingerprint"),
                h.get("item_count"),
                h.get("state"),
                h.get("source"),
                h.get("amendment_id"),
                h.get("created_at"),
            ),
        )


# ---- 提交 ----


def _item_from_pool(pool: dict[str, dict[str, Any]], key: str) -> dict[str, Any]:
    src = source_item(pool[key])
    return {
        "business_key": key,
        "summary": {
            "title": src["title"],
            "amount": src["amount"],
            "qty": src["qty"],
            "event_ts": src["event_ts"],
            "revision": src["revision"],
        },
        "item_fingerprint": item_fingerprint(src),
    }


def _compute_new_items(
    *,
    strategy: str,
    current_items: list[dict[str, Any]],
    ops: list[dict[str, Any]],
    pool: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    Return the full post-amendment item set.
    Both strategies produce a fingerprint over the complete item set; they differ in
    how retained items are carried (incremental keeps stored digests, resign recomputes
    every item from the allowed pool) and how the old fingerprint is archived.
    """
    by_key: dict[str, dict[str, Any]] = {
        it["business_key"]: dict(it) for it in current_items
    }
    removed: set[str] = set()

    for op in ops:
        key = op["business_key"]
        if key not in pool:
            raise ValueError(f"业务键 {key} 不在该批次允许的样本集合内")
        if op["op"] == "add":
            if key in by_key and key not in removed:
                raise ValueError(f"业务键 {key} 已在包内，不能重复追加")
            removed.discard(key)
        else:
            from_key = op["from_business_key"]
            if from_key not in by_key or from_key in removed:
                raise ValueError(f"业务键 {from_key} 不在包内，无法替换")
            if key in by_key and key not in removed and key != from_key:
                raise ValueError(f"业务键 {key} 已在包内，不能作为替换目标")
            removed.add(from_key)
            by_key.pop(from_key, None)
        by_key[key] = _item_from_pool(pool, key)

    if strategy == "resign":
        # 整包重签：完整条目集全部按源池重算摘要与条目指纹
        rebuilt: dict[str, dict[str, Any]] = {}
        for key in sorted(by_key):
            if key not in pool:
                raise ValueError(f"业务键 {key} 不在该批次允许的样本集合内，无法整包重签")
            rebuilt[key] = _item_from_pool(pool, key)
        by_key = rebuilt

    return [by_key[k] for k in sorted(by_key)]


def submit_amendment(amendment_id: int) -> dict[str, Any]:
    conn = connect()
    conn.isolation_level = None
    try:
        amendment = pack_repo.get_amendment(conn, amendment_id)
        if not amendment:
            raise HTTPException(status_code=404, detail="补证单不存在")
        if amendment["state"] != "draft":
            raise HTTPException(status_code=409, detail="只有草稿状态的补证单可以提交")

        strategy = amendment["strategy"]
        failure_policy = amendment["failure_policy"]
        ops = _loads(amendment.get("ops_json"), [])
        pack_id = amendment["pack_id"]

        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        if pack["status"] == "released":
            raise HTTPException(status_code=409, detail="已放行包禁止补证")
        if pack["amend_state"] != "in_progress":
            raise HTTPException(status_code=409, detail="该包当前不在补证进行中")

        subject = subject_repo.get_subject(conn, pack["subject_id"])
        if not subject:
            raise HTTPException(status_code=404, detail="举证对象不存在")

        # 同一批次的样本行按包内条目同步；若同批次还有其它包，补证会污染它们的条数互证
        siblings = [
            pid
            for pid in pack_repo.list_pack_ids_for_batch(conn, pack["batch_id"])
            if pid != pack_id
        ]
        if siblings:
            raise HTTPException(
                status_code=409,
                detail="同一采样批次已生成多个证据包，补证会使其它包条数互证失效，禁止补证",
            )

        pool = allowed_pool(load_fixture_rows(subject["fixture"]))
        current_items = pack_repo.list_items(conn, pack_id)

        now = _now()
        failure_reason: str | None = None
        new_items: list[dict[str, Any]] = []
        new_fp: str | None = None
        new_version = int(pack["version"]) + 1
        applied = False
        checklist_outcome: dict[str, Any] | None = None

        try:
            new_items = _compute_new_items(
                strategy=strategy,
                current_items=current_items,
                ops=ops,
                pool=pool,
            )
            new_fp = pack_fingerprint_from_digests(
                [(it["business_key"], it["item_fingerprint"]) for it in new_items]
            )
            if new_fp == pack["fingerprint"]:
                raise ValueError("补证后的完整条目集未产生新指纹")
        except ValueError as exc:
            failure_reason = str(exc)
            new_items = []

        if failure_reason is None:
            snapshot = _snapshot_pack(conn, pack)
            conn.execute("BEGIN IMMEDIATE")
            # 原子认领草稿，防止并发重复提交（失败则回滚并直接拒绝，不进入命运分支）
            claimed = conn.execute(
                "UPDATE pack_amendments SET state='submitting', snapshot_json=?, updated_at=? "
                "WHERE id=? AND state='draft'",
                (_dumps(snapshot), now, amendment_id),
            )
            if claimed.rowcount != 1:
                conn.execute("ROLLBACK")
                raise HTTPException(status_code=409, detail="补证单已被提交或状态已变更")
            try:
                applied = True
                _apply_items(conn, pack, strategy, current_items, new_items, pool)

                # 新指纹落库；旧指纹按策略归档（历史 / 作废仅审计）
                conn.execute(
                    """
                    UPDATE evidence_packs
                    SET fingerprint=?, item_count=?, version=?, status='built', amend_state='in_progress'
                    WHERE id=?
                    """,
                    (new_fp, len(new_items), new_version, pack_id),
                )
                pack_repo.mark_fingerprint_state(
                    conn, pack_id, int(pack["version"]), OLD_FINGERPRINT_STATE[strategy]
                )
                pack_repo.add_fingerprint_history(
                    conn,
                    pack_id=pack_id,
                    version=new_version,
                    fingerprint=new_fp,
                    item_count=len(new_items),
                    state="active",
                    source=f"amend:{strategy}",
                    amendment_id=amendment_id,
                    created_at=now,
                )

                # 作废旧清单通过态：旧版本结果一律 voided，仅审计可查
                conn.execute(
                    "UPDATE checklist_results SET voided=1 WHERE pack_id=? AND pack_version<?",
                    (pack_id, new_version),
                )
                conn.execute(
                    "DELETE FROM checklist_results WHERE pack_id=? AND pack_version=?",
                    (pack_id, new_version),
                )

                # 补证后必须复检清单
                amended_pack = pack_repo.get_pack(conn, pack_id)
                checklist_outcome = checklist_service.evaluate_on_conn(conn, amended_pack)
                checklist_ok = bool(checklist_outcome["all_passed"])

                if checklist_ok:
                    conn.execute(
                        """
                        UPDATE evidence_packs
                        SET amend_state='none', status='checked_ok'
                        WHERE id=?
                        """,
                        (pack_id,),
                    )
                    pack_repo.update_amendment(
                        conn,
                        amendment_id,
                        state="succeeded",
                        applied=1,
                        after_version=new_version,
                        after_count=len(new_items),
                        after_fingerprint=new_fp,
                        after_status="checked_ok",
                        checklist_rerun_passed=1,
                        updated_at=now,
                    )
                    conn.execute("COMMIT")
                    return _submit_response(conn, amendment_id, pack_id)
                failure_reason = "补证后清单复检未通过"
            except (ValueError, HTTPException) as exc:
                failure_reason = str(getattr(exc, "detail", exc))
            except Exception as exc:  # 兜底，避免半补证落库
                failure_reason = f"补证执行异常: {exc}"

            if failure_policy == "rollback":
                conn.execute("ROLLBACK")
                # 整笔回滚后不得留下半补证可放行态，也不阻塞下一次补证；
                # 若本单是在此前失败态上重试，则恢复到先前的补证前状态
                conn.execute(
                    "UPDATE evidence_packs SET amend_state=? WHERE id=?",
                    (_prior_amend_state(amendment), pack_id),
                )
                _record_failed_attempt(
                    conn,
                    amendment_id=amendment_id,
                    pack_id=pack_id,
                    failure_reason=failure_reason or "补证失败",
                    applied=0,
                    after_version=None,
                    after_count=None,
                    after_fingerprint=None,
                    after_status=None,
                    checklist_ok=None,
                    now=now,
                )
                return _submit_response(conn, amendment_id, pack_id)
            else:
                # 失败进入补证失败态：保留已落库的补证结果供审计，禁止放行
                failed_status = "amend_failed" if applied else pack["status"]
                conn.execute(
                    "UPDATE evidence_packs SET amend_state='failed', status=? WHERE id=?",
                    (failed_status, pack_id),
                )
                _record_failed_attempt(
                    conn,
                    amendment_id=amendment_id,
                    pack_id=pack_id,
                    failure_reason=failure_reason or "补证失败",
                    applied=1 if applied else 0,
                    after_version=new_version if applied else None,
                    after_count=len(new_items) if applied else None,
                    after_fingerprint=new_fp if applied else None,
                    after_status=failed_status if applied else None,
                    checklist_ok=(
                        bool(checklist_outcome["all_passed"]) if checklist_outcome else None
                    ),
                    now=now,
                )
                return _submit_response(conn, amendment_id, pack_id)
        else:
            # 计算阶段即失败：未触碰任何包数据
            conn.execute("BEGIN IMMEDIATE")
            try:
                if failure_policy == "rollback":
                    conn.execute(
                        "UPDATE evidence_packs SET amend_state=? WHERE id=?",
                        (_prior_amend_state(amendment), pack_id),
                    )
                    _record_failed_attempt(
                        conn,
                        amendment_id=amendment_id,
                        pack_id=pack_id,
                        failure_reason=failure_reason,
                        applied=0,
                        after_version=None,
                        after_count=None,
                        after_fingerprint=None,
                        after_status=None,
                        checklist_ok=None,
                        now=now,
                        commit=False,
                    )
                else:
                    conn.execute(
                        "UPDATE evidence_packs SET amend_state='failed' WHERE id=?",
                        (pack_id,),
                    )
                    _record_failed_attempt(
                        conn,
                        amendment_id=amendment_id,
                        pack_id=pack_id,
                        failure_reason=failure_reason,
                        applied=0,
                        after_version=None,
                        after_count=None,
                        after_fingerprint=None,
                        after_status=None,
                        checklist_ok=None,
                        now=now,
                        commit=False,
                    )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            return _submit_response(conn, amendment_id, pack_id)
    finally:
        conn.close()


def _record_failed_attempt(
    conn,
    *,
    amendment_id: int,
    pack_id: int,
    failure_reason: str,
    applied: int,
    after_version: int | None,
    after_count: int | None,
    after_fingerprint: str | None,
    after_status: str | None,
    checklist_ok: bool | None,
    now: str,
    commit: bool = True,
) -> None:
    pack_repo.update_amendment(
        conn,
        amendment_id,
        state="failed",
        applied=applied,
        after_version=after_version,
        after_count=after_count,
        after_fingerprint=after_fingerprint,
        after_status=after_status,
        checklist_rerun_passed=(
            None if checklist_ok is None else (1 if checklist_ok else 0)
        ),
        failure_reason=failure_reason,
        updated_at=now,
    )
    if commit:
        conn.commit()


def _apply_items(
    conn,
    pack: dict[str, Any],
    strategy: str,
    current_items: list[dict[str, Any]],
    new_items: list[dict[str, Any]],
    pool: dict[str, dict[str, Any]],
) -> None:
    pack_id = pack["id"]
    batch_id = pack["batch_id"]
    new_keys = {it["business_key"] for it in new_items}
    old_keys = {it["business_key"] for it in current_items}

    removed = sorted(old_keys - new_keys)
    if removed:
        pack_repo.delete_items_by_key(conn, pack_id, removed)
        sample_repo.delete_rows_by_key(conn, batch_id, removed)

    if strategy == "resign":
        # 整包重签：完整条目集整体重写，样本行同步按源池刷新
        pack_repo.replace_items(conn, pack_id, new_items)
    else:
        # 增量补证：保留原条目，仅追加新条目
        changed = [it for it in new_items if it["business_key"] not in old_keys]
        if changed:
            pack_repo.insert_items(conn, pack_id, changed)

    existing_rows = {r["business_key"] for r in sample_repo.list_rows(conn, batch_id)}
    for it in new_items:
        key = it["business_key"]
        if strategy == "incremental" and key in existing_rows and key not in removed:
            # 保留的原条目与样本行保持原样，不静默改条目
            continue
        src = source_item(pool[key])
        sample_repo.upsert_row(
            conn,
            batch_id,
            {
                "business_key": key,
                "title": src["title"],
                "amount": src["amount"],
                "qty": src["qty"],
                "event_ts": src["event_ts"],
                "revision": src["revision"],
            },
            origin="amended",
        )
    conn.execute(
        "UPDATE sample_batches SET sample_count=(SELECT COUNT(*) FROM sample_rows WHERE batch_id=?) WHERE id=?",
        (batch_id, batch_id),
    )


def _submit_response(conn, amendment_id: int, pack_id: int) -> dict[str, Any]:
    amendment = pack_repo.get_amendment(conn, amendment_id)
    pack = pack_repo.get_pack(conn, pack_id)
    out = _serialize_amendment(amendment)
    out["pack"] = pack
    out["fingerprint_history"] = pack_repo.list_fingerprint_history(conn, pack_id)
    out["checklist"] = check_repo.list_results(conn, pack_id=pack_id)
    out["release_gate"] = {
        "checklist_all_passed": check_repo.all_passed(conn, pack_id, int(pack["version"])),
        "blocked": pack["amend_state"] in ("in_progress", "failed")
        or pack["status"] == "amend_failed",
        "amend_state": pack["amend_state"],
    }
    return out


# ---- 互证 ----


def verify_pack(pack_id: int) -> dict[str, Any]:
    conn = connect()
    try:
        pack = pack_repo.get_pack(conn, pack_id)
        if not pack:
            raise HTTPException(status_code=404, detail="证据包不存在")
        items = pack_repo.list_items(conn, pack_id)
        rows = sample_repo.list_rows(conn, pack["batch_id"])
        history = pack_repo.list_fingerprint_history(conn, pack_id)
        checklist = check_repo.list_results(conn, pack_id=pack_id)
        decisions = conn.execute(
            "SELECT decision, created_at FROM release_decisions WHERE pack_id=? ORDER BY id DESC LIMIT 1",
            (pack_id,),
        ).fetchone()
        sibling_packs = [
            pid
            for pid in pack_repo.list_pack_ids_for_batch(conn, pack["batch_id"])
            if pid != pack_id
        ]

        recomputed_fp = pack_fingerprint_from_digests(
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
        active = [h for h in history if h["state"] == "active"]
        current_checklist = [c for c in checklist if c.get("pack_version") == pack["version"]]
        stale_passes = [
            c
            for c in checklist
            if c.get("pack_version") != pack["version"] and c["passed"] and not c.get("voided")
        ]

        checks = {
            "fingerprint_matches_items": recomputed_fp == pack["fingerprint"] and digests_ok,
            "item_digests_match_summaries": digests_ok,
            "item_count_matches": len(items) == pack["item_count"],
            "row_count_matches": len(rows) == pack["item_count"],
            "active_history_unique": len(active) == 1,
            "active_history_current": bool(active)
            and active[0]["version"] == pack["version"]
            and active[0]["fingerprint"] == pack["fingerprint"],
            "old_versions_archived": all(
                h["state"] != "active" for h in history if h["version"] != pack["version"]
            ),
            "checklist_present_for_current_version": len(current_checklist) > 0,
            "checklist_current_all_passed": check_repo.all_passed(
                conn, pack_id, int(pack["version"])
            ),
            "no_stale_pass": len(stale_passes) == 0,
            "amend_not_in_progress": pack["amend_state"] != "in_progress",
            "amend_not_failed": pack["amend_state"] != "failed"
            and pack["status"] != "amend_failed",
            "batch_has_no_sibling_pack": len(sibling_packs) == 0,
        }
        gate_blockers = []
        if pack["status"] == "released":
            gate_blockers.append("已放行")
        if pack["amend_state"] == "in_progress":
            gate_blockers.append("补证进行中")
        if pack["amend_state"] == "failed" or pack["status"] == "amend_failed":
            gate_blockers.append("补证失败态")
        if not checks["checklist_current_all_passed"]:
            gate_blockers.append("当前版本清单未全部通过")
        if not checks["fingerprint_matches_items"]:
            gate_blockers.append("包指纹与条目不一致")

        return {
            "pack_id": pack_id,
            "pack_no": pack["pack_no"],
            "version": pack["version"],
            "status": pack["status"],
            "amend_state": pack["amend_state"],
            "item_count": pack["item_count"],
            "stored_item_count": len(items),
            "row_count": len(rows),
            "stored_fingerprint": pack["fingerprint"],
            "recomputed_fingerprint": recomputed_fp,
            "fingerprint_history": history,
            "checklist": checklist,
            "latest_decision": dict(decisions) if decisions else None,
            "checks": checks,
            "release_allowed": len(gate_blockers) == 0,
            "release_blockers": gate_blockers,
        }
    finally:
        conn.close()
