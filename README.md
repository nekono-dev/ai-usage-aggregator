# ai-usage-aggregator

各種AIエージェントCLI（Claude Code / Codex / pi）のローカルセッションログから
トークン使用量を集計し、HTTP API で返すスタンドアロンアプリケーションです。
Ubuntu 上で Docker コンテナとして動作し、外部公開は Cloudflare Tunnel 経由、
API キー認証必須です。

## 構成

```
Claude Code / Codex / pi のセッションログ (JSONL)
        │  read-only マウント
        ▼
┌─────────────────────────────┐        ┌──────────────────┐
│ app コンテナ (FastAPI)      │◄───────│ cloudflared      │
│  - 5分ごとにログを走査      │  LAN内 │  コンテナ        │
│  - SQLite に蓄積            │ のみ   │  (Tunnel)        │
│  - /v1/report, /v1/summary  │        └────────┬─────────┘
└─────────────────────────────┘                 │ Cloudflare Tunnel
                                                ▼
                                        Muse（パーソナルエージェント）
                                        が API からデータを取得
```

- app の 8000 番ポートはホストにもインターネットにも公開しません
  （compose の `expose` のみ）。到達経路は同一 compose ネットワーク内の
  cloudflared から `http://app:8000` だけです。
- ログディレクトリは読み取り専用マウント、app コンテナは非root・
  `cap_drop: ALL`・`read_only: true` で動作します。

## 前提（Ubuntu）

Docker Engine と compose プラグインをインストールします。

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
sudo usermod -aG docker $USER   # 再ログイン後に有効
```

## Cloudflare Tunnel の作成

1. [Cloudflare Zero Trust](https://one.dash.cloudflare.com/) ダッシュボードで
   Networks → Tunnels → Create a tunnel（Cloudflared）を選択
2. トンネル名を付け、表示されるトークン（`TUNNEL_TOKEN`）を控える
3. Public Hostname を追加:
   - Subdomain / Domain: 公開に使うホスト名（例: `usage.example.com`）
   - Service: Type `HTTP`、URL `app:8000`
     （cloudflared コンテナから見た app コンテナのアドレスです）

## 起動手順

```bash
cp .env.example .env
# .env を編集: APP_API_KEY（openssl rand -hex 32 で生成）、TUNNEL_TOKEN、
# CLAUDE_LOG_DIR / CODEX_LOG_DIR / PI_LOG_DIR（ホスト側の実パス）
docker compose up -d --build
docker compose ps        # app が healthy になることを確認
docker compose logs -f app
```

## 疎通確認

ローカル（コンテナ内から）:

```bash
docker compose exec app python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/healthz').read())"
# => b'{"status":"ok"}'
```

外部（Tunnel 経由、API キー付き）:

```bash
curl -H "Authorization: Bearer $APP_API_KEY" https://usage.example.com/v1/summary
```

## API リファレンス

認証: `Authorization: Bearer <APP_API_KEY>` または `X-API-Key: <APP_API_KEY>`。
`/healthz` のみ認証不要です。

### GET /healthz

```json
{"status": "ok"}
```

### POST /v1/usage/records

レコードを1件（オブジェクト）または複数件（配列）登録します。
`source_id` が重複するものはスキップされます。

```bash
curl -X POST -H "Authorization: Bearer $APP_API_KEY" -H 'Content-Type: application/json' \
  https://usage.example.com/v1/usage/records \
  -d '{"source_id":"manual:2026-11-21:1","occurred_at":"2026-11-21T00:00:00+00:00",
       "agent":"manual","provider":"anthropic","model":"claude-sonnet-5-5",
       "input_tokens":1000,"output_tokens":200,"cache_read_tokens":0,"cache_write_tokens":0}'
# => {"inserted":1,"skipped":0}
```

### GET /v1/report?from=YYYY-MM-DD&to=YYYY-MM-DD&group_by=day|model|agent

期間内の集計（トークン内訳合計 + 概算コストUSD）を返します。
`from` / `to` を省略すると直近30日、`group_by` の既定は `day` です。

```bash
curl -H "Authorization: Bearer $APP_API_KEY" \
  "https://usage.example.com/v1/report?group_by=model"
```

### GET /v1/summary

直近7日 / 30日の合計とモデル別内訳を返します。日次レポート用に主に使う想定です。

```bash
curl -H "Authorization: Bearer $APP_API_KEY" https://usage.example.com/v1/summary
```

概算コストは `app/prices.py` の単価表（モデルごとの input / output /
cache_read / cache_write、USD/1Mトークン）で計算します。単価表に無いモデルは
コスト 0 になり、`price_known: false` が付きます。

### 課金種別（billing）とコストの意味

各レコードは課金種別 `billing`（`api` / `subscription` / `unknown`）を持ちます。

- `api`: API 課金で使った分。`api_cost_usd` に集計されます。
- `subscription`: サブスクリプション契約（Claude Pro、ChatGPT Plus 等）の枠を
  消費した分。**API 使用量ではなく、API 請求も発生しません。**
- コレクタが取り込むレコードは既定で `subscription` です
  （`COLLECTOR_BILLING` で変更可）。

レスポンスの `api_equivalent_cost_usd` は、全レコードを API リスト価格で
換算した**参考値**です。サブスク利用分を「API で処理した場合いくらか」の
比較に使うためのもので、実際の支出ではありません。

## ログの取り込み仕様

アプリ起動中、`COLLECT_INTERVAL_SEC`（既定300秒）ごとに
`LOG_DIRS` で指定したディレクトリ配下の `**/*.jsonl` を走査します。

- Claude Code: `type == "assistant"` 行の `message.usage` を集計
- Codex: `token_count` イベントの `last_token_usage` を集計
  （input_tokens に含まれる cached 分は cache_read に振り分けて格納）
- pi: assistant メッセージの `usage`（input / output / cacheRead / cacheWrite）を集計

重複排除はレコードの `source_id`（UNIQUE 制約）で行うため、再走査しても
二重登録されません。

## セキュリティ注意

- ポートを直接公開しないでください。外部公開は Cloudflare Tunnel 経由のみです。
- `APP_API_KEY` は推測できない値を生成し（`openssl rand -hex 32`）、
  `.env` はリポジトリにコミットしないでください（`.gitignore` 済み）。
- API キーを Muse（パーソナルエージェント）へ渡すときは、安全な手段
  （暗号化された保管・受け渡し）を使ってください。平文のメールや
  チャットへの貼り付けは避けてください。
- `TUNNEL_TOKEN` も同様に秘密情報です。漏れた場合は Zero Trust
  ダッシュボードでトンネルを作り直してください。
