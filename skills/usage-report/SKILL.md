---
name: usage-report
description: ai-usage-aggregator の実測データから AI 利用量レポート（日次テーブルと週次・月次グラフ）を生成し、定形フォーマットで届ける手順。レポートの生成・配信を任された AI エージェントはこの手順に従う。
---

# AI利用量レポートの生成と配信

## 生成物

`report/usage_charts.py` が次を生成する。計算はスクリプトが行うので、
エージェントが数値を再計算してはいけない。

- `usage_tables.md`: 日次テーブル（ベンダ別・モデル別。行はその日の
  実測に現れたモデルだけ）
- `usage.html`: 週次・月次の棒グラフ（ラベルは「$金額 / トークン量」）、
  期間ごとのベンダ別合計、利用割合の円グラフ

## 前提

- ai-usage-aggregator が稼働し、API キーで `/v1/summary` と
  `/v1/report` を取得できること
- Python 3.11 以上。依存は標準ライブラリのみ（追加インストール不要）

## 導入

このリポジトリを clone するだけ。スクリプトは `report/usage_charts.py`。

## データ取得

スクリプトが次の優先順で取得する。エージェントは自分の環境に合う
ものを用意すればよい。

1. 取得済み JSON を渡す: `--summary-json`（/v1/summary の応答）と
   `--daily-json`（/v1/report?group_by=model の応答）
2. 環境変数で直接取得: `AGGREGATOR_BASE_URL`（既定
   `http://localhost:8000`）と `AGGREGATOR_API_KEY`
3. Muse のカスタムコネクタ（custom.ai-usage-aggregator）がある環境
   では、そのヘルパーを自動で使う

## 実行

```bash
python3 report/usage_charts.py [--out DIR] [--date YYYY-MM-DD]
```

- `--date`: 日次テーブルの対象日。省略時は JST の前日
- `--out`: 出力先。省略時は `report/reports/`
- テーブルは標準出力にも出る

## 配信フォーマット

レポートは次の形で届ける。推定値や料金プランとの比較は付けない。

1. 1行目: **AI使用量レポート（YYYY-MM-DD）**（日付は生成日）
2. 2行目: 金額は API 換算の参考値。
3. `usage_tables.md` の内容をそのまま載せる
4. `usage.html` を添付する

対象日に実測が無い場合は、テーブルが空になる。その旨を 1 行で伝える。

## 定期実行

毎日朝（利用者のタイムゾーンで 07:00 目安）に上記の生成と配信を
スケジュールする。失敗時は再試行 1 回まで。それでも失敗したら、
失敗した手順とエラー内容を短く報告する。
