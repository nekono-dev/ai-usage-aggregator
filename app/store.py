"""SQLite による使用量レコードの蓄積と集計。

DB パスは環境変数 DB_PATH（既定 /data/usage.db）。
source_id の UNIQUE 制約 + INSERT OR IGNORE で、同じログ行を何度走査しても
二重登録されない（コレクタの冪等性はここで担保する）。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "/data/usage.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS usage_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL,
    agent TEXT NOT NULL,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT 'unknown',
    billing TEXT NOT NULL DEFAULT 'unknown',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_occurred_at ON usage_records (occurred_at);
"""

BILLING_KINDS = ("api", "subscription", "unknown")


def connect() -> sqlite3.Connection:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        # 旧スキーマ（billing 列なし）からの移行
        cols = {r[1] for r in conn.execute("PRAGMA table_info(usage_records)")}
        if "billing" not in cols:
            conn.execute("ALTER TABLE usage_records ADD COLUMN billing TEXT NOT NULL DEFAULT 'unknown'")


def insert_records(records: list[dict]) -> tuple[int, int]:
    """レコードを一括登録し、(inserted, skipped) 件数を返す。"""
    inserted = 0
    with connect() as conn:
        for r in records:
            billing = r.get("billing", "unknown")
            if billing not in BILLING_KINDS:
                billing = "unknown"
            cur = conn.execute(
                """INSERT OR IGNORE INTO usage_records
                   (source_id, occurred_at, agent, provider, model, billing,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
                   VALUES (:source_id, :occurred_at, :agent, :provider, :model, :billing,
                           :input_tokens, :output_tokens, :cache_read_tokens, :cache_write_tokens)""",
                {
                    "source_id": r["source_id"],
                    "occurred_at": r["occurred_at"],
                    "agent": r["agent"],
                    "provider": r.get("provider", ""),
                    "model": r.get("model") or "unknown",
                    "billing": billing,
                    "input_tokens": int(r.get("input_tokens", 0)),
                    "output_tokens": int(r.get("output_tokens", 0)),
                    "cache_read_tokens": int(r.get("cache_read_tokens", 0)),
                    "cache_write_tokens": int(r.get("cache_write_tokens", 0)),
                },
            )
            inserted += cur.rowcount
    return inserted, len(records) - inserted


_GROUP_EXPR = {
    "day": "substr(occurred_at, 1, 10)",
    "model": "model",
    "agent": "agent",
}


def aggregate(date_from: str, date_to: str, group_by: str) -> list[dict]:
    """期間内を group_by で集計する。

    コスト計算のために内部では (グループキー, model, billing) 単位で集計
    して返す。呼び出し側でモデル別単価を掛けてグループ単位に合算する。
    日付は occurred_at の先頭10文字（UTC の暦日）で比較する。
    """
    expr = _GROUP_EXPR[group_by]
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT {expr} AS grp, model, billing,
                       SUM(input_tokens) AS input_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(cache_read_tokens) AS cache_read_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens,
                       COUNT(*) AS records
                FROM usage_records
                WHERE substr(occurred_at, 1, 10) BETWEEN ? AND ?
                GROUP BY grp, model, billing
                ORDER BY grp, model, billing""",
            (date_from, date_to),
        ).fetchall()
    return [dict(r) for r in rows]


def totals_by_model(date_from: str, date_to: str) -> list[dict]:
    """期間内のモデル別・課金種別合計（summary 用）。"""
    with connect() as conn:
        rows = conn.execute(
            """SELECT model, billing,
                      SUM(input_tokens) AS input_tokens,
                      SUM(output_tokens) AS output_tokens,
                      SUM(cache_read_tokens) AS cache_read_tokens,
                      SUM(cache_write_tokens) AS cache_write_tokens,
                      COUNT(*) AS records
               FROM usage_records
               WHERE substr(occurred_at, 1, 10) BETWEEN ? AND ?
               GROUP BY model, billing
               ORDER BY model, billing""",
            (date_from, date_to),
        ).fetchall()
    return [dict(r) for r in rows]
