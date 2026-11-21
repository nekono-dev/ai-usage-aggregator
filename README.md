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

### 公開URLの形（サブドメイン / パス配下）

- **サブドメインのルート**（例: `https://usage.example.com/`）: そのまま
  動きます。追加設定は不要です。
- **パス配下**（例: `https://ai.example.com/usage/`）: Cloudflare Tunnel は
  パスを剥がさないため、リクエストは `/usage/healthz` の形でアプリに届き
  ます。アプリはプレフィックスを検出して剥がすので、この形でも動きます。
  - 検出は自動です（先頭セグメントの次が既知ルートの場合に剥がす）。
  - 確実に動かしたい場合は `.env` に `APP_BASE_PATH=/usage` を設定して
    ください（自動検出より優先されます）。
  - `X-Forwarded-Prefix` ヘッダが付いている場合もそれを使います。

## 起動手順

```bash
cp .env.example .env
# .env を編集: APP_API_KEY（openssl rand -hex 32 で生成）、TUNNEL_TOKEN、
# CLAUDE_LOG_DIR / CODEX_LOG_DIR / PI_LOG_DIR（ホスト側の実パス）、
# APP_UID / APP_GID（ログの所有ユーザーで実行した id -u / id -g）
docker compose up -d --build
docker compose ps        # app が healthy になることを確認
docker compose logs -f app
```

既存の `usage-data` ボリュームに SQLite データがある場合、`APP_UID` / `APP_GID` を
変更する前に、ボリューム内のディレクトリとファイルの所有者を新しい UID/GID に合わせてください。

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

### 管理 API（/v1/admin/*）

運用・メンテナンス用です。認証は通常の API と同じキーです。

- `GET /v1/admin/status` : 稼働中バージョン（ビルド時の git コミット）、
  DB のレコード数とサイズ、最終収集時刻、メンテナンス状態、更新状況
- `POST /v1/admin/collect` : ログ収集を即時実行
- `POST /v1/admin/backup` : SQLite のバックアップを DB と同じボリュームの
  `backups/` に作成。`GET /v1/admin/backups` で一覧
- `POST /v1/admin/maintenance` `{"enabled": true|false}` :
  メンテナンスモード切替。有効中は `/healthz` と管理 API 以外に 503 を返す
- `POST /v1/admin/update` `{"ref": "main"}` : 更新リクエストを記録する
  （`ref` は `main` / `develop` / `vX.Y.Z` のみ）。実際の更新は updater が実行
- `GET /v1/admin/update` : 保留中のリクエストと直近の更新結果

## セルフアップデート（updater サイドカー）

アプリ本体は最小権限（非root、read_only、cap_drop ALL）を維持し、
コンテナの再作成が要る処理は専任の updater コンテナが担います。

1. 管理 API で更新リクエストを受けると、アプリは共有ボリューム
   （`control`）の `update-request.json` に意図を記録するだけです
2. updater がそれを検出し、固定手順のみを実行します:
   稼働中コンテナのラベルから compose プロジェクト名を特定 →
   `git fetch` して対象 ref をチェックアウト → `docker compose build app`
   （コミットハッシュをバージョンとして埋め込み）→ `docker compose up -d app`
   → `/healthz` で疎通確認
3. 疎通に失敗したら旧コミットへ戻して再ビルドし、自動でロールバックします
4. 進行と結果は `update-status.json` に書かれ、`GET /v1/admin/status` や
   `GET /v1/admin/update` から確認できます。処理済みリクエストは
   `control/history/` に残ります

安全策:

- docker.sock を持つのは updater だけです。updater はネットワークの
  受付を一切せず、任意コマンド実行の口もありません
- 更新対象は `main` / `develop` / `vX.Y.Z` タグのみ。同時実行はなく、
  リクエストは10分で失効します
- デプロイ用チェックアウトは updater が `git checkout` で切り替えます。
  開発用クローンとは別に、デプロイ専用のクローンを使ってください
- この機能の初回有効化だけは手動です（`git pull` して
  `docker compose up -d --build`）。以降の更新はリクエスト経由でできます
- updater の git fetch は、デプロイ用チェックアウトのリモート設定を
  そのまま使います。SSH リモートの場合は、compose 実行ユーザーの
  `~/.ssh`（config と鍵）を読み取り専用で同一パスにマウントします。
  updater はコンテナ内で root として動くため、マウントした config と
  鍵をそのまま使うと OpenSSH が所有者不一致で無視します。そのため
  updater は処理のたびに `~/.ssh` を所有者を揃えた専用コピーに
  ステージングし、config 内のパス参照も書き換えてから使います
  （`sudo` で compose を実行すると HOME が変わって鍵を
  参照できないので、通常ユーザーで実行してください）。
  HTTPS リモートの場合は、認証情報が同じパスで参照できるよう
  ホスト側で資格情報を設定しておく必要があります
- 動作確認だけしたい場合は `.env` に `UPDATER_DRY_RUN=1` を設定すると、
  updater が git / docker を実行せず成功扱いで状態だけ進めます

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
