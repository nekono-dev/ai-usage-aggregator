"""ローカルセッションログの走査パーサ。

環境変数 LOG_DIRS にカンマ区切りで「agent名=パス」を指定する。
例: LOG_DIRS=claude-code=/logs/claude,codex=/logs/codex,pi=/logs/pi

冪等性は処理済み行数の保持ではなく、レコードの source_id の重複排除
（store 側の UNIQUE 制約 + INSERT OR IGNORE）で担保する。そのため
再起動しても、同じファイルを最初から読み直しても二重登録されない。

課金種別（billing）: これらの CLI は通常サブスクリプション契約で使うため、
コレクタが登録するレコードの billing は既定で "subscription" とする
（環境変数 COLLECTOR_BILLING で変更可）。サブスク利用は API 課金ではない
ので、コスト換算はあくまで API リスト価格での参考値として扱うこと。

想定フォーマットと違う行は無理に解釈せずスキップする。1行の失敗で
全体が落ちないよう、パースは行単位で例外を握りつぶす。
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import store

COLLECTOR_BILLING = os.environ.get("COLLECTOR_BILLING", "subscription")
if COLLECTOR_BILLING not in ("api", "subscription", "unknown"):
    COLLECTOR_BILLING = "subscription"


def _iso_utc(ts) -> str | None:
    """ISO文字列 / epoch秒 / epochミリ秒 を ISO8601 UTC 文字列に正規化する。"""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        sec = ts / 1000 if ts > 1e12 else ts
        return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat()
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return None


def _hash_id(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()


def _iter_jsonl(path: Path):
    """(行番号, dict) を順に返す。壊れた行はスキップ。"""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield lineno, obj
    except OSError:
        return


# --- Claude Code -------------------------------------------------------------
# ~/.claude/projects 配下の **/*.jsonl。type == "assistant" の行に
# message.usage（input_tokens / output_tokens / cache_read_input_tokens /
# cache_creation_input_tokens）と message.model、timestamp、message.id が入る。
# 同一 message.id の行が複数回現れる（ストリーミングの途中経過）ため、
# source_id に message.id を含めることで最初の1件だけが登録される。

def parse_claude_code(path: Path) -> list[dict]:
    records = []
    for _lineno, obj in _iter_jsonl(path):
        try:
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message") or {}
            usage = msg.get("usage")
            msg_id = msg.get("id")
            if not usage or not msg_id:
                continue
            occurred = _iso_utc(obj.get("timestamp"))
            if occurred is None:
                continue
            request_id = obj.get("requestId")
            source_id = f"claude-code:{msg_id}:{request_id}" if request_id else f"claude-code:{msg_id}"
            records.append({
                "source_id": source_id,
                "occurred_at": occurred,
                "agent": "claude-code",
                "billing": COLLECTOR_BILLING,
                "provider": "anthropic",
                "model": msg.get("model") or "unknown",
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_tokens": usage.get("cache_read_input_tokens", 0),
                "cache_write_tokens": usage.get("cache_creation_input_tokens", 0),
            })
        except Exception:
            continue
    return records


# --- Codex -------------------------------------------------------------------
# ~/.codex/sessions 配下の rollout **/*.jsonl。payload.type == "token_count" の
# イベントに payload.info.last_token_usage（input_tokens / cached_input_tokens /
# output_tokens）が入る。
# 注意: Codex の input_tokens には cached_input_tokens が含まれる仕様なので、
# 二重計算を避けるため input_tokens には cached を差し引いた値を格納し、
# cached 分は cache_read_tokens に入れる。
# モデル名は同一ファイル内の turn_context 行（payload.model）から拾う。

def parse_codex(path: Path) -> list[dict]:
    records = []
    model = "unknown"
    for lineno, obj in _iter_jsonl(path):
        try:
            payload = obj.get("payload") or {}
            if payload.get("model"):
                model = payload["model"]
            if payload.get("type") != "token_count":
                continue
            info = payload.get("info") or {}
            usage = info.get("last_token_usage")
            if not usage:
                continue
            occurred = _iso_utc(obj.get("timestamp"))
            if occurred is None:
                continue
            input_total = int(usage.get("input_tokens", 0))
            cached = int(usage.get("cached_input_tokens", 0))
            records.append({
                "source_id": "codex:" + _hash_id(str(path), str(lineno), occurred),
                "occurred_at": occurred,
                "agent": "codex",
                "billing": COLLECTOR_BILLING,
                "provider": "openai",
                "model": info.get("model") or model,
                "input_tokens": max(input_total - cached, 0),
                "output_tokens": int(usage.get("output_tokens", 0)),
                "cache_read_tokens": cached,
                "cache_write_tokens": 0,
            })
        except Exception:
            continue
    return records


# --- pi ----------------------------------------------------------------------
# ~/.pi/agent/sessions 配下の **/*.jsonl。type == "message" の行の
# message（role == "assistant"）に usage（input / output / cacheRead /
# cacheWrite）と model が入る。timestamp は行側（ISO文字列）または
# message 側（epochミリ秒）のどちらかにある。

def parse_pi(path: Path) -> list[dict]:
    records = []
    for lineno, obj in _iter_jsonl(path):
        try:
            msg = obj.get("message") or {}
            if msg.get("role") != "assistant":
                continue
            usage = msg.get("usage")
            if not usage:
                continue
            occurred = _iso_utc(obj.get("timestamp") or msg.get("timestamp"))
            if occurred is None:
                continue
            records.append({
                "source_id": "pi:" + _hash_id(str(path), str(lineno)),
                "occurred_at": occurred,
                "agent": "pi",
                "billing": COLLECTOR_BILLING,
                "provider": "",
                "model": msg.get("model") or "unknown",
                "input_tokens": int(usage.get("input", 0)),
                "output_tokens": int(usage.get("output", 0)),
                "cache_read_tokens": int(usage.get("cacheRead", 0)),
                "cache_write_tokens": int(usage.get("cacheWrite", 0)),
            })
        except Exception:
            continue
    return records


PARSERS = {
    "claude-code": parse_claude_code,
    "codex": parse_codex,
    "pi": parse_pi,
}


def configured_dirs() -> list[tuple[str, Path]]:
    """LOG_DIRS を [(agent, path), ...] に変換する。不正な要素は無視。"""
    out: list[tuple[str, Path]] = []
    for part in os.environ.get("LOG_DIRS", "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        agent, _, raw_path = part.partition("=")
        agent = agent.strip()
        if agent in PARSERS and raw_path.strip():
            out.append((agent, Path(raw_path.strip())))
    return out


def collect_once() -> dict:
    """設定された全ディレクトリを1回走査し、新規レコードを登録する。"""
    total_inserted = 0
    total_seen = 0
    for agent, base in configured_dirs():
        if not base.is_dir():
            continue
        parser = PARSERS[agent]
        for path in sorted(base.rglob("*.jsonl")):
            try:
                records = parser(path)
            except Exception:
                continue
            if records:
                inserted, _skipped = store.insert_records(records)
                total_inserted += inserted
                total_seen += len(records)
    return {"inserted": total_inserted, "seen": total_seen}
