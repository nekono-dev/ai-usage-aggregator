# エージェント向け 導入・保守ガイド

この文書は、AI エージェント（Muse 等）がユーザーと対話しながら
ai-usage-aggregator の導入から保守までを行うための手順書です。
エージェントは各ステップで確認を取り、検証結果をユーザーに報告しながら
進めてください。秘密情報（API キー、トークン、秘密鍵）はチャットに
貼らせず、ホスト上のファイル（.env、deploy_key）に直接書かせてください。

## 全体像

- app: 使用量の集計 API。最小権限（非 root、read_only）で動く
- updater: セルフアップデート専任。docker.sock を持つのはこれだけ。
  ソースは専用ボリューム内の自前クローン（/src/repo）で管理し、
  ホストのチェックアウトはブートストラップ専用
- cloudflared: 外部公開（Cloudflare Tunnel）を使う場合のみ

計測は2プレーン（混ぜない）:
- サブスクリプション計測: エージェント CLI のローカルログを集計
- API 計測: プロバイダ公式 Admin API を同期（キーを設定した場合のみ）

## 導入

### 0. 事前にユーザーへ確認すること

1. リポジトリの URL と、updater の clone 認証方式
   （SSH デプロイキー推奨 / HTTPS トークン）
2. 公開方法（Cloudflare Tunnel のトークンの有無、またはサブドメイン /
   パス配下のどちらで公開するか）
3. エージェント CLI のログの場所（既定: ~/.claude/projects、
   ~/.codex/sessions、~/.pi/agent/sessions）
4. API 計測を使うか（使う場合は後からでも設定可。今は不要が普通）

### 1. インストーラーを実行する

ホストで:

```bash
git clone <リポジトリURL> ai-usage-aggregator-src   # 取得用（任意の方法でよい）
cd ai-usage-aggregator-src
./install.sh ~/ai-usage-aggregator
```

install.sh が clone、.env 生成（APP_API_KEY はランダム）、デプロイキー
生成、起動、ヘルスチェックまで行います。途中で表示される公開鍵を、
ユーザーが GitHub リポジトリの Settings → Deploy keys に
Read-only で登録したことを確認してください（エージェントが GitHub の
管理権限を持つ場合は、公開鍵を受け取って API で登録しても構いません）。

### 2. 導入後の検証（エージェントが API 経由で確認）

- `GET /healthz` が 200
- `GET /v1/admin/status` で records が増えていくこと、
  `provider_sync.configured_providers` が想定どおりであること
- `GET /v1/summary` が返ること
- updater の初回 clone が済んでいること:
  `docker compose logs updater` に `cloned ... into /src/repo` がある、
  または最初の更新リクエストが checkout 段階を通過すること

### 3. 公開設定

- Tunnel: .env の TUNNEL_TOKEN を設定し `docker compose up -d` で反映。
  パス配下で公開する場合は APP_BASE_PATH（例 /usage）を設定する
  （未設定でもアプリが自動検出する）
- 公開 URL で /healthz と /v1/summary（要 API キー）を確認する

## 日常の保守

### 更新（通常フロー）

1. 変更を develop へマージする
2. `POST /v1/admin/update` に `{"ref": "develop"}` を送る
3. `GET /v1/admin/update` で state が done になることを確認する。
   detail に `followed origin/develop by reset (history diverged)` と
   ある場合は、履歴書き換えへ自動追従したという意味（異常ではない）
4. `GET /v1/admin/status` の version が新しいコミットになっていること

### バックアップ

- `POST /v1/admin/backup` → `GET /v1/admin/backups` で一覧確認。
  実体は DB ボリュームの backups/ 配下

### メンテナンスモード

- `POST /v1/admin/maintenance` に `{"enabled": true}`。
  /healthz と /v1/admin/* 以外が 503 になる。解除は false

### 設定変更（.env を変えたら）

- app の設定（単価に影響しないもの、APP_BASE_PATH、プロバイダの
  Admin キー等）: .env を編集し `docker compose up -d app`
  （ホストのブートストラップ用チェックアウトで実行。古い場合は
  先に git pull する）
- updater の設定（GIT_REPO_URL、認証）: .env 編集後に
  `docker compose up -d updater`。デプロイキーを差し替えた場合は
  ./deploy_key を置き換えてから

### API 計測の有効化

1. 各プロバイダで Admin キーを発行する（Anthropic: sk-ant-admin…、
   OpenAI: sk-admin…。通常の API キーやサブスク契約では読めない）
2. .env の ANTHROPIC_ADMIN_KEY / OPENAI_ADMIN_KEY に設定し app を再作成
3. `POST /v1/admin/sync` で即時同期し、`GET /v1/api-usage` と
   admin status の provider_sync.last_sync が ok であることを確認

## 既存デプロイ（ホストクローン方式）からの移行

1. .env に GIT_REPO_URL を追加する
2. ./deploy_key に updater 用デプロイキーを生成し、公開鍵を
   リポジトリの Deploy keys に Read-only で登録する
3. ホストのチェックアウトで git pull し、
   `docker compose up -d --build updater` で updater を入れ替える
4. updater が source ボリュームへ clone したことをログで確認する。
   以後、ホストのチェックアウトはブートストラップ専用となり、
   更新は管理 API から行う

## トラブルシューティング

| 症状 | 原因と対処 |
|---|---|
| 更新が `git clone failed` / `checkout failed` で失敗 | clone 認証の問題。デプロイキーが Deploy keys に登録されているか、GIT_TOKEN の権限、GIT_REPO_URL の綴りを確認 |
| 更新が `build failed: no configuration file provided` | updater が古い（compose の cwd 修正前）。ホストで pull + `docker compose up -d --build updater` |
| 更新の detail に reset の記録 | 履歴書き換えへの自動追従。正常系。頻発する場合はスカッシュマージ等の運用を見直す |
| admin status の version が `unknown` | GIT_COMMIT を埋めずに手動ビルドされた app。updater 経由で更新すると刻印される |
| 公開 URL で 404 | パスプレフィックスの自動検出外の構成。APP_BASE_PATH を明示する |
| records が増えない | LOG_DIRS と CLAUDE_LOG_DIR 等のマウント先を確認。admin/collect で即時収集して切り分ける |
| API 計測が 0 のまま | Admin キーの種別（admin 専用キーか）と provider_sync.last_sync のエラーを確認 |
