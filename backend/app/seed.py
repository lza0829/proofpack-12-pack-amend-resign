from __future__ import annotations

from datetime import datetime, timezone

from app.db import connect

# 举证对象种子
SUBJECT_SEEDS = [
    {
        "code": "store_dayend",
        "title": "门店日结抽检",
        "domain": "store",
        "fixture": "store_dayend.json",
        "sample_rate_default": 0.3,
        "enabled": 1,
    },
    {
        "code": "channel_settle",
        "title": "渠道结算抽检",
        "domain": "channel",
        "fixture": "channel_settle.json",
        "sample_rate_default": 0.3,
        "enabled": 1,
    },
    {
        "code": "price_change",
        "title": "价目变更核证",
        "domain": "price",
        "fixture": "price_change.json",
        "sample_rate_default": 0.3,
        "enabled": 1,
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    """Add a column to an existing table when the database predates it."""
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(
            """
            -- 举证对象
            CREATE TABLE IF NOT EXISTS subjects(
                id INTEGER PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                domain TEXT NOT NULL,
                fixture TEXT NOT NULL,
                sample_rate_default REAL NOT NULL DEFAULT 0.3,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );
            -- 采样批次
            CREATE TABLE IF NOT EXISTS sample_batches(
                id INTEGER PRIMARY KEY,
                batch_no TEXT NOT NULL UNIQUE,
                subject_id INTEGER NOT NULL,
                seed TEXT NOT NULL,
                rate REAL NOT NULL,
                source_count INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(subject_id) REFERENCES subjects(id)
            );
            -- 样本行
            CREATE TABLE IF NOT EXISTS sample_rows(
                id INTEGER PRIMARY KEY,
                batch_id INTEGER NOT NULL,
                business_key TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                event_ts TEXT,
                revision INTEGER,
                origin TEXT NOT NULL DEFAULT 'sampled',
                FOREIGN KEY(batch_id) REFERENCES sample_batches(id)
            );
            -- 证据包
            CREATE TABLE IF NOT EXISTS evidence_packs(
                id INTEGER PRIMARY KEY,
                pack_no TEXT NOT NULL UNIQUE,
                batch_id INTEGER NOT NULL,
                subject_id INTEGER NOT NULL,
                item_count INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                amend_state TEXT NOT NULL DEFAULT 'none',
                created_at TEXT NOT NULL,
                FOREIGN KEY(batch_id) REFERENCES sample_batches(id),
                FOREIGN KEY(subject_id) REFERENCES subjects(id)
            );
            -- 证据包条目
            CREATE TABLE IF NOT EXISTS pack_items(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER NOT NULL,
                business_key TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                item_fingerprint TEXT NOT NULL,
                FOREIGN KEY(pack_id) REFERENCES evidence_packs(id)
            );
            -- 校验清单结果
            CREATE TABLE IF NOT EXISTS checklist_results(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER NOT NULL,
                pack_version INTEGER NOT NULL DEFAULT 1,
                check_code TEXT NOT NULL,
                check_title TEXT NOT NULL,
                passed INTEGER NOT NULL,
                voided INTEGER NOT NULL DEFAULT 0,
                detail TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(pack_id) REFERENCES evidence_packs(id)
            );
            -- 放行决策
            CREATE TABLE IF NOT EXISTS release_decisions(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(pack_id) REFERENCES evidence_packs(id)
            );
            -- 包指纹历史（每次重签都留档；superseded=历史，voided=作废仅审计）
            CREATE TABLE IF NOT EXISTS pack_fingerprint_history(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                fingerprint TEXT NOT NULL,
                item_count INTEGER NOT NULL,
                state TEXT NOT NULL,
                source TEXT NOT NULL,
                amendment_id INTEGER,
                created_at TEXT NOT NULL,
                FOREIGN KEY(pack_id) REFERENCES evidence_packs(id)
            );
            -- 补证单（发起 -> 提交/撤销；失败按所选命运落账）
            CREATE TABLE IF NOT EXISTS pack_amendments(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER NOT NULL,
                strategy TEXT NOT NULL,
                failure_policy TEXT NOT NULL,
                state TEXT NOT NULL,
                ops_json TEXT NOT NULL,
                before_version INTEGER,
                after_version INTEGER,
                before_count INTEGER,
                after_count INTEGER,
                before_fingerprint TEXT,
                after_fingerprint TEXT,
                before_status TEXT,
                before_amend_state TEXT,
                after_status TEXT,
                applied INTEGER NOT NULL DEFAULT 0,
                snapshot_json TEXT,
                checklist_rerun_passed INTEGER,
                failure_reason TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(pack_id) REFERENCES evidence_packs(id)
            );
            -- 设置
            CREATE TABLE IF NOT EXISTS settings(
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            -- 影子试跑（空挂，后题用）
            CREATE TABLE IF NOT EXISTS shadow_runs(
                id INTEGER PRIMARY KEY,
                pack_id INTEGER,
                rule_version_id INTEGER,
                status TEXT,
                note TEXT,
                created_at TEXT
            );
            -- 规则版本（空挂，后题用）
            CREATE TABLE IF NOT EXISTS rule_versions(
                id INTEGER PRIMARY KEY,
                code TEXT,
                title TEXT,
                payload_json TEXT,
                active INTEGER DEFAULT 0,
                created_at TEXT
            );
            """
        )
        # 老库迁移：补齐补证所需列
        _ensure_column(conn, "evidence_packs", "version", "version INTEGER NOT NULL DEFAULT 1")
        _ensure_column(
            conn,
            "evidence_packs",
            "amend_state",
            "amend_state TEXT NOT NULL DEFAULT 'none'",
        )
        _ensure_column(
            conn,
            "checklist_results",
            "pack_version",
            "pack_version INTEGER NOT NULL DEFAULT 1",
        )
        _ensure_column(
            conn,
            "checklist_results",
            "voided",
            "voided INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(
            conn,
            "sample_rows",
            "origin",
            "origin TEXT NOT NULL DEFAULT 'sampled'",
        )
        # 老包补一条版本 1 的指纹历史
        conn.execute(
            """
            INSERT INTO pack_fingerprint_history(
                pack_id, version, fingerprint, item_count, state, source, amendment_id, created_at
            )
            SELECT p.id, p.version, p.fingerprint, p.item_count, 'active', 'build', NULL, p.created_at
            FROM evidence_packs p
            WHERE NOT EXISTS (
                SELECT 1 FROM pack_fingerprint_history h WHERE h.pack_id = p.id
            )
            """
        )
        # 同一证据包同时只允许一张未结补证单（草稿/提交中）
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_pack_amendment_open
            ON pack_amendments(pack_id)
            WHERE state IN ('draft','submitting')
            """
        )
        if conn.execute("SELECT COUNT(*) c FROM subjects").fetchone()["c"] == 0:
            now = _now()
            for s in SUBJECT_SEEDS:
                conn.execute(
                    """
                    INSERT INTO subjects(
                        code, title, domain, fixture, sample_rate_default, enabled, created_at
                    ) VALUES (?,?,?,?,?,?,?)
                    """,
                    (
                        s["code"],
                        s["title"],
                        s["domain"],
                        s["fixture"],
                        s["sample_rate_default"],
                        s["enabled"],
                        now,
                    ),
                )
        if conn.execute("SELECT COUNT(*) c FROM settings").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("default_sample_rate", "0.3"),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("require_checklist_pass", "1"),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?)",
                ("sample_mode", "seeded"),
            )
        conn.commit()
    finally:
        conn.close()
