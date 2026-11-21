#!/usr/bin/env python3
"""ai-usage-aggregator のセルフアップデート実行役（updater サイドカー）。

アプリ本体は最小権限のままにし、コンテナの再作成が要る処理だけを
このサイドカーが担う。ネットワークの受付は一切しない。

動作:
- CONTROL_DIR（アプリと共有するボリューム）の update-request.json を監視
- リクエストが来たら固定手順のみ実行する（任意コマンド実行はしない）
  1. 稼働中アプリのコンテナラベルから compose プロジェクト名を特定
  2. git fetch して対象 ref（main / develop / vX.Y.Z のみ）をチェックアウト
  3. docker compose build app（GIT_COMMIT を埋め込み）→ up -d app
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
PROJECT_DIR = Path(os.environ.get("PROJECT_DIR", "/project"))
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

# git / ssh 実行用のステージング HOME。
# コンテナは root で動くため、ホストユーザーの所有する ~/.ssh を直接使うと
# OpenSSH が所有者不一致で config と鍵を無視する（"Bad owner or permissions"）。
# そこでマウントされた ~/.ssh を root 所有・厳密なパーミッションで
# コピーし直し、パス参照も書き換えたうえで、git の HOME として使う。
STAGE_HOME = Path("/tmp/updater-home")
_git_env: dict = {}


def prepare_git_env() -> dict:
    """マウントされた実行ユーザーの ~/.ssh をステージングして git 用 env を返す。"""
    src_home = Path(os.environ.get("SOURCE_HOME") or os.environ.get("HOME", "/root"))
    staged: dict = {}
    try:
        src_ssh = src_home / ".ssh"
        if src_ssh.is_dir():
            dst_ssh = STAGE_HOME / ".ssh"
            if dst_ssh.exists():
                shutil.rmtree(dst_ssh)
            STAGE_HOME.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_ssh, dst_ssh, symlinks=False)
            dst_ssh.chmod(0o700)
            for item in dst_ssh.rglob("*"):
                if item.is_file():
                    item.chmod(0o644 if item.suffix == ".pub" else 0o600)
            config = dst_ssh / "config"
            if config.exists():
                config.write_text(
                    config.read_text(encoding="utf-8", errors="replace")
                    .replace(str(src_home), str(STAGE_HOME)),
                    encoding="utf-8")
                # $HOME 経由の config 参照は実行環境（setuid や passwd の
                # ホーム参照）で効かないことがあるため、ssh には -F で
                # ステージングした config を明示的に渡す
                staged["GIT_SSH_COMMAND"] = (
                    f"ssh -F {dst_ssh}/config"
                    " -o StrictHostKeyChecking=accept-new"
                    f" -o UserKnownHostsFile={STAGE_HOME}/known_hosts")
            staged["HOME"] = str(STAGE_HOME)
        src_gitconfig = src_home / ".gitconfig"
        if src_gitconfig.is_file():
            dst_gitconfig = STAGE_HOME / ".gitconfig"
            STAGE_HOME.mkdir(parents=True, exist_ok=True)
            dst_gitconfig.write_text(
                src_gitconfig.read_text(encoding="utf-8", errors="replace")
                .replace(str(src_home), str(STAGE_HOME)),
                encoding="utf-8")
            dst_gitconfig.chmod(0o600)
            staged["HOME"] = str(STAGE_HOME)
    except OSError as e:
        log(f"ssh staging failed (continuing without it): {e}")
        return {}
    if staged:
        log(f"staged ssh config from {src_home} into {STAGE_HOME}")
    return staged


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


def run(cmd: list[str], env_extra: dict | None = None, timeout: int = 1800) -> tuple[int, str]:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=env)
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode, out[-500:]


def git(*args: str) -> tuple[int, str]:
    return run(_GIT + list(args), env_extra=_git_env, timeout=300)


def discover_project() -> str | None:
    """稼働中 app コンテナのラベルから compose プロジェクト名を特定する。

    プロジェクト名がずれると別プロジェクトとして作り直してしまうため、
    推測ではなく実行中コンテナの実績値を使う。
    """
    code, out = run(["docker", "ps", "-q", "--filter",
                     "label=com.docker.compose.service=app"], timeout=60)
    if code != 0 or not out:
        return None
    container = out.splitlines()[0].strip()
    code, out = run(["docker", "inspect", "--format",
                     "{{ index .Config.Labels \"com.docker.compose.project\" }}",
                     container], timeout=60)
    if code != 0:
        return None
    return out.strip() or None


def compose(project: str, *args: str, env_extra: dict | None = None) -> tuple[int, str]:
    return run(["docker", "compose", "-p", project, *args],
               env_extra=env_extra, timeout=1800)


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


def checkout_ref(ref: str) -> tuple[int, str]:
    code, out = git("fetch", "--tags", "origin")
    if code != 0:
        return code, out
    if ref in ("main", "develop"):
        code, out = git("checkout", ref)
        if code != 0:
            return code, out
        return git("pull", "--ff-only", "origin", ref)
    return git("checkout", f"refs/tags/{ref}")


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
    global _git_env
    _git_env = prepare_git_env()
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
    code, prev = git("rev-parse", "HEAD")
    if code != 0:
        set_status(request_id, "failed", ref=ref, detail=f"rev-parse failed: {prev}")
        return
    prev = prev.strip()
    code, out = checkout_ref(ref)
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
        set_status(request_id, "done", ref=ref,
                   from_version=prev, to_version=new)
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
