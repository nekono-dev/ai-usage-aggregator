"""プロバイダ公式 Admin API からの API 利用実績の同期。

ローカルログのコレクタ（サブスクリプション利用の計測）とは別プレーン。
API 課金の利用はローカルログにも公式 API にも現れるため、二重計上を
避けるべく保存先を分け（store の api_usage_daily / api_cost_daily）、
合算はしない。公式側が API 利用の正本。

認証情報は環境変数 ANTHROPIC_ADMIN_KEY / OPENAI_ADMIN_KEY。
未設定のプロバイダは同期対象外（LOG_DIRS と同じ考え方）。
キーはどのレスポンスにも含めない。

同期ウィンドウは直近 SYNC_DAYS 日（既定35日）。公式コストは約24時間
遅れて確定し、約30日遡って改訂されるため、毎回取り直して upsert する。
日付はプロバイダのバケット（UTC の暦日）で格納する。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

from . import store

ANTHROPIC_ADMIN_KEY = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
OPENAI_ADMIN_KEY = os.environ.get("OPENAI_ADMIN_KEY", "")
ANTHROPIC_API_BASE = os.environ.get("ANTHROPIC_API_BASE", "https://api.anthropic.com").rstrip("/")
OPENAI_API_BASE = os.environ.get("OPENAI_API_BASE", "https://api.openai.com").rstrip("/")
SYNC_DAYS = int(os.environ.get("PROVIDER_SYNC_DAYS", "35"))


class ProviderError(Exception):
    pass


def configured_providers() -> list[str]:
    providers = []
    if ANTHROPIC_ADMIN_KEY:
        providers.append("anthropic")
    if OPENAI_ADMIN_KEY:
        providers.append("openai")
    return providers


def _get_json(url: str, headers: dict) -> dict:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:200]
        raise ProviderError(f"HTTP {e.code}: {body}") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ProviderError(f"connection error: {e}") from e


def _window(days: int) -> tuple[datetime, datetime]:
    """同期対象の [開始, 終了) を UTC の日境界で返す。"""
    today = datetime.now(timezone.utc).date()
    start = datetime.combine(today - timedelta(days=days - 1),
                             datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.combine(today + timedelta(days=1),
                           datetime.min.time(), tzinfo=timezone.utc)
    return start, end


def _rfc3339(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------- Anthropic

def _anthropic_headers() -> dict:
    return {"x-api-key": ANTHROPIC_ADMIN_KEY,
            "anthropic-version": "2023-06-01"}


def _anthropic_paged(path: str, params: dict) -> list[dict]:
    """data 配列をページングしながら全件返す。"""
    items: list[dict] = []
    page = None
    while True:
        q = dict(params)
        if page:
            q["page"] = page
        payload = _get_json(f"{ANTHROPIC_API_BASE}{path}?{urllib.parse.urlencode(q)}",
                            _anthropic_headers())
        items.extend(payload.get("data", []))
        if payload.get("has_more") and payload.get("next_page"):
            page = payload["next_page"]
        else:
            return items


def sync_anthropic(days: int) -> dict:
    start, end = _window(days)
    params = {"starting_at": _rfc3339(start), "ending_at": _rfc3339(end),
              "bucket_width": "1d", "group_by[]": "model"}
    usage_rows = []
    for bucket in _anthropic_paged("/v1/organizations/usage_report/messages", params):
        day = str(bucket.get("starting_at", ""))[:10]
        if not day:
            continue
        for r in bucket.get("results", []):
            cache_creation = r.get("cache_creation") or {}
            if isinstance(cache_creation, dict):
                cache_write = int(cache_creation.get("ephemeral_5m_input_tokens", 0)) \
                    + int(cache_creation.get("ephemeral_1h_input_tokens", 0))
            else:
                cache_write = int(r.get("cache_creation_input_tokens", 0))
            usage_rows.append({
                "provider": "anthropic", "date": day,
                "model": r.get("model") or "unknown",
                "input_tokens": int(r.get("uncached_input_tokens", 0)),
                "output_tokens": int(r.get("output_tokens", 0)),
                "cache_read_tokens": int(r.get("cache_read_input_tokens", 0)),
                "cache_write_tokens": cache_write,
                "requests": 0,  # usage_report はモデル別リクエスト数を返さない
            })
    cost_params = {"starting_at": _rfc3339(start), "ending_at": _rfc3339(end),
                   "group_by[]": "description"}
    cost_rows = []
    for bucket in _anthropic_paged("/v1/organizations/cost_report", cost_params):
        day = str(bucket.get("starting_at", ""))[:10]
        if not day:
            continue
        for r in bucket.get("results", []):
            if str(r.get("currency", "USD")).upper() != "USD":
                continue
            # amount は最小通貨単位（セント）の10進文字列
            cost_rows.append({
                "provider": "anthropic", "date": day,
                "group_key": r.get("description") or "",
                "amount_usd": float(r.get("amount", "0")) / 100.0,
            })
    synced_at = datetime.now(timezone.utc).isoformat()
    return {"usage_rows": store.upsert_api_usage(usage_rows, synced_at),
            "cost_rows": store.upsert_api_cost(cost_rows, synced_at)}


# ---------------------------------------------------------------- OpenAI

def _openai_headers() -> dict:
    return {"Authorization": f"Bearer {OPENAI_ADMIN_KEY}"}


def _openai_paged(path: str, params: dict) -> list[dict]:
    items: list[dict] = []
    page = None
    while True:
        q = dict(params)
        if page:
            q["page"] = page
        payload = _get_json(f"{OPENAI_API_BASE}{path}?{urllib.parse.urlencode(q)}",
                            _openai_headers())
        items.extend(payload.get("data", []))
        if payload.get("has_more") and payload.get("next_page"):
            page = payload["next_page"]
        else:
            return items


def sync_openai(days: int) -> dict:
    start, end = _window(days)
    params = {"start_time": int(start.timestamp()), "end_time": int(end.timestamp()),
              "bucket_width": "1d", "group_by": "model"}
    usage_rows = []
    for bucket in _openai_paged("/v1/organization/usage/completions", params):
        day = datetime.fromtimestamp(bucket.get("start_time", 0),
                                     timezone.utc).strftime("%Y-%m-%d")
        for r in bucket.get("results", []):
            cached = int(r.get("input_cached_tokens", 0))
            # OpenAI の input_tokens はキャッシュ読込分を含むため、
            # ローカル計測の定義（input はキャッシュ別）に合わせて差し引く
            usage_rows.append({
                "provider": "openai", "date": day,
                "model": r.get("model") or "unknown",
                "input_tokens": max(int(r.get("input_tokens", 0)) - cached, 0),
                "output_tokens": int(r.get("output_tokens", 0)),
                "cache_read_tokens": cached,
                "cache_write_tokens": int(r.get("input_cache_write_tokens", 0)),
                "requests": int(r.get("num_model_requests", 0)),
            })
    cost_params = {"start_time": int(start.timestamp()),
                   "end_time": int(end.timestamp()),
                   "bucket_width": "1d", "group_by": "line_item"}
    cost_rows = []
    for bucket in _openai_paged("/v1/organization/costs", cost_params):
        day = datetime.fromtimestamp(bucket.get("start_time", 0),
                                     timezone.utc).strftime("%Y-%m-%d")
        for r in bucket.get("results", []):
            amount = r.get("amount") or {}
            if str(amount.get("currency", "usd")).lower() != "usd":
                continue
            cost_rows.append({
                "provider": "openai", "date": day,
                "group_key": r.get("line_item") or "",
                "amount_usd": float(amount.get("value", 0)),
            })
    synced_at = datetime.now(timezone.utc).isoformat()
    return {"usage_rows": store.upsert_api_usage(usage_rows, synced_at),
            "cost_rows": store.upsert_api_cost(cost_rows, synced_at)}


# ------------------------------------------------------------------ 本体

_SYNCERS = {"anthropic": sync_anthropic, "openai": sync_openai}


def sync_once(days: int | None = None) -> dict:
    """設定済みプロバイダを同期し、プロバイダ別の結果を返す。例外は投げない。"""
    results = {}
    for provider in configured_providers():
        try:
            counts = _SYNCERS[provider](days if days is not None else SYNC_DAYS)
            results[provider] = {"ok": True, **counts}
        except ProviderError as e:
            results[provider] = {"ok": False, "error": str(e)}
        except Exception as e:  # 想定外でも他プロバイダと本体を止めない
            results[provider] = {"ok": False, "error": f"unexpected: {e}"}
    return results
