#!/usr/bin/env python3
"""AI利用量の日次テーブルとグラフ（棒＋円＋折れ線）を生成する。

出力:
- usage_tables.md: 日次テーブル（Markdown、ベンダ別、モデル行は実測に
  現れたものだけ）
- usage.html: 週次・月次の棒グラフ、日次推移の折れ線グラフ（Claude /
  Codex 合算）と利用割合の円グラフ、期間ごとのベンダ別合計

対象日（JST）:
- 日次: 12:00 前は前日、12:00 以降は当日
- 週次・月次: 常に前日を末日とする（週次は7日間、月次は30日間）

データ取得（優先順）:
1. --daily-json / --weekly-json / --monthly-json で取得済み JSON を渡す
   （いずれも /v1/report?group_by=model の応答）
2. 環境変数 AGGREGATOR_API_KEY と AGGREGATOR_BASE_URL
   （既定 http://localhost:8000）で API から直接取得する
3. Muse カスタムコネクタのヘルパーがあればそれを使う

使い方: python3 usage_charts.py [--out DIR] [--date YYYY-MM-DD]
--date は日次テーブルの対象日の上書き（週次・月次の起点は変わらない）。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
BASE = Path(__file__).resolve().parent
HELPER = Path.home() / "workspace/custom-connectors/custom.ai-usage-aggregator/bin/usage_api.py"

FAMILY_COLORS = {
    "Claude": ["#dc2626", "#ea580c", "#d97706", "#db2777", "#f59e0b", "#991b1b"],
    "Codex": ["#2563eb", "#7c3aed", "#0891b2", "#4f46e5", "#0d9488", "#1e40af"],
    "Other": ["#666666", "#888888"],
}
GROUPS = ("Claude", "Codex", "Other")


# ---------- データ取得 ----------

def _http_get(path: str) -> dict:
    base = os.environ.get("AGGREGATOR_BASE_URL", "http://localhost:8000").rstrip("/")
    key = os.environ["AGGREGATOR_API_KEY"]
    url = base + path
    if shutil.which("curl"):
        out = subprocess.run(
            ["curl", "-s", "-m", "60", "-H", f"Authorization: Bearer {key}", url],
            capture_output=True, text=True, check=True, timeout=90)
        return json.loads(out.stdout)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read())


def _helper(*args: str) -> dict:
    out = subprocess.run(
        [sys.executable, str(HELPER), *args],
        capture_output=True, text=True, check=True, timeout=120)
    return json.loads(out.stdout)["body"]


def report_dates(now: datetime) -> dict:
    """対象日を決める（JST）。

    日次は 12:00 前は前日、12:00 以降は当日。週次・月次は常に前日を
    末日とする（週次は7日間、月次は30日間）。
    """
    today = now.date()
    yesterday = today - timedelta(days=1)
    return {
        "daily": today if now.hour >= 12 else yesterday,
        "week": (yesterday - timedelta(days=6), yesterday),
        "month": (yesterday - timedelta(days=29), yesterday),
    }


def _fetch_report(frm: str, to: str, group_by: str = "model") -> dict:
    if os.environ.get("AGGREGATOR_API_KEY"):
        return _http_get(f"/v1/report?group_by={group_by}&from={frm}&to={to}")
    if HELPER.exists():
        return _helper("report", "--group-by", group_by, "--from", frm, "--to", to)
    raise SystemExit(
        "データ取得手段がありません: --daily-json 等を渡すか、"
        "AGGREGATOR_API_KEY を設定してください")


def _days(report: dict, frm: str, to: str) -> list[dict]:
    """group_by=day の応答を期間内の全日分に整える（実測の無い日は 0）。"""
    from datetime import date as _date
    by_date = {g["group"]: g for g in report.get("groups", [])}
    out = []
    d = _date.fromisoformat(frm)
    end = _date.fromisoformat(to)
    while d <= end:
        g = by_date.get(d.isoformat())
        out.append({"date": d.isoformat(),
                    "total_tokens": g["total_tokens"] if g else 0,
                    "api_equivalent_cost_usd":
                        g["api_equivalent_cost_usd"] if g else 0.0})
        d += timedelta(days=1)
    return out


def _window(report: dict, frm: str, to: str) -> dict:
    """report 応答を summary の期間と同じ形（from/to/by_model）に直す。"""
    return {"from": frm, "to": to, "by_model": [
        {"model": g["group"], "total_tokens": g["total_tokens"],
         "api_equivalent_cost_usd": g["api_equivalent_cost_usd"],
         "records": g.get("records", 0)}
        for g in report.get("groups", [])]}


def fetch_all(daily_date: str, week: tuple, month: tuple) -> tuple[dict, dict]:
    """(windows, daily report) を取得する。windows は last_7d/last_30d。"""
    wf, wt = week[0].isoformat(), week[1].isoformat()
    mf, mt = month[0].isoformat(), month[1].isoformat()
    body = {"last_7d": _window(_fetch_report(wf, wt), wf, wt),
            "last_30d": _window(_fetch_report(mf, mt), mf, mt)}
    body["last_7d"]["by_day"] = _days(_fetch_report(wf, wt, "day"), wf, wt)
    body["last_30d"]["by_day"] = _days(_fetch_report(mf, mt, "day"), mf, mt)
    return body, _fetch_report(daily_date, daily_date)


# ---------- 整形 ----------

def group_of(model: str) -> str:
    if model.startswith("claude"):
        return "Claude"
    if model.startswith(("gpt", "codex", "o1", "o3", "o4")):
        return "Codex"
    return "Other"


def display_name(model: str) -> str:
    if model.startswith("claude-"):
        rest = model[len("claude-"):]
        parts = rest.split("-")
        return " ".join([parts[0].capitalize()] + [".".join(parts[1:])] if len(parts) > 1 else parts)
    if model.startswith("gpt-"):
        return "GPT-" + model[len("gpt-"):].replace("-sol", " Sol").replace("-luna", " Luna")
    return model


def fmt_m(tokens: int) -> str:
    return f"{tokens / 1_000_000:,.2f}M"


def models_of(window: dict, group: str | None = None) -> list[dict]:
    """その期間で実測のある（総トークン > 0）モデルを利用量の降順で返す。"""
    ms = [m for m in window.get("by_model", []) if m["total_tokens"] > 0
          and (group is None or group_of(m["model"]) == group)]
    return sorted(ms, key=lambda m: -m["total_tokens"])


def build_tables(daily: dict, daily_date: str) -> str:
    """日次テーブル。daily は /v1/report（group_by=model）の応答。"""
    rows = [{"model": g["group"], "total_tokens": g["total_tokens"],
             "api_equivalent_cost_usd": g["api_equivalent_cost_usd"],
             "records": g["records"]}
            for g in daily.get("groups", []) if g["total_tokens"] > 0]
    lines = []
    grand_tok = 0
    grand_cost = 0.0
    grand_records = 0
    for group in GROUPS:
        ms = sorted((m for m in rows if group_of(m["model"]) == group),
                    key=lambda m: -m["total_tokens"])
        if not ms:
            continue
        sub_tok = sum(m["total_tokens"] for m in ms)
        sub_cost = sum(m["api_equivalent_cost_usd"] for m in ms)
        grand_tok += sub_tok
        grand_cost += sub_cost
        grand_records += sum(m["records"] for m in ms)
        lines.append(f"**{group}（日次）**")
        lines.append("")
        lines.append("| モデル | 利用量 | API換算 |")
        lines.append("|---|---:|---:|")
        for m in ms:
            lines.append(f"| {display_name(m['model'])} | {fmt_m(m['total_tokens'])} "
                         f"| ${m['api_equivalent_cost_usd']:,.2f} |")
        lines.append(f"| **{group} 小計** | **{fmt_m(sub_tok)}** | **${sub_cost:,.2f}** |")
        lines.append("")
    lines.append(f"日次合計: {fmt_m(grand_tok)} / 換算 ${grand_cost:,.2f}"
                 f"（{grand_records}件、{daily_date}）")
    return "\n".join(lines)


# ---------- グラフ ----------

def assign_colors(body: dict) -> dict[str, str]:
    """モデル表示名 → 色。Claude は暖色系、Codex は寒色系で統一する。"""
    color_of: dict[str, str] = {}
    counters: dict[str, int] = {}
    names = set()
    for key in ("last_7d", "last_30d"):
        for m in body[key].get("by_model", []):
            if m["total_tokens"] > 0:
                names.add(m["model"])
    for raw in sorted(names):
        fam = group_of(raw)
        i = counters.get(fam, 0)
        counters[fam] = i + 1
        palette = FAMILY_COLORS[fam]
        color_of[display_name(raw)] = palette[i % len(palette)]
    return color_of


def bar_chart(title: str, rows: list[tuple[str, int, float, str, str]], color_of: dict) -> str:
    """横棒グラフ（HTML）。rows = (表示名, トークン量, 金額, トークン量ラベル, 金額ラベル)。

    指標はタイトルで示すため、棒の中にはモデル名を置き、数値は棒の
    外（右端のすぐ外）に置く。モデル名が棒の幅に収まらない場合は、
    名前も棒のすぐ外に置き、その右に数値を続ける。正確な配置は JS が
    実測幅で行い、ここでの in/out は初期表示の目安。
    ページ上部の指標切替（利用量／金額）で JS が全グラフの行を並び替え、
    棒の長さと数値ラベルを選んだ指標に合わせて再計算する。
    初期表示は利用量の降順。
    """
    max_v = max((t for _, t, _, _, _ in rows), default=0) or 1
    parts = ['<div class="chart" data-chart>',
             f'<div class="chart-title">{title}</div><div class="rows">']
    for name, tokens, cost, tok_label, cost_label in rows:
        w = tokens / max_v * 100
        color = color_of.get(name, "#666")
        cls = "in" if w >= 40 else "out"
        parts.append(
            f'<div class="row" data-tokens="{tokens}" data-cost="{cost}" '
            f'data-tokens-label="{tok_label}" data-cost-label="{cost_label}">'
            f'<span class="rbar"><span class="rtrack">'
            f'<i style="width:{w:.1f}%;background:{color}"></i></span>'
            f'<span class="rname {cls}">{name}</span>'
            f'<span class="rvalue">{tok_label}</span></span></div>')
    parts.append("</div></div>")
    return "".join(parts)


def line_chart(window: dict) -> str:
    """日次推移の折れ線グラフ（Claude / Codex 合算、指標切替に連動）。

    週次・月次の各パネルに置く。横軸は日付、縦軸は選択中の指標で、
    指標ごとに正規化した SVG を2枚重ねて切替える（data-line）。
    """
    days = window.get("by_day") or []
    if not days:
        return ""
    W, H = 600.0, 168.0
    x0, x1, y_top, y_base = 4.0, 596.0, 16.0, 138.0
    n = len(days)

    def xs(i: int) -> float:
        return x0 + (x1 - x0) * (i / (n - 1) if n > 1 else 0.5)

    blocks = []
    for key, vkey, fmt in (("tokens", "total_tokens", fmt_m),
                           ("cost", "api_equivalent_cost_usd",
                            lambda v: f"${v:,.2f}")):
        vals = [d[vkey] for d in days]
        maxv = max(vals) or 1
        pts = [(xs(i), y_base - (v / maxv) * (y_base - y_top))
               for i, v in enumerate(vals)]
        line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        area = (f"M {pts[0][0]:.1f},{y_base:.1f} "
                + " ".join(f"L {x:.1f},{y:.1f}" for x, y in pts)
                + f" L {pts[-1][0]:.1f},{y_base:.1f} Z")
        dots = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.4" fill="#3b82f6"/>'
                       for x, y in pts)
        idx = sorted({0, (n - 1) // 2, n - 1})
        xlabels = "".join(
            f'<text x="{xs(i):.1f}" y="156" font-size="10" fill="currentColor" '
            f'text-anchor="{"start" if i == 0 else "end" if i == n - 1 else "middle"}">'
            f'{days[i]["date"][5:]}</text>' for i in idx)
        hidden = "" if key == "tokens" else " hidden"
        blocks.append(
            f'<div data-line="{key}"{hidden}>'
            f'<svg viewBox="0 0 {W:.0f} {H:.0f}" role="img" aria-label="日次推移" '
            f'style="width:100%;height:auto;display:block">'
            f'<line x1="{x0}" y1="{y_top}" x2="{x1}" y2="{y_top}" stroke="currentColor" stroke-opacity="0.15"/>'
            f'<line x1="{x0}" y1="{y_base}" x2="{x1}" y2="{y_base}" stroke="currentColor" stroke-opacity="0.3"/>'
            f'<text x="{x0}" y="11" font-size="10" fill="currentColor">{fmt(maxv)}</text>'
            f'<path d="{area}" fill="#3b82f6" fill-opacity="0.10"/>'
            f'<polyline points="{line}" fill="none" stroke="#3b82f6" stroke-width="1.8"/>'
            f'{dots}{xlabels}</svg></div>')
    return (f'<div class="chart seg"><div class="pie-title">日次推移'
            f'（<span data-total="tokens">利用量</span>'
            f'<span data-total="cost" hidden>金額</span>）</div>'
            f'{"".join(blocks)}</div>')


SORT_SCRIPT = """<script>
function placeLabels(row, pct) {
  var track = row.querySelector('.rtrack');
  var name = row.querySelector('.rname');
  var val = row.querySelector('.rvalue');
  if (!track || !name || !val) return;
  var fillW = track.clientWidth * pct / 100;
  var nameW = name.offsetWidth;
  if (nameW + 12 <= fillW) {
    name.className = 'rname in';
    name.style.left = '6px';
    val.style.left = (fillW + 6) + 'px';
  } else {
    name.className = 'rname out';
    name.style.left = (fillW + 6) + 'px';
    val.style.left = (fillW + 6 + nameW + 8) + 'px';
  }
}
document.querySelectorAll('[data-chart] .row').forEach(function (r) {
  var fill = r.querySelector('.rtrack i');
  var pct = parseFloat((fill.style.width || '0').replace('%', '')) || 0;
  placeLabels(r, pct);
});
document.querySelectorAll('[data-metric]').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var key = btn.getAttribute('data-metric');
    document.querySelectorAll('[data-metric]').forEach(function (b) {
      b.classList.toggle('active', b === btn);
    });
    document.querySelectorAll('.chart-title').forEach(function (t) {
      t.textContent = key === 'tokens' ? '利用量' : '金額';
    });
    document.querySelectorAll('[data-pie]').forEach(function (p) {
      p.hidden = p.getAttribute('data-pie') !== key;
    });
    document.querySelectorAll('[data-line]').forEach(function (p) {
      p.hidden = p.getAttribute('data-line') !== key;
    });
    document.querySelectorAll('[data-total]').forEach(function (s) {
      s.hidden = s.getAttribute('data-total') !== key;
    });
    document.querySelectorAll('[data-chart]').forEach(function (chart) {
      var rowsEl = chart.querySelector('.rows');
      var rows = Array.prototype.slice.call(rowsEl.children);
      var max = 0;
      rows.forEach(function (r) {
        max = Math.max(max, parseFloat(r.getAttribute('data-' + key)) || 0);
      });
      if (!max) max = 1;
      rows.sort(function (a, b) {
        return (parseFloat(b.getAttribute('data-' + key)) || 0)
             - (parseFloat(a.getAttribute('data-' + key)) || 0);
      });
      rows.forEach(function (r) {
        var v = parseFloat(r.getAttribute('data-' + key)) || 0;
        var pct = v / max * 100;
        r.querySelector('.rtrack i').style.width = pct + '%';
        var val = r.querySelector('.rvalue');
        val.textContent = r.getAttribute('data-' + key + '-label');
        placeLabels(r, pct);
        rowsEl.appendChild(r);
      });
    });
  });
});
</script>"""


def pie_chart(window: dict, color_of: dict, value_key: str = "total_tokens") -> str:
    """利用割合の円グラフ（モデル別スライス＋凡例）。

    スライスは量の多い順ではなく、Claude → Codex → Other のグループ順に
    配置する（グループ内は value_key の降順）。
    value_key は "total_tokens"（利用量）か "api_equivalent_cost_usd"（金額）。
    """
    ms = []
    for group in GROUPS:
        ms.extend(sorted(models_of(window, group), key=lambda m: -m[value_key]))
    total = sum(m[value_key] for m in ms) or 1
    cx, cy, r = 100.0, 100.0, 88.0
    angle = -90.0
    paths = []
    labels = []
    for m in ms:
        frac = m[value_key] / total
        sweep = frac * 360.0
        a0 = math.radians(angle)
        a1 = math.radians(angle + sweep)
        x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
        x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
        large = 1 if sweep > 180 else 0
        color = color_of.get(display_name(m["model"]), "#666")
        if frac >= 0.999:
            paths.append(f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="{color}"/>')
        else:
            paths.append(f'<path d="M {cx} {cy} L {x0:.1f} {y0:.1f} A {r} {r} 0 {large} 1 {x1:.1f} {y1:.1f} Z" '
                         f'fill="{color}" stroke="#fff" stroke-width="1.5"/>')
        if frac >= 0.05:
            am = math.radians(angle + sweep / 2)
            lx, ly = cx + r * 0.62 * math.cos(am), cy + r * 0.62 * math.sin(am)
            labels.append(f'<text x="{lx:.1f}" y="{ly:.1f}" font-size="12" font-weight="bold" '
                          f'fill="#fff" text-anchor="middle" dominant-baseline="central">'
                          f'{frac * 100:.0f}%</text>')
        angle += sweep
    svg = (f'<svg viewBox="0 0 200 200" role="img" aria-label="利用割合" '
           f'style="width:min(200px,44vw);height:auto;flex:none">{"".join(paths)}{"".join(labels)}</svg>')

    legend = []
    for group in GROUPS:
        gms = [m for m in ms if group_of(m["model"]) == group]
        if not gms:
            continue
        legend.append(f'<div style="font-weight:bold;margin-top:4px">{group}</div>')
        for m in gms:
            name = display_name(m["model"])
            legend.append(
                f'<div><span style="display:inline-block;width:10px;height:10px;'
                f'background:{color_of.get(name, "#666")};margin-right:6px"></span>{name}</div>')
    return (f'<div style="display:flex;gap:16px;align-items:flex-start;justify-content:center">{svg}'
            f'<div style="font-size:12px;line-height:1.35;min-width:0">{"".join(legend)}</div></div>')


def build_html(body: dict, today: datetime) -> str:
    color_of = assign_colors(body)
    panels = []
    for key, period_label, tab_id in (("last_7d", "週次", "weekly"),
                                      ("last_30d", "月次", "monthly")):
        w = body[key]
        parts = [f"<h2>{period_label}: {w['from']}〜{w['to']}</h2>"]
        totals_tok = []
        totals_cost = []
        charts = []
        for group in GROUPS:
            ms = models_of(w, group)
            if not ms:
                continue
            rows = [(display_name(m["model"]), m["total_tokens"],
                     m["api_equivalent_cost_usd"],
                     fmt_m(m["total_tokens"]),
                     f"${m['api_equivalent_cost_usd']:,.2f}")
                    for m in ms]
            charts.append(f"<h3>{group}</h3>")
            charts.append(bar_chart("利用量", rows, color_of))
            g_cost = sum(m["api_equivalent_cost_usd"] for m in ms)
            g_tok = sum(m["total_tokens"] for m in ms)
            totals_tok.append(f"{group} 合計: {fmt_m(g_tok)}")
            totals_cost.append(f"{group} 合計: ${g_cost:,.2f}")
        # ベンダ別合計は期間見出しの直下に置く（特定ベンダの節に属さないため）。
        # 他のグラフと同じ box セグメントに収め、箇条書きで示す。
        # 指標切替に連動し、選択中の指標の合計だけを見せる
        tok_items = "".join(f"<li>{t}</li>" for t in totals_tok)
        cost_items = "".join(f"<li>{t}</li>" for t in totals_cost)
        parts.append(f'<div class="chart seg"><ul class="totals-list" data-total="tokens">{tok_items}</ul>'
                     f'<ul class="totals-list" data-total="cost" hidden>{cost_items}</ul></div>')
        lc = line_chart(w)
        if lc:
            parts.append(lc)
        parts.extend(charts)
        parts.append(f'<div class="chart seg" data-pie="tokens"><div class="pie-title">利用割合（利用量）</div>'
                     f'{pie_chart(w, color_of, "total_tokens")}</div>')
        parts.append(f'<div class="chart seg" data-pie="cost" hidden><div class="pie-title">利用割合（金額）</div>'
                     f'{pie_chart(w, color_of, "api_equivalent_cost_usd")}</div>')
        panels.append(f'<div class="panel panel-{tab_id}">{"".join(parts)}</div>')

    return f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<title>AI利用量 {today.strftime('%Y-%m-%d')}</title>
<style>
:root{{color-scheme:light dark}}
body{{font-family:system-ui,-apple-system,"Hiragino Sans",sans-serif;max-width:960px;margin:12px auto;padding:0 16px;color:#1a1a1a;background:#fff;line-height:1.5}}
h1{{font-size:20px;margin:6px 0 2px}}
h2{{font-size:16px;margin:14px 0 4px}}
h3{{font-size:14px;margin:12px 0 4px}}
p{{margin:4px 0 8px}}
.tabs > input{{display:none}}
.tab-labels{{display:flex;gap:8px}}
.tab-labels label{{padding:6px 16px;border:1px solid #ccc;border-radius:999px;cursor:pointer;font-size:13px}}
.controls{{display:flex;justify-content:space-between;align-items:center;margin:12px 0}}
.metric-btns button{{padding:6px 16px;margin-left:6px;border:1px solid #ccc;border-radius:999px;background:#fff;cursor:pointer;font-size:13px;color:#1a1a1a}}
.metric-btns button.active{{background:#1a1a1a;color:#fff;border-color:#1a1a1a}}
.panel{{display:none}}
#tab-weekly:checked ~ .panel-weekly{{display:block}}
#tab-monthly:checked ~ .panel-monthly{{display:block}}
#tab-weekly:checked ~ .controls label[for='tab-weekly'],
#tab-monthly:checked ~ .controls label[for='tab-monthly']{{background:#1a1a1a;color:#fff;border-color:#1a1a1a}}
.chart{{border:1px solid #ddd;border-radius:8px;padding:10px 12px;background:#fff}}
.seg{{margin:8px 0}}
.totals-list{{margin:0;padding-left:1.3em}}
.totals-list li{{margin:2px 0}}
.pie-title{{font-weight:bold;font-size:13px;margin-bottom:6px}}
.chart-title{{font-weight:bold;font-size:13px;margin-bottom:6px}}
.row{{display:block;margin:5px 0;font-size:12px}}
.rbar{{position:relative;height:16px;display:block}}
.rtrack{{position:absolute;left:0;right:68px;top:0;height:16px}}
.rtrack i{{position:absolute;left:0;top:0;height:16px}}
.rname{{position:absolute;top:0;height:16px;display:flex;align-items:center;white-space:nowrap;font-weight:600}}
.rname.in{{color:#fff}}
.rname.out{{color:#1a1a1a}}
.rvalue{{position:absolute;top:0;height:16px;display:flex;align-items:center;white-space:nowrap}}
@media (max-width:560px){{
body{{padding:0 8px}}
.row{{font-size:11px}}
.rbar{{height:14px}}
.rtrack{{right:58px;height:14px}}
.rtrack i{{height:14px}}
.rname{{height:14px}}
.rvalue{{height:14px;font-size:11px}}
.chart{{padding:8px}}
.tab-labels label{{padding:5px 12px}}
.metric-btns button{{padding:5px 12px}}
}}
@media (prefers-color-scheme:dark){{
body{{background:#121212;color:#e8e8e8}}
.chart{{background:#1e1e1e;border-color:#333}}
.tab-labels label{{background:#1e1e1e;border-color:#555;color:#e8e8e8}}
#tab-weekly:checked ~ .controls label[for='tab-weekly'],
#tab-monthly:checked ~ .controls label[for='tab-monthly']{{background:#fff;color:#000;border-color:#fff}}
.metric-btns button{{background:#1e1e1e;border-color:#555;color:#e8e8e8}}
.metric-btns button.active{{background:#fff;color:#000;border-color:#fff}}
.rname.out{{color:#e8e8e8}}
}}
</style>
</head><body>
<h1>AI利用量</h1>
<p>金額は API 換算の参考値。</p>
<div class="tabs">
<input type="radio" name="period" id="tab-weekly" checked>
<input type="radio" name="period" id="tab-monthly">
<div class="controls">
<div class="tab-labels"><label for="tab-weekly">週次</label><label for="tab-monthly">月次</label></div>
<div class="metric-btns"><button type="button" data-metric="tokens" class="active">利用量</button><button type="button" data-metric="cost">金額</button></div>
</div>
{''.join(panels)}
</div>
{SORT_SCRIPT}
</body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="AI利用量レポート生成")
    parser.add_argument("--out", default=str(BASE / "reports"),
                        help="出力先ディレクトリ")
    parser.add_argument("--date", default=None,
                        help="日次テーブルの対象日 YYYY-MM-DD（既定は JST 12:00 前は前日、以降は当日）")
    parser.add_argument("--daily-json", default=None,
                        help="日次の /v1/report（group_by=model）応答 JSON ファイル")
    parser.add_argument("--weekly-json", default=None,
                        help="週次の /v1/report（group_by=model）応答 JSON ファイル")
    parser.add_argument("--monthly-json", default=None,
                        help="月次の /v1/report（group_by=model）応答 JSON ファイル")
    parser.add_argument("--weekly-days-json", default=None,
                        help="週次の /v1/report（group_by=day）応答 JSON ファイル")
    parser.add_argument("--monthly-days-json", default=None,
                        help="月次の /v1/report（group_by=day）応答 JSON ファイル")
    args = parser.parse_args()

    now = datetime.now(JST)
    dates = report_dates(now)
    daily_date = args.date or dates["daily"].isoformat()
    wf, wt = dates["week"][0].isoformat(), dates["week"][1].isoformat()
    mf, mt = dates["month"][0].isoformat(), dates["month"][1].isoformat()

    body = daily = None
    if args.weekly_json and args.monthly_json:
        body = {"last_7d": _window(json.loads(Path(args.weekly_json).read_text()), wf, wt),
                "last_30d": _window(json.loads(Path(args.monthly_json).read_text()), mf, mt)}
    if args.daily_json:
        daily = json.loads(Path(args.daily_json).read_text())
    if body is None or daily is None:
        fetched_body, fetched_daily = fetch_all(daily_date, dates["week"], dates["month"])
        body = body if body is not None else fetched_body
        daily = daily if daily is not None else fetched_daily
    if args.weekly_days_json:
        body["last_7d"]["by_day"] = _days(
            json.loads(Path(args.weekly_days_json).read_text()), wf, wt)
    if args.monthly_days_json:
        body["last_30d"]["by_day"] = _days(
            json.loads(Path(args.monthly_days_json).read_text()), mf, mt)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    tables = build_tables(daily, daily_date)
    (out_dir / "usage_tables.md").write_text(tables + "\n")
    (out_dir / "usage.html").write_text(build_html(body, now))
    print(tables)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
