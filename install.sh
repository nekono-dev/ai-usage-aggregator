#!/usr/bin/env bash
# ai-usage-aggregator のインストーラー。
#
# 使い方:
#   ./install.sh [インストール先ディレクトリ]
#
# 環境変数で上書きできるもの:
#   REPO_URL      取得元リポジトリ（既定: https://github.com/nekono-dev/ai-usage-aggregator.git）
#   TUNNEL_TOKEN  Cloudflare Tunnel のトークン（後から .env に書いてもよい）
#
# やること:
#   1. リポジトリを clone（ブートストラップ用。以後の更新は updater の
#      コンテナ内クローンが担う。公開リポジトリのため認証は不要）
#   2. .env を生成（APP_API_KEY はランダム生成）
#   3. docker compose で起動し、ヘルスチェックまで確認
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/nekono-dev/ai-usage-aggregator.git}"
DIR="${1:-./ai-usage-aggregator}"

for cmd in git docker; do
  command -v "$cmd" >/dev/null || { echo "error: $cmd が必要です" >&2; exit 1; }
done
docker compose version >/dev/null 2>&1 || { echo "error: docker compose v2 が必要です" >&2; exit 1; }

if [ -d "$DIR/.git" ]; then
  echo "既存のチェックアウトを使います: $DIR"
else
  git clone "$REPO_URL" "$DIR"
fi
cd "$DIR"

if [ -f .env ]; then
  echo ".env は既にあるため生成をスキップします"
else
  API_KEY="$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')"
  cp .env.example .env
  # プレースホルダを実値に差し替える（存在する行だけ）
  sed -i "s|^APP_API_KEY=.*|APP_API_KEY=${API_KEY}|" .env
  sed -i "s|^GIT_REPO_URL=.*|GIT_REPO_URL=${REPO_URL}|" .env
  if [ -n "${TUNNEL_TOKEN:-}" ]; then
    sed -i "s|^TUNNEL_TOKEN=.*|TUNNEL_TOKEN=${TUNNEL_TOKEN}|" .env
  fi
  echo ".env を生成しました（APP_API_KEY はランダム生成）"
fi

echo "コンテナを起動します..."
docker compose up -d --build

echo "ヘルスチェックを待っています..."
ok=0
for _ in $(seq 1 30); do
  if docker compose exec -T app python3 -c \
      "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" \
      >/dev/null 2>&1; then
    ok=1; break
  fi
  sleep 3
done
[ "$ok" = "1" ] || { echo "error: app のヘルスチェックが通りません。docker compose logs app を確認してください" >&2; exit 1; }

echo
echo "=== 導入完了"
echo "インストール先: $(pwd)"
grep '^APP_API_KEY=' .env | sed 's/^/  /'
echo "  確認: docker compose exec -T app python3 -c \\"
echo "          \"import urllib.request; print(urllib.request.urlopen('http://localhost:8000/healthz').read())\""
echo "  次のステップ:"
echo "    - Cloudflare Tunnel で公開する場合は .env の TUNNEL_TOKEN を設定し"
echo "      docker compose up -d cloudflared を実行"
echo "    - ログの場所が既定と違う場合は .env の CLAUDE_LOG_DIR 等を直し"
echo "      docker compose up -d app を実行"
echo "    - 以後の更新は管理 API（POST /v1/admin/update）から行えます"
