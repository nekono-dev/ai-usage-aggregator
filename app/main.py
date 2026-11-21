"""AI利用量集計 API。

- 認証: 環境変数 APP_API_KEY（必須。未設定なら起動時エラー）。
  Authorization: Bearer <key> または X-API-Key: <key> ヘッダで照合。
  /healthz のみ認証不要。
- 起動時にコレクタのバックグラウンドタスクを開始し、
  COLLECT_INTERVAL_SEC（既定 300 秒）ごとにローカルログを走査する。
- 公開パスのプレフィックス対応（サブドメイン / パス配下の両対応）:
  サブドメインのルートで公開する場合はプレフィックスなしでそのまま動く。
  パス配下（例 https://ai.nekono.dev/usage/）で公開する場合、Cloudflare
  Tunnel はパスを剥がさないため /usage/healthz の形で届き、そのままだと
  404 になる。BasePathMiddleware がプレフィックスを検出して剥がす。
  検出の優先順位: 環境変数 APP_BASE_PATH > X-Forwarded-Prefix ヘッダ >
  パス先頭セグメントの自動判定（次のセグメントが既知ルートの場合のみ）。
- 管理 API（/v1/admin/*、同じ API キー認証）:
  稼働状態の取得、ログ収集の即時実行、DB バックアップ、メンテナンス
  モード切替、更新リクエストの記録を行う。アプリ自身はコンテナを
  操作しない。更新リクエストは CONTROL_DIR（既定 /control）の
  update-request.json に記録するだけで、実際の git pull / 再ビルド /
  再起動は updater サイドカーが実行し、結果を update-status.json に
  書き戻す。メンテナンスモードは maintenance.json で保持し、有効中は
  /healthz と /v1/admin/* 以外へ 503 を返す。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sqlite3
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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

# ビルド時に Dockerfile の ARG GIT_COMMIT から埋め込まれる
APP_VERSION = os.environ.get("APP_VERSION", "unknown")
# updater サイドカーとの制御ファイル受け渡し用ディレクトリ
CONTROL_DIR = Path(os.environ.get("CONTROL_DIR", "/control"))
STARTED_AT = time.time()
REQUEST_TTL_SEC = 600  # 更新リクエストの有効期限（updater 側と合わせる）

_LAST_COLLECT: dict = {"at": None, "result": None}

app = FastAPI(title="ai-usage-aggregator", version="0.1.0")


def _read_json_file(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def _maintenance_enabled() -> bool:
    state = _read_json_file(CONTROL_DIR / "maintenance.json")
    return bool(state and state.get("enabled"))

# アプリ自身のルートとして実在する先頭セグメント
_KNOWN_ROOT_SEGMENTS = {"healthz", "v1", "docs", "redoc", "openapi.json"}


class BasePathMiddleware:
    """公開パスプレフィックス（例 /usage）を検出して剥がす ASGI ミドルウェア。

    プレフィックスが分かった場合は scope["path"] を書き換え、
    scope["root_path"] に積む（URL 生成・ドキュメント表示用）。
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") == "http":
            prefix = self._detect_prefix(scope)
            if prefix:
                path = scope.get("path", "")
                new_path = path[len(prefix):] or "/"
                scope["path"] = new_path
                if scope.get("raw_path"):
                    scope["raw_path"] = new_path.encode("utf-8")
                scope["root_path"] = scope.get("root_path", "") + prefix
            # メンテナンスモード中は /healthz と管理 API 以外を止める。
            # プレフィックス剥がし後のパスで判定する
            effective_path = scope.get("path", "")
            if _maintenance_enabled() and effective_path != "/healthz" \
                    and not effective_path.startswith("/v1/admin"):
                body = json.dumps({"detail": "maintenance mode"}).encode("utf-8")
                await send({"type": "http.response.start", "status": 503,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"content-length", str(len(body)).encode())]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _detect_prefix(scope) -> str:
        path = scope.get("path", "")
        candidates = []
        env_prefix = os.environ.get("APP_BASE_PATH", "").strip().rstrip("/")
        if env_prefix:
            candidates.append(env_prefix if env_prefix.startswith("/") else "/" + env_prefix)
        for name, value in scope.get("headers", []):
            if name == b"x-forwarded-prefix" and value:
                fwd = value.decode("latin-1").strip().rstrip("/")
                if fwd:
                    candidates.append(fwd if fwd.startswith("/") else "/" + fwd)
                break
        for cand in candidates:
            if path == cand or path.startswith(cand + "/"):
                return cand
        # 自動判定: 先頭セグメントが既知ルートではなく、次のセグメントが
        # 既知ルートである場合に限り、先頭セグメントをプレフィックスとみなす
        parts = path.split("/")
        if len(parts) >= 3 and parts[1] not in _KNOWN_ROOT_SEGMENTS \
                and parts[2] in _KNOWN_ROOT_SEGMENTS:
            return "/" + parts[1]
        return ""


app.add_middleware(BasePathMiddleware)


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
            result = await asyncio.to_thread(collector.collect_once)
            _LAST_COLLECT.update(
                at=datetime.now(timezone.utc).isoformat(), result=result)
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


# ---------------------------------------------------------------- 管理 API

_ADMIN = [Depends(require_api_key)]


def _pending_request() -> dict | None:
    req = _read_json_file(CONTROL_DIR / "update-request.json")
    if not req:
        return None
    if time.time() - float(req.get("requested_at", 0)) > REQUEST_TTL_SEC:
        return None  # 失効したリクエストは保留扱いにしない
    return req


@app.get("/v1/admin/status", dependencies=_ADMIN)
def admin_status() -> dict:
    db_path = Path(store.DB_PATH)
    records = None
    if db_path.exists():
        with store.connect() as conn:
            records = conn.execute("SELECT COUNT(*) FROM usage_records").fetchone()[0]
    return {
        "version": APP_VERSION,
        "started_at": datetime.fromtimestamp(STARTED_AT, timezone.utc).isoformat(),
        "uptime_sec": int(time.time() - STARTED_AT),
        "db": {
            "path": str(db_path),
            "size_bytes": db_path.stat().st_size if db_path.exists() else 0,
            "records": records,
        },
        "collector": {
            "configured": bool(collector.configured_dirs()),
            "interval_sec": COLLECT_INTERVAL_SEC,
            "last_collect": _LAST_COLLECT,
        },
        "maintenance": _maintenance_enabled(),
        "update": {
            "pending_request": _pending_request(),
            "last_status": _read_json_file(CONTROL_DIR / "update-status.json"),
        },
    }


@app.post("/v1/admin/collect", dependencies=_ADMIN)
def admin_collect() -> dict:
    if not collector.configured_dirs():
        raise HTTPException(status_code=400, detail="LOG_DIRS が未設定です")
    result = collector.collect_once()
    _LAST_COLLECT.update(at=datetime.now(timezone.utc).isoformat(), result=result)
    return {"collected": result}


def _backup_dir() -> Path:
    return Path(store.DB_PATH).parent / "backups"


@app.post("/v1/admin/backup", dependencies=_ADMIN)
def admin_backup() -> dict:
    backup_dir = _backup_dir()
    backup_dir.mkdir(parents=True, exist_ok=True)
    name = "usage-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + ".db"
    dest_path = backup_dir / name
    src = store.connect()
    try:
        dest = sqlite3.connect(dest_path)
        try:
            src.backup(dest)
        finally:
            dest.close()
    finally:
        src.close()
    return {"name": name, "size_bytes": dest_path.stat().st_size,
            "created_at": datetime.now(timezone.utc).isoformat()}


@app.get("/v1/admin/backups", dependencies=_ADMIN)
def admin_backups() -> dict:
    backup_dir = _backup_dir()
    items = []
    if backup_dir.exists():
        for f in sorted(backup_dir.glob("*.db"), reverse=True):
            items.append({"name": f.name, "size_bytes": f.stat().st_size,
                          "modified_at": datetime.fromtimestamp(
                              f.stat().st_mtime, timezone.utc).isoformat()})
    return {"backups": items}


class MaintenanceIn(BaseModel):
    enabled: bool


@app.post("/v1/admin/maintenance", dependencies=_ADMIN)
def admin_maintenance(body: MaintenanceIn) -> dict:
    state = {"enabled": body.enabled,
             "updated_at": datetime.now(timezone.utc).isoformat()}
    _write_json_atomic(CONTROL_DIR / "maintenance.json", state)
    return state


@app.get("/v1/admin/maintenance", dependencies=_ADMIN)
def admin_maintenance_state() -> dict:
    return {"enabled": _maintenance_enabled()}


_ALLOWED_REF = re.compile(r"^(main|develop|v\d+\.\d+\.\d+)$")


class UpdateIn(BaseModel):
    ref: str = "main"


@app.post("/v1/admin/update", dependencies=_ADMIN)
def admin_update(body: UpdateIn) -> dict:
    if not _ALLOWED_REF.match(body.ref):
        raise HTTPException(status_code=422,
                            detail="ref は main / develop / vX.Y.Z のみ許可されます")
    pending = _pending_request()
    if pending:
        raise HTTPException(status_code=409, detail={
            "message": "更新リクエストが既に保留中です",
            "request_id": pending.get("request_id")})
    req = {
        "request_id": uuid.uuid4().hex,
        "ref": body.ref,
        "requested_at": time.time(),
        "requested_at_iso": datetime.now(timezone.utc).isoformat(),
        "current_version": APP_VERSION,
    }
    _write_json_atomic(CONTROL_DIR / "update-request.json", req)
    return {"request_id": req["request_id"], "ref": body.ref, "status": "pending"}


@app.get("/v1/admin/update", dependencies=_ADMIN)
def admin_update_state() -> dict:
    return {
        "pending_request": _pending_request(),
        "last_status": _read_json_file(CONTROL_DIR / "update-status.json"),
    }
