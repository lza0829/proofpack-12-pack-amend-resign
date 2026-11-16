"""补证（pack amend & re-sign）回归测试。

以服务层直测主链：采样 -> 打包 -> 清单 -> 补证 -> 复检 -> 放行/驳回。
每个用例使用独立的临时 DATA_DIR，避免相互污染。
"""

from __future__ import annotations

import os
import tempfile

import pytest

_TMP = tempfile.mkdtemp(prefix="proofpack-test-")
os.environ.setdefault("DATA_DIR", _TMP)

from app.db import connect  # noqa: E402
from app.seed import init_db  # noqa: E402
from app.services import (  # noqa: E402
    amendment_service,
    checklist_service,
    pack_service,
    release_service,
    sample_service,
)
from app.services import checklist_service as checklist_mod  # noqa: E402


@pytest.fixture()
def fresh_db():
    conn = connect()
    try:
        conn.executescript(
            """
            DROP TABLE IF EXISTS pack_fingerprint_history;
            DROP TABLE IF EXISTS pack_amendments;
            DROP TABLE IF EXISTS checklist_results;
            DROP TABLE IF EXISTS release_decisions;
            DROP TABLE IF EXISTS pack_items;
            DROP TABLE IF EXISTS evidence_packs;
            DROP TABLE IF EXISTS sample_rows;
            DROP TABLE IF EXISTS sample_batches;
            DROP TABLE IF EXISTS shadow_runs;
            DROP TABLE IF EXISTS rule_versions;
            DROP TABLE IF EXISTS settings;
            DROP TABLE IF EXISTS subjects;
            """
        )
        conn.commit()
    finally:
        conn.close()
    init_db()


def _ready_pack(subject_id: int = 1, seed: str = "test-seed") -> dict:
    batch = sample_service.run_sample(subject_id, rate=0.3, seed=seed)
    pack = pack_service.build_pack(batch["id"])
    checklist_service.run_checklist(pack["id"])
    return pack


def _keys(pack_id: int):
    view = amendment_service.pool_view(pack_id)
    inside = [x["business_key"] for x in view["allowed_pool"] if x["in_pack"]]
    outside = [x["business_key"] for x in view["allowed_pool"] if not x["in_pack"]]
    return inside, outside


def _amend(pack_id, strategy, policy, ops):
    created = amendment_service.create_amendment(
        pack_id=pack_id, strategy=strategy, failure_policy=policy, ops=ops
    )
    return amendment_service.submit_amendment(created["id"])


# ---- 成功路径 ----


def test_incremental_amend_appends_and_resigns(fresh_db):
    pack = _ready_pack()
    inside, outside = _keys(pack["id"])

    result = _amend(
        pack["id"], "incremental", "rollback", [{"op": "add", "business_key": outside[0]}]
    )

    assert result["state"] == "succeeded"
    assert result["after_count"] == result["before_count"] + 1
    assert result["after_version"] == result["before_version"] + 1
    assert result["after_fingerprint"] != result["before_fingerprint"]
    states = [(h["version"], h["state"]) for h in result["fingerprint_history"]]
    assert states == [(1, "superseded"), (2, "active")]
    assert result["checklist_rerun_passed"] == 1
    assert result["pack"]["status"] == "checked_ok"
    assert result["pack"]["amend_state"] == "none"


def test_resign_replaces_and_voids_old_version(fresh_db):
    pack = _ready_pack(2)
    inside, outside = _keys(pack["id"])

    result = _amend(
        pack["id"],
        "resign",
        "rollback",
        [{"op": "replace", "from_business_key": inside[0], "business_key": outside[0]}],
    )

    assert result["state"] == "succeeded"
    assert result["after_count"] == result["before_count"]  # 替换不改变条数
    states = [(h["version"], h["state"]) for h in result["fingerprint_history"]]
    assert states == [(1, "voided"), (2, "active")]
    keys = {it["business_key"] for it in pack_service.get_pack(pack["id"])["items"]}
    assert outside[0] in keys and inside[0] not in keys


def test_release_after_success(fresh_db):
    pack = _ready_pack()
    _, outside = _keys(pack["id"])
    _amend(pack["id"], "incremental", "rollback", [{"op": "add", "business_key": outside[0]}])
    decision = release_service.decide(pack["id"], "pass")
    assert decision["decision"] == "pass"
    assert decision["fingerprint_verified"] is True


# ---- 门禁 ----


def test_release_blocked_while_amend_in_progress(fresh_db):
    pack = _ready_pack()
    _, outside = _keys(pack["id"])
    amendment_service.create_amendment(
        pack_id=pack["id"],
        strategy="incremental",
        failure_policy="rollback",
        ops=[{"op": "add", "business_key": outside[0]}],
    )
    with pytest.raises(Exception) as exc:
        release_service.decide(pack["id"], "pass")
    assert "补证进行中" in str(exc.value)


def test_released_pack_cannot_be_amended(fresh_db):
    pack = _ready_pack()
    release_service.decide(pack["id"], "pass")
    _, outside = _keys(pack["id"])
    with pytest.raises(Exception) as exc:
        amendment_service.create_amendment(
            pack_id=pack["id"],
            strategy="incremental",
            failure_policy="rollback",
            ops=[{"op": "add", "business_key": outside[0]}],
        )
    assert "已放行" in str(exc.value)


def test_item_only_in_pool_can_be_added(fresh_db):
    pack = _ready_pack()
    result = _amend(
        pack["id"], "incremental", "rollback", [{"op": "add", "business_key": "NOT-IN-POOL"}]
    )
    assert result["state"] == "failed"
    assert "允许的样本集合" in result["failure_reason"]


# ---- 失败命运 ----


def test_rollback_policy_restores_pre_amend_state(fresh_db):
    pack = _ready_pack()
    before = amendment_service.verify_pack(pack["id"])
    result = _amend(
        pack["id"], "incremental", "rollback", [{"op": "add", "business_key": "NOT-IN-POOL"}]
    )
    assert result["state"] == "failed"
    assert result["pack"]["amend_state"] == "none"
    assert result["pack"]["version"] == 1
    assert result["pack"]["fingerprint"] == before["stored_fingerprint"]
    after = amendment_service.verify_pack(pack["id"])
    assert after["release_allowed"] is True


def test_failed_state_blocks_release_until_cancelled(fresh_db):
    pack = _ready_pack()
    created = amendment_service.create_amendment(
        pack_id=pack["id"],
        strategy="incremental",
        failure_policy="failed_state",
        ops=[{"op": "add", "business_key": "NOT-IN-POOL"}],
    )
    amendment_service.submit_amendment(created["id"])
    assert amendment_service.get_amendment(created["id"])["state"] == "failed"
    with pytest.raises(Exception) as exc:
        release_service.decide(pack["id"], "pass")
    assert "补证失败态" in str(exc.value)

    amendment_service.cancel_amendment(created["id"])
    assert amendment_service.verify_pack(pack["id"])["release_allowed"] is True


def test_post_apply_failure_rolls_back_completely(fresh_db, monkeypatch):
    pack = _ready_pack()
    _, outside = _keys(pack["id"])
    before = amendment_service.verify_pack(pack["id"])

    real = checklist_mod.evaluate_on_conn

    def failing(conn, pack_row):
        outcome = dict(real(conn, pack_row))
        outcome["all_passed"] = False
        return outcome

    monkeypatch.setattr(checklist_mod, "evaluate_on_conn", failing)

    result = _amend(
        pack["id"], "incremental", "rollback", [{"op": "add", "business_key": outside[0]}]
    )
    assert result["state"] == "failed"
    assert result["pack"]["version"] == 1
    assert result["pack"]["item_count"] == before["stored_item_count"]
    assert result["pack"]["fingerprint"] == before["stored_fingerprint"]
    history = [(h["version"], h["state"]) for h in result["fingerprint_history"]]
    assert history == [(1, "active")]

    monkeypatch.setattr(checklist_mod, "evaluate_on_conn", real)
    assert amendment_service.verify_pack(pack["id"])["release_allowed"] is True


def test_post_apply_failure_failed_state_is_cancellable(fresh_db, monkeypatch):
    pack = _ready_pack()
    _, outside = _keys(pack["id"])
    real = checklist_mod.evaluate_on_conn

    def failing(conn, pack_row):
        outcome = dict(real(conn, pack_row))
        outcome["all_passed"] = False
        return outcome

    monkeypatch.setattr(checklist_mod, "evaluate_on_conn", failing)
    created = amendment_service.create_amendment(
        pack_id=pack["id"],
        strategy="incremental",
        failure_policy="failed_state",
        ops=[{"op": "add", "business_key": outside[0]}],
    )
    result = amendment_service.submit_amendment(created["id"])
    assert result["state"] == "failed"
    assert result["pack"]["amend_state"] == "failed"
    assert result["pack"]["version"] == 2
    assert result["pack"]["status"] == "amend_failed"

    monkeypatch.setattr(checklist_mod, "evaluate_on_conn", real)
    cancelled = amendment_service.cancel_amendment(created["id"])
    assert cancelled["pack"]["version"] == 1
    assert cancelled["pack"]["amend_state"] == "none"
    assert [(h["version"], h["state"]) for h in cancelled["fingerprint_history"]] == [
        (1, "active")
    ]


# ---- 互证 ----


def test_verify_detects_unsigned_item_change(fresh_db):
    pack = _ready_pack()
    conn = connect()
    try:
        conn.execute(
            "UPDATE pack_items SET summary_json=? WHERE pack_id=? AND id=("
            "SELECT MIN(id) FROM pack_items WHERE pack_id=?)",
            ('{"title":"篡改","amount":1,"qty":1,"event_ts":"x","revision":1}', pack["id"], pack["id"]),
        )
        conn.commit()
    finally:
        conn.close()

    verify = amendment_service.verify_pack(pack["id"])
    assert verify["checks"]["fingerprint_matches_items"] is False
    assert verify["release_allowed"] is False
    with pytest.raises(Exception) as exc:
        release_service.decide(pack["id"], "pass")
    assert "指纹与条目不一致" in str(exc.value)


def test_verify_cross_checks_after_chain(fresh_db):
    pack = _ready_pack(2)
    _, outside = _keys(pack["id"])
    _amend(pack["id"], "incremental", "rollback", [{"op": "add", "business_key": outside[0]}])
    _, outside2 = _keys(pack["id"])
    _amend(pack["id"], "resign", "rollback", [{"op": "add", "business_key": outside2[0]}])

    verify = amendment_service.verify_pack(pack["id"])
    assert verify["version"] == 3
    assert verify["item_count"] == verify["stored_item_count"] == verify["row_count"]
    assert verify["checks"]["active_history_unique"] is True
    assert verify["checks"]["old_versions_archived"] is True
    assert verify["checks"]["no_stale_pass"] is True
    assert verify["release_allowed"] is True


def test_old_checklist_pass_is_voided_after_amend(fresh_db):
    pack = _ready_pack()
    _, outside = _keys(pack["id"])
    _amend(pack["id"], "incremental", "rollback", [{"op": "add", "business_key": outside[0]}])
    verify = amendment_service.verify_pack(pack["id"])
    old = [c for c in verify["checklist"] if c["pack_version"] == 1]
    assert old and all(c["voided"] == 1 for c in old)
    assert not [c for c in verify["checklist"] if c["pack_version"] != 2 and c["passed"] and not c["voided"]]


def test_second_pack_from_same_batch_blocks_amend(fresh_db):
    batch = sample_service.run_sample(1, rate=0.3, seed="dup")
    pack_service.build_pack(batch["id"])
    second = pack_service.build_pack(batch["id"])
    checklist_service.run_checklist(second["id"])
    _, outside = _keys(second["id"])
    with pytest.raises(Exception) as exc:
        amendment_service.create_amendment(
            pack_id=second["id"],
            strategy="incremental",
            failure_policy="rollback",
            ops=[{"op": "add", "business_key": outside[0]}],
        )
    assert "多个证据包" in str(exc.value)
