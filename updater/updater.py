#!/usr/bin/env python3
"""ai-usage-aggregator のセルフアップデート実行役（updater サイドカー）。

アプリ本体は最小権限のままにし、コンテナの再作成が要る処理だけを
このサイドカーが担う。ネットワークの受付は一切しない。

ソースはホストのチェックアウトではなく、updater 専用ボリューム内の
自前クローン（PROJECT_DIR）を使う。ホスト側の git 状態に干渉されず、
手動の git 操作で更新が壊れることもない。クローンが無ければ
GIT_REPO_URL から初回 clone する。このリポジトリは公開前提のため、
clone / fetch に認証は不要（URL は差し替え可能）。

動作:
- CONTROL_DIR（アプリと共有するボリューム）の update-request.json を監視
- リクエストが来たら固定手順のみ実行する（任意コマンド実行はしない）
  1. 稼働中アプリのコンテナラベルから compose プロジェクト名を特定
  2. 自前クローンを fetch して対象 ref（main / develop / vX.Y.Z のみ）を
     チェックアウト。ブランチは pull --ff-only を試み、分岐していたら
     （履歴の書き換え等）origin の向き先へ reset --hard で自動追従する
  3. docker compose build app（GIT_COMMIT を埋め込み）→ up -d app。
     compose はプロジェクトディレクトリを cwd にして実行し、ログ
     ディレクトリのマウント元は稼働中コンテナから引き継ぐ
  4. /healthz で疎通確認。失敗したら旧コミットへ戻して再ビルド（ロールバック）
- 進行状況と結果は update-status.json に書き、アプリの管理 API から読める
- 処理済みリクエストは history/ へ移し、同じ request_id は二度実行しない
- リクエストの有効期限は REQUEST_TTL_SEC（既定 600 秒）。過ぎたものは失効扱い
- UPDATER_DRY_RUN=1 のときは git / docker を実行せず、成功扱いで状態だけ進める
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CONTROL_DIR = Path(os.environ.get("CONTROL_DIR", "/control"))
PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", "/src/repo"))
REPO_URL = os.environ.get("GIT_REPO_URL") or \
    "https://github.com/nekono-dev/ai-usage-aggregator.git"
HEALTH_URL = os.environ.get("APP_HEALTH_URL", "http://app:8000/healthz")
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "5"))
REQUEST_TTL_SEC = float(os.environ.get("REQUEST_TTL_SEC", "600"))
HEALTH_TIMEOUT_SEC = float(os.environ.get("HEALTH_TIMEOUT_SEC", "120"))
DRY_RUN = os.environ.get("UPDATER_DRY_RUN", "") not in ("", "0", "false")

REQUEST_FILE = CONTROL_DIR / "update-request.json"
STATUS_FILE = CONTROL_DIR / "update-status.json"
STATE_FILE = CONTROL_DIR / "updater-state.json"
LOG_FILE = CONTROL_DIR / "updater.log"
HISTORY_DIR = CONTROL_DIR / "history"

_ALLOWED_REF = re.compile(r"^(main|develop|v\d+\.\d+\.\d+)$")
_GIT = ["git", "-c", f"safe.directory={PROJECT_DIR}", "-C", str(PROJECT_DIR)]

# 公開リポジトリ前提のため、git の clone / fetch に認証は不要。
_git_env: dict = {}


def ensure_clone() -> tuple[bool, str]:
    """自前クローンが無ければ GIT_REPO_URL から clone する。

    PROJECT_DIR（例 /src/repo）の親が専用ボリューム。PROJECT_DIR 直下
    には .env がファイルマウントされていて clone 先にできないため、
    隣に clone してから中身（.git 含む）を移す。.env はリポジトリに
    含まれないので衝突しない。
    """
    if (PROJECT_DIR / ".git").is_dir():
        return True, ""
    if not REPO_URL:
        return False, "GIT_REPO_URL is not set (cannot clone)"
    tmp = PROJECT_DIR.parent / "clone-tmp"
    try:
        if tmp.exists():
            shutil.rmtree(tmp)
        code, out = run(["git", "clone", REPO_URL, str(tmp)],
                        env_extra=_git_env, timeout=600)
        if code != 0:
            return False, f"git clone failed: {out}"
        PROJECT_DIR.mkdir(parents=True, exist_ok=True)
        for item in tmp.iterdir():
            item.rename(PROJECT_DIR / item.name)
        tmp.rmdir()
        log(f"cloned {REPO_URL} into {PROJECT_DIR}")
        return True, ""
    except OSError as e:
        return False, f"clone staging failed: {e}"
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    line = f"{now_iso()} {message}"
    print(line, flush=True)
    try:
        CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def set_status(request_id: str, state: str, **extra) -> None:
    status = {"request_id": request_id, "state": state, "updated_at": now_iso()}
    status.update(extra)
    write_json_atomic(STATUS_FILE, status)
    log(f"request {request_id}: {state} {extra if extra else ''}")


def processed_ids() -> list[str]:
    state = read_json(STATE_FILE) or {}
    return list(state.get("processed", []))


def mark_processed(request_id: str) -> None:
    ids = processed_ids()
    if request_id not in ids:
        ids.append(request_id)
    write_json_atomic(STATE_FILE, {"processed": ids[-50:]})


def archive_request(request_id: str) -> None:
    try:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        if REQUEST_FILE.exists():
            REQUEST_FILE.replace(HISTORY_DIR / f"{request_id}.json")
    except OSError as e:
        log(f"archive failed: {e}")


def run(cmd: list[str], env_extra: dict | None = None, timeout: int = 1800,
        cwd: str | None = None) -> tuple[int, str]:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                          env=env, cwd=cwd)
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode, out[-500:]


def git(*args: str) -> tuple[int, str]:
    return run(_GIT + list(args), env_extra=_git_env, timeout=300)


def _app_container() -> str | None:
    code, out = run(["docker", "ps", "-q", "--filter",
                     "label=com.docker.compose.service=app"], timeout=60)
    if code != 0 or not out:
        return None
    return out.splitlines()[0].strip()


def discover_project() -> str | None:
    """稼働中 app コンテナのラベルから compose プロジェクト名を特定する。

    プロジェクト名がずれると別プロジェクトとして作り直してしまうため、
    推測ではなく実行中コンテナの実績値を使う。
    """
    container = _app_container()
    if not container:
        return None
    code, out = run(["docker", "inspect", "--format",
                     "{{ index .Config.Labels \"com.docker.compose.project\" }}",
                     container], timeout=60)
    if code != 0:
        return None
    return out.strip() or None


# compose ファイルのログディレクトリは ${CLAUDE_LOG_DIR:-./logs/claude} の
# ように相対パスが既定で、updater 内から compose を実行すると解決先が
# ずれる。稼働中コンテナの実マウント元（ホスト絶対パス）を環境変数で
# 明示し、再作成後も同じマウントになるよう引き継ぐ。
_LOG_MOUNT_ENVS = {"/logs/claude": "CLAUDE_LOG_DIR",
                   "/logs/codex": "CODEX_LOG_DIR",
                   "/logs/pi": "PI_LOG_DIR"}
_RUNTIME_ENV: dict = {}


def discover_log_env() -> dict:
    container = _app_container()
    if not container:
        return {}
    code, out = run(["docker", "inspect", "--format", "{{ json .Mounts }}",
                     container], timeout=60)
    if code != 0:
        return {}
    try:
        mounts = json.loads(out)
    except json.JSONDecodeError:
        return {}
    envs = {}
    for m in mounts:
        var = _LOG_MOUNT_ENVS.get(m.get("Destination", ""))
        if var and m.get("Source"):
            envs[var] = m["Source"]
    return envs


def compose(project: str, *args: str, env_extra: dict | None = None) -> tuple[int, str]:
    merged = dict(_RUNTIME_ENV)
    if env_extra:
        merged.update(env_extra)
    return run(["docker", "compose", "-p", project, *args],
               env_extra=merged, timeout=1800, cwd=str(PROJECT_DIR))


def wait_health() -> bool:
    deadline = time.time() + HEALTH_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(3)
    return False


def checkout_ref(ref: str) -> tuple[int, str, str]:
    """ref をチェックアウトし、(終了コード, 出力, 補足) を返す。

    ブランチは通常 pull --ff-only で進める。リモート側の履歴が書き換え
    られて分岐していた場合は、デプロイ専用クローンである前提で
    origin/<ref> へ reset --hard して自動追従する（補足にその旨を返す）。
    ロールバックは呼び出し側が旧 HEAD を保持して行うため、reset しても
    旧コミットへは戻れる。
    """
    code, out = git("fetch", "--tags", "origin")
    if code != 0:
        return code, out, ""
    if ref in ("main", "develop"):
        code, out = git("checkout", ref)
        if code != 0:
            return code, out, ""
        code, out = git("pull", "--ff-only", "origin", ref)
        if code == 0:
            return 0, out, ""
        log(f"pull --ff-only failed for {ref} (diverged?); "
            f"resetting to origin/{ref}")
        code, out = git("reset", "--hard", f"origin/{ref}")
        if code == 0:
            return 0, out, f"followed origin/{ref} by reset (history diverged)"
        return code, out, ""
    code, out = git("checkout", f"refs/tags/{ref}")
    return code, out, ""


def build_and_restart(project: str, commit: str) -> tuple[bool, str]:
    code, out = compose(project, "build", "app",
                        env_extra={"GIT_COMMIT": commit})
    if code != 0:
        return False, f"build failed: {out}"
    code, out = compose(project, "up", "-d", "app")
    if code != 0:
        return False, f"up failed: {out}"
    if not wait_health():
        return False, "health check timed out"
    return True, "ok"


def process_request(req: dict) -> None:
    global _RUNTIME_ENV
    request_id = str(req.get("request_id", ""))
    ref = str(req.get("ref", ""))
    if not _ALLOWED_REF.match(ref):
        set_status(request_id, "rejected", ref=ref, detail="ref not allowed")
        return
    if DRY_RUN:
        set_status(request_id, "running", ref=ref, detail="dry run")
        time.sleep(1)
        set_status(request_id, "done", ref=ref, detail="dry run",
                   from_version=req.get("current_version"), to_version="dry-run")
        return
    set_status(request_id, "running", ref=ref)
    project = discover_project()
    if not project:
        set_status(request_id, "failed", ref=ref,
                   detail="could not discover compose project from running app container")
        return
    _RUNTIME_ENV = discover_log_env()
    ok_clone, clone_detail = ensure_clone()
    if not ok_clone:
        set_status(request_id, "failed", ref=ref, detail=clone_detail)
        return
    code, prev = git("rev-parse", "HEAD")
    if code != 0:
        set_status(request_id, "failed", ref=ref, detail=f"rev-parse failed: {prev}")
        return
    prev = prev.strip()
    code, out, note = checkout_ref(ref)
    if code != 0:
        set_status(request_id, "failed", ref=ref, detail=f"checkout failed: {out}")
        return
    code, new = git("rev-parse", "HEAD")
    new = new.strip() if code == 0 else ""
    if new and new == prev:
        set_status(request_id, "done", ref=ref, detail="already up to date",
                   from_version=prev, to_version=new)
        return
    ok, detail = build_and_restart(project, new[:12])
    if ok:
        extra = {"detail": note} if note else {}
        set_status(request_id, "done", ref=ref,
                   from_version=prev, to_version=new, **extra)
        return
    # ロールバック: 旧コミットへ戻して再ビルド
    log(f"update failed ({detail}); rolling back to {prev}")
    git("checkout", prev)
    ok2, detail2 = build_and_restart(project, prev[:12])
    if ok2:
        set_status(request_id, "rolled_back", ref=ref, detail=detail,
                   from_version=prev, to_version=prev)
    else:
        set_status(request_id, "failed", ref=ref,
                   detail=f"{detail}; rollback failed: {detail2}",
                   from_version=prev, to_version=new)


def main() -> int:
    log(f"updater started (dry_run={DRY_RUN}, project_dir={PROJECT_DIR})")
    ok_clone, clone_detail = ensure_clone()
    if not ok_clone:
        log(f"initial clone skipped/failed: {clone_detail} "
            "(will retry on update request)")
    while True:
        try:
            req = read_json(REQUEST_FILE)
            if req:
                request_id = str(req.get("request_id", ""))
                if request_id and request_id in processed_ids():
                    archive_request(request_id)
                elif not request_id:
                    archive_request("invalid")
                elif time.time() - float(req.get("requested_at", 0)) > REQUEST_TTL_SEC:
                    set_status(request_id, "expired", ref=req.get("ref"))
                    mark_processed(request_id)
                    archive_request(request_id)
                else:
                    try:
                        process_request(req)
                    except Exception as e:  # 想定外でも監視ループは止めない
                        set_status(request_id, "failed", detail=f"updater error: {e}")
                    mark_processed(request_id)
                    archive_request(request_id)
        except Exception as e:
            log(f"loop error: {e}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    sys.exit(main())
