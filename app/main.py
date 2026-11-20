"""AI利用量集計 API。

- 認証: 環境変数 APP_API_KEY（必須。未設定なら起動時エラー）。
  Authorization: Bearer <key> または X-API-Key: <key> ヘッダで照合。
  /healthz のみ認証不要。
- 起動時にコレクタのバックグラウンドタスクを開始し、
  COLLECT_INTERVAL_SEC（既定 300 秒）ごとにローカルログを走査する。
"""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import date, timedelta

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from . import collector, prices, store

COST_NOTE = ("api_equivalent_cost_usd は API リスト価格で換算した参考値。"
             "billing=subscription の利用はサブスクリプション契約の枠消費であり、"
             "API 使用量・API 請求ではない。api_cost_usd は billing=api の分のみの換算。")

API_KEY = os.environ.get("APP_API_KEY", "")
if not API_KEY:
    raise RuntimeError("APP_API_KEY が未設定です。環境変数で API キーを設定してください。")

COLLECT_INTERVAL_SEC = int(os.environ.get("COLLECT_INTERVAL_SEC", "300"))

app = FastAPI(title="ai-usage-aggregator", version="0.1.0")


def require_api_key(authorization: str | None = Header(default=None),
                    x_api_key: str | None = Header(default=None)) -> None:
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[len("Bearer "):]
    elif x_api_key:
        token = x_api_key
    if token is None or not secrets.compare_digest(token, API_KEY):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


class UsageRecordIn(BaseModel):
    source_id: str
    occurred_at: str  # ISO8601
    agent: str
    provider: str = ""
    model: str = "unknown"
    # api = API課金 / subscription = サブスク枠の消費 / unknown = 不明
    billing: str = Field(default="unknown", pattern="^(api|subscription|unknown)$")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


@app.on_event("startup")
async def _startup() -> None:
    store.init_db()
    if collector.configured_dirs():
        asyncio.create_task(_collect_loop())


async def _collect_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(collector.collect_once)
        except Exception:
            pass  # コレクタの失敗で API 本体を落とさない
        await asyncio.sleep(COLLECT_INTERVAL_SEC)


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/v1/usage/records", dependencies=[Depends(require_api_key)])
def post_records(body: UsageRecordIn | list[UsageRecordIn]) -> dict:
    items = body if isinstance(body, list) else [body]
    records = [item.model_dump() for item in items]
    inserted, skipped = store.insert_records(records)
    return {"inserted": inserted, "skipped": skipped}


def _with_cost(row: dict) -> dict:
    cost, known = prices.estimate_cost(
        row["model"], row["input_tokens"], row["output_tokens"],
        row["cache_read_tokens"], row["cache_write_tokens"])
    row = dict(row)
    row["total_tokens"] = (row["input_tokens"] + row["output_tokens"]
                           + row["cache_read_tokens"] + row["cache_write_tokens"])
    # API リスト価格での換算値。billing=subscription の場合は実際の請求ではない
    row["api_equivalent_cost_usd"] = round(cost, 6)
    row["price_known"] = known  # False のモデルは単価不明（コスト 0 として扱う）
    return row


@app.get("/v1/report", dependencies=[Depends(require_api_key)])
def report(date_from: str | None = Query(default=None, alias="from"),
           date_to: str | None = Query(default=None, alias="to"),
           group_by: str = Query(default="day", pattern="^(day|model|agent)$")) -> dict:
    today = date.today()
    d_to = date_to or today.isoformat()
    d_from = date_from or (today - timedelta(days=29)).isoformat()

    # (グループ, model, billing) 単位の行を、モデル別単価でコスト換算してから合算する
    grouped: dict[str, dict] = {}
    for row in store.aggregate(d_from, d_to, group_by):
        cost, _known = prices.estimate_cost(
            row["model"], row["input_tokens"], row["output_tokens"],
            row["cache_read_tokens"], row["cache_write_tokens"])
        g = grouped.setdefault(row["grp"], {
            "group": row["grp"], "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "records": 0, "api_cost_usd": 0.0, "api_equivalent_cost_usd": 0.0,
            "by_billing": {},
        })
        for key in ("input_tokens", "output_tokens", "cache_read_tokens",
                    "cache_write_tokens", "records"):
            g[key] += row[key]
        g["api_equivalent_cost_usd"] += cost
        if row["billing"] == "api":
            g["api_cost_usd"] += cost
        b = g["by_billing"].setdefault(row["billing"], 0)
        g["by_billing"][row["billing"]] = b + row["input_tokens"] + row["output_tokens"] \
            + row["cache_read_tokens"] + row["cache_write_tokens"]
    groups = []
    for g in grouped.values():
        g["total_tokens"] = (g["input_tokens"] + g["output_tokens"]
                             + g["cache_read_tokens"] + g["cache_write_tokens"])
        g["api_cost_usd"] = round(g["api_cost_usd"], 6)
        g["api_equivalent_cost_usd"] = round(g["api_equivalent_cost_usd"], 6)
        groups.append(g)
    return {"from": d_from, "to": d_to, "group_by": group_by,
            "note": COST_NOTE, "groups": groups}


@app.get("/v1/summary", dependencies=[Depends(require_api_key)])
def summary() -> dict:
    today = date.today()

    def window(days: int) -> dict:
        d_from = (today - timedelta(days=days - 1)).isoformat()
        d_to = today.isoformat()
        models = [_with_cost(r) for r in store.totals_by_model(d_from, d_to)]
        total = {
            "input_tokens": sum(m["input_tokens"] for m in models),
            "output_tokens": sum(m["output_tokens"] for m in models),
            "cache_read_tokens": sum(m["cache_read_tokens"] for m in models),
            "cache_write_tokens": sum(m["cache_write_tokens"] for m in models),
            "records": sum(m["records"] for m in models),
        }
        total["total_tokens"] = (total["input_tokens"] + total["output_tokens"]
                                 + total["cache_read_tokens"] + total["cache_write_tokens"])
        total["api_equivalent_cost_usd"] = round(
            sum(m["api_equivalent_cost_usd"] for m in models), 6)
        total["api_cost_usd"] = round(
            sum(m["api_equivalent_cost_usd"] for m in models if m["billing"] == "api"), 6)
        return {"from": d_from, "to": d_to, "total": total, "by_model": models}

    return {"note": COST_NOTE, "last_7d": window(7), "last_30d": window(30)}
