"""
小澤ファイナンス - ギャップアップ全銘柄スキャナー
Version: 1.10（根本修正：英数字コード除外 → キャッシュ有効化 + 会社名表示）
作成: Claude（株式バックテストエンジニア）
窓口: 児玉圭司

変更履歴:
  v1.5 → ポートフォリオ選択UI + 会社名表示
  v1.6 → 個別株のみに絞り込み（コード範囲フィルター）
  v1.7 → フィルター強化（isin→直接比較 + JPX市場区分でETF/REIT/インフラをダブル除外）
  v1.8 → DataFrameフィルタ方式に変更（インデックスズレ修正）
  v1.9 → xlrd自動インストール追加
  v1.10 → 根本修正：130A等の英数字コードがint変換エラーでfallbackに落ちていた問題を修正
          → price_cacheが正しく使われるようになり実行時間が大幅短縮

使い方:
    pip install pandas numpy yfinance tqdm requests openpyxl xlrd
    python gap_up_scanner.py

★ v1.9以降：stock_list.csvの削除は不要です。バージョンが変わると自動で再取得します。
"""

import os
import sys
import time
import json
import math
import mimetypes
import subprocess
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import requests
import numpy as np
import pandas as pd

# xlrd自動インストール（.xls形式のJPXファイル読み込みに必要）
try:
    import xlrd
except ImportError:
    print("[自動インストール] xlrd をインストールします...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "xlrd", "-q"])
    print("[自動インストール] xlrd インストール完了")
from datetime import datetime
from tqdm import tqdm

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ==============================================================
# CONFIG
# ==============================================================
CONFIG = {
    "gap_threshold":    3.0,
    "max_gap_filter":   30.0,
    "entry_time":       "15:30",
    "exit_time":        "09:00",
    "position_size":    3_300_000,
    "margin":           1_000_000,
    "start_date":       "2015-01-01",
    "end_date":         "2026-04-30",
    "top_n":            3,
    "output_file":      "gap_up_report.html",
    "min_trades":       20,
    "min_data_days":    100,
    "html_detail_max":  200,
    "min_net_profit":   10_000,
    "macd_fast":        12,
    "macd_slow":        26,
    "macd_signal":      9,
}

# ==============================================================
# ★ v1.6 追加：除外コード帯（投資信託・ETF・REIT）
# ==============================================================
# 1300〜1999：上場投資信託（ETF）・J-REIT など
# 2000〜2999：投資信託パッケージ（小澤さんが「いらない」と言ったやつ）
# 3000〜9999：個別株（対象）
EXCLUDE_CODE_RANGE = range(1300, 3000)

# ==============================================================
# 会社名マップ
# ==============================================================
def load_name_map() -> dict:
    if not os.path.exists("stock_list.csv"):
        return {}
    try:
        df = pd.read_csv("stock_list.csv")
        if "name" in df.columns:
            return dict(zip(df["ticker"], df["name"].fillna("")))
    except Exception:
        pass
    return {}

# ==============================================================
# 【サブ1】stock_list_agent
# ==============================================================
STOCK_LIST_VERSION = "1.10"

def stock_list_agent() -> list:
    cache_path = "stock_list.csv"
    if os.path.exists(cache_path):
        df = pd.read_csv(cache_path)
        # バージョンチェック：古いCSVは自動再取得
        cached_ver = df.get("version", pd.Series([""])).iloc[0] if "version" in df.columns else ""
        has_name   = "name" in df.columns and df["name"].notna().any() and (df["name"].astype(str).str.strip() != "").any()
        if cached_ver == STOCK_LIST_VERSION and has_name:
            tickers = df["ticker"].tolist()
            print(f"  -> キャッシュから {len(tickers)} 銘柄読込（会社名: あり）")
            return tickers
        else:
            print(f"  -> CSVが古い or 会社名なし → 自動で再取得します（削除不要）")
            os.remove(cache_path)

    print("  -> JPX公式XLSからダウンロード試行...")
    tickers, names = [], []
    try:
        url = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
        r   = requests.get(url, timeout=30)
        if r.status_code == 200:
            df_jpx   = pd.read_excel(pd.io.common.BytesIO(r.content))

            # 列名確認（デバッグ）
            print(f"  -> JPX列名: {list(df_jpx.columns)}")

            code_col = next((c for c in df_jpx.columns if "コード" in str(c)), None)
            name_col = next((c for c in df_jpx.columns
                             if "銘柄名" in str(c) or "銘柄名称" in str(c)), None)
            mkt_col  = next((c for c in df_jpx.columns
                             if "市場" in str(c) or "商品区分" in str(c)), None)

            print(f"  -> コード列:{code_col} / 銘柄名列:{name_col} / 市場区分列:{mkt_col}")

            if code_col:
                work = df_jpx[df_jpx[code_col].notna()].copy()
                work["_code_str"] = work[code_col].astype(str).str.strip()

                # ★ v1.10 根本修正：純粋な4桁数字のみ残す（130A等の英数字コードを除外）
                work = work[work["_code_str"].str.match(r'^\d{4}$')].copy()
                work["_code_int"] = work["_code_str"].astype(int)

                # フィルター①：個別株のみ（3000〜9999）
                work = work[~((work["_code_int"] >= 1300) & (work["_code_int"] < 3000))].copy()

                # フィルター②：市場区分（ETF/ETN/REIT/インフラ除外）
                if mkt_col:
                    work = work[~work[mkt_col].astype(str).str.contains(
                        "ETF|ETN|REIT|インフラ", na=False)].copy()
                    print(f"  -> 市場区分フィルター適用（ETF/ETN/REIT/インフラ除外）")

                tickers = [f"{c}.T" for c in work["_code_str"]]
                names   = work[name_col].fillna("").tolist() if name_col else [""] * len(tickers)

                print(f"  -> JPX公式から {len(tickers)} 銘柄取得（個別株のみ）")
                if names and names[0]:
                    print(f"  -> ✅ 会社名取得成功: {names[0]}（{tickers[0]}）")
                else:
                    print(f"  -> ❌ 会社名列が見つかりません（name_col={name_col}）")
    except Exception as e:
        print(f"  -> JPX取得失敗（{e}）-> フォールバック")

    if not tickers:
        # ★ v1.6：フォールバックも3000〜に変更
        tickers = [f"{i}.T" for i in range(3000, 10000)]
        names   = [""] * len(tickers)
        print(f"  -> フォールバック: {len(tickers)} ティッカー（3000〜9999・個別株のみ）")

    pd.DataFrame({"ticker": tickers, "name": names, "version": [STOCK_LIST_VERSION]*len(tickers)}).to_csv(cache_path, index=False)
    return tickers

# ==============================================================
# 【サブ2】price_fetch_agent
# ==============================================================
def price_fetch_agent(tickers: list) -> None:
    import yfinance as yf
    os.makedirs("price_cache", exist_ok=True)
    need_fetch = [t for t in tickers if not os.path.exists(f"price_cache/{t}.pkl")]
    print(f"  -> 取得必要: {len(need_fetch)} 銘柄 / キャッシュ済み: {len(tickers)-len(need_fetch)} 銘柄")
    if not need_fetch:
        return
    for ticker in tqdm(need_fetch, desc="株価データ取得", ncols=80):
        try:
            df = yf.download(ticker, start=CONFIG["start_date"], end=CONFIG["end_date"],
                             progress=False, auto_adjust=True)
            if df is None or len(df) < CONFIG["min_data_days"]:
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df = df.droplevel(1, axis=1)
            df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
            if len(df) >= CONFIG["min_data_days"]:
                df.to_pickle(f"price_cache/{ticker}.pkl")
        except Exception:
            pass
        time.sleep(0.05)

# ==============================================================
# 【サブ3】backtest_agent
# ==============================================================
def run_backtest(df: pd.DataFrame, ticker: str):
    max_gap_filter = CONFIG["max_gap_filter"]
    position_size  = CONFIG["position_size"]
    margin         = CONFIG["margin"]
    gap_threshold  = CONFIG["gap_threshold"]
    min_net_profit = CONFIG["min_net_profit"]

    trades, equity, gap_up_count = [], float(margin), 0
    closes, opens, dates = df["Close"].values, df["Open"].values, df.index

    for i in range(len(df) - 1):
        close_t = float(closes[i])
        open_t1 = float(opens[i + 1])
        if close_t <= 0 or open_t1 <= 0:
            continue
        gap_pct = (open_t1 - close_t) / close_t * 100
        if abs(gap_pct) > max_gap_filter:
            continue
        if gap_pct >= gap_threshold:
            gap_up_count += 1
        shares = int(position_size / close_t)
        if shares == 0:
            continue
        pnl     = (open_t1 - close_t) * shares
        equity += pnl
        trades.append({
            "entry_date":  dates[i].strftime("%Y-%m-%d"),
            "exit_date":   dates[i + 1].strftime("%Y-%m-%d"),
            "entry_price": round(close_t, 2),
            "exit_price":  round(open_t1, 2),
            "shares":      shares,
            "gap_pct":     round(gap_pct, 2),
            "pnl":         round(pnl),
            "pnl_pct":     round(gap_pct, 2),
            "cumulative":  round(equity - margin),
        })

    if len(trades) < CONFIG["min_trades"]:
        return None
    if abs(equity - margin) < min_net_profit:
        return None

    monthly = {}
    for t in trades:
        y, m = t["entry_date"][:4], t["entry_date"][5:7]
        monthly.setdefault(y, {}).setdefault(m, 0)
        monthly[y][m] += t["pnl"]

    return {
        "ticker":       ticker,
        "trades":       trades,
        "equity_curve": [margin + t["cumulative"] for t in trades],
        "monthly":      monthly,
        "gap_up_count": gap_up_count,
        "raw_net":      equity - margin,
    }

def backtest_agent(tickers: list) -> list:
    results, skipped = [], 0
    for ticker in tqdm(tickers, desc="バックテスト実行", ncols=80):
        if not os.path.exists(f"price_cache/{ticker}.pkl"):
            skipped += 1
            continue
        try:
            df = pd.read_pickle(f"price_cache/{ticker}.pkl")
            r  = run_backtest(df, ticker)
            if r:
                results.append(r)
        except Exception as e:
            print(f"  [SKIP] {ticker}: {e}")
            skipped += 1
    print(f"  -> 有効: {len(results)} 銘柄 / スキップ: {skipped} 銘柄")
    return results

# ==============================================================
# 【サブ4】scoring_agent
# ==============================================================
def calc_kpi(result: dict) -> dict:
    trades = result["trades"]
    margin = CONFIG["margin"]
    net_profit   = sum(t["pnl"] for t in trades)
    gross_profit = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    gross_loss   = abs(sum(t["pnl"] for t in trades if t["pnl"] < 0))
    pf       = round(gross_profit / gross_loss, 3) if gross_loss > 0 else 999.99
    win_rate = round(len([t for t in trades if t["pnl"] > 0]) / len(trades) * 100, 1)
    start_dt = datetime.strptime(CONFIG["start_date"], "%Y-%m-%d")
    end_dt   = datetime.strptime(CONFIG["end_date"],   "%Y-%m-%d")
    years    = (end_dt - start_dt).days / 365.25
    final_eq = margin + net_profit
    cagr     = round(((final_eq / margin) ** (1 / years) - 1) * 100, 2) if final_eq > 0 and years > 0 else -100.0
    eq_series = pd.Series(result["equity_curve"])
    peak      = eq_series.cummax()
    max_dd    = round(((eq_series - peak) / peak).min() * 100, 2)
    returns  = [t["pnl"] / margin for t in trades]
    std_r    = float(np.std(returns))
    sharpe   = round((float(np.mean(returns)) / std_r) * math.sqrt(252), 2) if std_r > 0 else 0.0
    rf       = round(cagr / abs(max_dd), 2) if max_dd != 0 else 0.0
    gap_freq = round(result.get("gap_up_count", 0) / len(trades) * 100, 1)
    return {
        "net_profit":   round(net_profit),
        "cagr":         cagr,
        "pf":           pf,
        "win_rate":     win_rate,
        "max_dd":       max_dd,
        "sharpe":       sharpe,
        "rf":           rf,
        "total_trades": len(trades),
        "gap_freq":     gap_freq,
    }

def scoring_agent(results: list) -> list:
    for r in results:
        r["kpi"] = calc_kpi(r)
    before  = len(results)
    results = [r for r in results if not (r["kpi"]["pf"] >= 999.0 and r["kpi"]["win_rate"] < 1.0)]
    filtered = before - len(results)
    if filtered > 0:
        print(f"  -> 矛盾KPIフィルター: {filtered} 銘柄除外")
    all_cagr   = [r["kpi"]["cagr"] for r in results]
    cagr_min, cagr_max = min(all_cagr), max(all_cagr)
    cagr_range = cagr_max - cagr_min + 1e-9
    for r in results:
        kpi = r["kpi"]
        cagr_norm = (kpi["cagr"] - cagr_min) / cagr_range
        pf_norm   = min(kpi["pf"], 5.0) / 5.0
        rf_norm   = min(max(kpi["rf"], 0), 50) / 50.0
        r["score"] = round(pf_norm * 0.4 + cagr_norm * 0.3 + rf_norm * 0.3, 4)
    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
    return results

# ==============================================================
# 【サブ5】report_agent
# ==============================================================
def _month_color(v):
    if v > 0:
        return f"background:rgba(46,160,67,{min(max(v/500_000,0.15),0.7):.2f})"
    elif v < 0:
        return f"background:rgba(218,54,51,{min(max(abs(v)/500_000,0.15),0.7):.2f})"
    return "background:#f5f5f5"

def _monthly_html(monthly):
    months  = [f"{m:02d}" for m in range(1, 13)]
    mlabels = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
    rows = []
    for y in sorted(monthly.keys()):
        cells, ytotal = [], 0
        for m in months:
            v = monthly[y].get(m, 0)
            ytotal += v
            cells.append(f'<td style="{_month_color(v)}">{int(v/10000):+d}万</td>' if v != 0
                         else '<td style="background:#f5f5f5;color:#ccc">-</td>')
        cells.append(f'<td style="{_month_color(ytotal)};font-weight:bold">{int(ytotal/10000):+d}万</td>')
        rows.append(f'<tr><th>{y}</th>{"".join(cells)}</tr>')
    hdr = "".join(f"<th>{ml}</th>" for ml in mlabels)
    return f'<table class="monthly"><thead><tr><th>年</th>{hdr}<th>合計</th></tr></thead><tbody>{"".join(rows)}</tbody></table>'

def _trade_rows(trades, limit=50):
    rows = []
    for t in trades[-limit:]:
        cls = "pos" if t["pnl"] >= 0 else "neg"
        rows.append(f'<tr><td>{t["entry_date"]}</td><td>{t["exit_date"]}</td>'
                    f'<td>{t["entry_price"]:,.2f}</td><td>{t["exit_price"]:,.2f}</td>'
                    f'<td>{t["shares"]:,}</td>'
                    f'<td class="{cls}">{t["gap_pct"]:+.2f}%</td>'
                    f'<td class="{cls}">{t["pnl"]:+,}</td>'
                    f'<td>{t["cumulative"]:+,}</td></tr>')
    return "\n".join(rows)

def report_agent(ranked_results: list, name_map: dict) -> None:
    now_str    = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    top_n      = CONFIG["top_n"]
    detail_max = CONFIG["html_detail_max"]
    cfg        = CONFIG

    def dname(ticker):
        name = name_map.get(ticker, "")
        return f"{name}（{ticker}）" if name else ticker

    top3       = ranked_results[:3]
    sum_profit = sum(r["kpi"]["net_profit"] for r in top3)
    avg_cagr   = round(sum(r["kpi"]["cagr"]    for r in top3) / 3, 2)
    avg_pf     = round(sum(r["kpi"]["pf"]       for r in top3) / 3, 3)
    avg_wr     = round(sum(r["kpi"]["win_rate"] for r in top3) / 3, 1)
    avg_dd     = round(sum(r["kpi"]["max_dd"]   for r in top3) / 3, 2)
    avg_sharpe = round(sum(r["kpi"]["sharpe"]   for r in top3) / 3, 2)

    all_kpis_js = {}
    for r in ranked_results:
        k = r["kpi"]
        all_kpis_js[r["ticker"]] = {
            "np": k["net_profit"], "cagr": k["cagr"], "pf": k["pf"],
            "wr": k["win_rate"],   "dd":   k["max_dd"], "sh": k["sharpe"],
            "rf": k["rf"],         "tr":   k["total_trades"], "gf": k["gap_freq"],
            "sc": r["score"],      "rk":   r["rank"],
            "nm": name_map.get(r["ticker"], ""),
        }

    trade_data_js = {}
    for r in ranked_results[:detail_max]:
        trade_data_js[r["ticker"]] = [
            {
                "d": t["entry_date"], "x": t["exit_date"], "p": t["pnl"],
                "ep": t["entry_price"], "xp": t["exit_price"],
                "s": t["shares"], "g": t["gap_pct"],
            }
            for t in r["trades"]
        ]

    rank_rows = []
    for r in ranked_results:
        k  = r["kpi"]
        nc = "pos" if k["net_profit"] >= 0 else "neg"
        dc = "neg" if k["max_dd"] < 0 else ""
        rank_rows.append(
            f'<tr class="rank-row" data-rank="{r["rank"]}" data-ticker="{r["ticker"]}">'
            f'<td><input type="checkbox" class="stock-chk" data-ticker="{r["ticker"]}" onchange="onCheck(this)"></td>'
            f'<td>{r["rank"]}</td><td><b>{r["ticker"]}</b></td>'
            f'<td class="name-cell">{name_map.get(r["ticker"],"")}</td>'
            f'<td>{k["pf"]}</td><td class="pos">{k["cagr"]}%</td>'
            f'<td class="{dc}">{k["max_dd"]}%</td><td>{k["sharpe"]}</td>'
            f'<td>{k["win_rate"]}%</td><td>{k["total_trades"]}</td>'
            f'<td>{k["gap_freq"]}%</td>'
            f'<td class="{nc}">{k["net_profit"]:,}円</td>'
            f'<td>{r["score"]}</td></tr>'
        )

    detail_sections, chart_scripts = [], []
    for r in ranked_results[:detail_max]:
        k       = r["kpi"]
        rank    = r["rank"]
        ticker  = r["ticker"]
        display = "" if rank <= top_n else ' style="display:none"'
        dc      = "neg" if k["max_dd"] < 0 else "pos"
        nc      = "pos" if k["net_profit"] >= 0 else "neg"
        eq_labels = json.dumps([t["entry_date"] for t in r["trades"]])
        eq_data   = json.dumps(r["equity_curve"])

        detail_sections.append(f"""
<section class="detail-section" data-rank="{rank}" data-ticker="{ticker}"{display}>
  <h2><span class="rank-badge">{rank}位</span> {dname(ticker)}</h2>
  <div class="kpi-grid">
    <div class="kpi-card"><div class="kpi-label">純利益</div><div class="kpi-value {nc}">{k['net_profit']:,}円</div></div>
    <div class="kpi-card"><div class="kpi-label">CAGR</div><div class="kpi-value pos">{k['cagr']}%</div></div>
    <div class="kpi-card"><div class="kpi-label">PF</div><div class="kpi-value">{k['pf']}</div></div>
    <div class="kpi-card"><div class="kpi-label">勝率</div><div class="kpi-value">{k['win_rate']}%</div></div>
    <div class="kpi-card"><div class="kpi-label">最大DD</div><div class="kpi-value {dc}">{k['max_dd']}%</div></div>
    <div class="kpi-card"><div class="kpi-label">シャープ</div><div class="kpi-value">{k['sharpe']}</div></div>
    <div class="kpi-card"><div class="kpi-label">RF</div><div class="kpi-value">{k['rf']}</div></div>
    <div class="kpi-card"><div class="kpi-label">トレード回数</div><div class="kpi-value">{k['total_trades']}</div></div>
    <div class="kpi-card"><div class="kpi-label">GU頻度</div><div class="kpi-value">{k['gap_freq']}%</div></div>
  </div>
  <h3>資産曲線</h3>
  <div class="chart-wrap"><canvas id="chart_{rank}"></canvas></div>
  <h3>月次損益マトリクス</h3>
  <div class="table-wrap">{_monthly_html(r["monthly"])}</div>
  <h3>トレード一覧（直近50件）</h3>
  <div class="table-wrap">
    <table class="trades">
      <thead><tr><th>エントリー</th><th>エグジット</th>
      <th>前日終値</th><th>翌日始値</th><th>株数</th>
      <th>ギャップ</th><th>損益</th><th>累積</th></tr></thead>
      <tbody>{_trade_rows(r["trades"])}</tbody>
    </table>
  </div>
</section>""")

        chart_scripts.append(f"""
  DETAIL_CHARTS['{ticker}'] = new Chart(document.getElementById('chart_{rank}').getContext('2d'),{{
    type:'line',
    data:{{labels:{eq_labels},datasets:[{{label:'資産曲線',data:{eq_data},
      borderColor:'#2ea047',backgroundColor:'rgba(46,160,71,0.15)',
      borderWidth:2,pointRadius:0,tension:0.1,fill:true}}]}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{x:{{ticks:{{maxTicksLimit:8}}}},
        y:{{ticks:{{callback:v=>(v/10000).toFixed(0)+'万'}}}}}}}}
  }});""")

    all_kpis_json   = json.dumps(all_kpis_js,   ensure_ascii=False)
    trade_data_json = json.dumps(trade_data_js, ensure_ascii=False)

    html = f"""<!DOCTYPE html>
<html lang="ja"><head><meta charset="UTF-8">
<title>小澤ファイナンス｜ギャップアップ全銘柄スキャン v1.10</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Hiragino Sans',sans-serif;margin:0;padding:24px 24px 120px;background:#f6f8fa;color:#1f2328;line-height:1.5}}
.container{{max-width:1280px;margin:0 auto}}
header{{background:linear-gradient(135deg,#1a1f2e,#2d3748);color:white;padding:32px;border-radius:12px;margin-bottom:24px}}
header h1{{margin:0 0 8px;font-size:28px}}
header .meta{{opacity:.85;font-size:14px}}
header .config{{margin-top:16px;padding:16px;background:rgba(255,255,255,.08);border-radius:8px;font-size:13px;line-height:1.8}}
.badge-v{{display:inline-block;background:#2da44e;color:white;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:8px;vertical-align:middle}}
.summary-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;margin-bottom:24px}}
.summary-card{{background:white;padding:20px;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,.06);text-align:center}}
.summary-card .label{{font-size:12px;color:#656d76;margin-bottom:6px}}
.summary-card .value{{font-size:20px;font-weight:700}}
.summary-card .value.pos{{color:#2ea047}}.summary-card .value.neg{{color:#da3633}}
.toggle-bar{{background:white;padding:16px;border-radius:8px;margin-bottom:16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.toggle-bar .label{{font-weight:600;color:#656d76}}
.toggle-btn{{padding:8px 18px;border:1px solid #d0d7de;background:white;border-radius:6px;cursor:pointer;font-weight:600;transition:all .15s}}
.toggle-btn:hover{{background:#f6f8fa}}.toggle-btn.active{{background:#2da44e;color:white;border-color:#2da44e}}
.toggle-bar input{{padding:8px;border:1px solid #d0d7de;border-radius:6px;width:80px;font-size:14px}}
.period-bar{{background:white;padding:16px;border-radius:8px;margin-bottom:16px;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.period-bar .label{{font-weight:600;color:#656d76}}
.period-bar select{{padding:8px 12px;border:1px solid #d0d7de;border-radius:6px;background:white;font-size:14px}}
.period-note{{font-size:12px;color:#656d76}}
.ranking,.trades,.monthly{{width:100%;border-collapse:collapse;background:white;border-radius:8px;overflow:hidden}}
.ranking th,.trades th,.monthly th{{background:#f6f8fa;padding:10px;text-align:center;font-size:12px;color:#656d76;border-bottom:1px solid #d0d7de}}
.ranking td,.trades td,.monthly td{{padding:10px;text-align:center;border-bottom:1px solid #eaeef2;font-size:13px}}
.ranking td.name-cell{{text-align:left;font-size:12px;color:#444}}
.ranking tr:hover{{background:#f6f8fa}}
.ranking tr.selected{{background:#f0fff4 !important}}
.table-wrap{{overflow-x:auto;margin-bottom:16px}}
.detail-section{{background:white;padding:24px;border-radius:12px;margin-bottom:24px;box-shadow:0 1px 3px rgba(0,0,0,.06)}}
.detail-section h2{{margin:0 0 16px;padding-bottom:12px;border-bottom:2px solid #eaeef2;display:flex;align-items:center;gap:12px;flex-wrap:wrap}}
.detail-section h3{{margin:24px 0 12px;font-size:16px}}
.rank-badge{{background:#2da44e;color:white;padding:4px 12px;border-radius:12px;font-size:14px;white-space:nowrap}}
.kpi-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:16px}}
.kpi-card{{background:#f6f8fa;padding:12px;border-radius:6px;text-align:center}}
.kpi-card .kpi-label{{font-size:11px;color:#656d76;margin-bottom:4px}}
.kpi-card .kpi-value{{font-size:16px;font-weight:700}}
.kpi-card .kpi-value.pos{{color:#2ea047}}.kpi-card .kpi-value.neg{{color:#da3633}}
.chart-wrap{{height:280px;position:relative}}
.pos{{color:#2ea047;font-weight:600}}.neg{{color:#da3633;font-weight:600}}
.note-box{{background:#fff8e1;border:1px solid #ffc107;border-radius:8px;padding:12px 16px;font-size:13px;color:#555;margin-bottom:16px}}
#pf-panel{{position:fixed;bottom:0;left:0;right:0;background:white;border-top:3px solid #2da44e;box-shadow:0 -4px 20px rgba(0,0,0,.15);z-index:1000;transition:transform .3s ease}}
#pf-panel.collapsed{{transform:translateY(calc(100% - 52px))}}
#pf-header{{display:flex;align-items:center;gap:12px;padding:12px 24px;background:#f0fff4;cursor:pointer}}
#pf-header h3{{margin:0;font-size:16px;color:#1f2328}}
#pf-count-badge{{background:#2da44e;color:white;font-size:13px;font-weight:700;padding:2px 10px;border-radius:12px}}
#pf-header .pf-btns{{margin-left:auto;display:flex;gap:8px}}
#pf-header .pf-btns button{{padding:6px 14px;border-radius:6px;border:1px solid #d0d7de;background:white;cursor:pointer;font-size:13px;font-weight:600}}
#pf-header .pf-btns .btn-clear{{color:#da3633;border-color:#da3633}}
#pf-header .pf-btns .btn-collapse{{color:#656d76}}
#pf-body{{padding:16px 24px 24px}}
#pf-tickers{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:16px;min-height:32px}}
.pf-tag{{background:#e6f4ea;color:#1a7f37;padding:4px 10px;border-radius:12px;font-size:13px;font-weight:600;display:flex;align-items:center;gap:6px}}
.pf-tag button{{background:none;border:none;color:#da3633;cursor:pointer;font-size:14px;line-height:1;padding:0}}
#pf-kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:16px}}
.pf-kpi-card{{background:#f6f8fa;padding:12px;border-radius:6px;text-align:center}}
.pf-kpi-card .kpi-label{{font-size:11px;color:#656d76;margin-bottom:4px}}
.pf-kpi-card .kpi-value{{font-size:18px;font-weight:700}}
.pf-kpi-card .kpi-value.pos{{color:#2ea047}}.pf-kpi-card .kpi-value.neg{{color:#da3633}}
#pf-chart-wrap{{height:220px;position:relative}}
#pf-empty{{color:#656d76;font-size:14px;text-align:center;padding:24px}}
footer{{text-align:center;color:#656d76;font-size:12px;margin-top:32px;padding:16px}}
</style></head><body>
<div class="container">
<header>
  <h1>小澤ファイナンス｜ギャップアップ全銘柄スキャン <span class="badge-v">v1.8</span></h1>
  <div class="meta">スキャン日時：{now_str}　／　有効銘柄：{len(ranked_results)}銘柄（個別株のみ・ETF/投信除外）</div>
  <div class="config">
    戦略：<b>毎日 前日終値エントリー → 翌日始値エグジット（全オーバーナイト保有）</b><br>
    ポジション: <b>¥{cfg['position_size']:,}</b> / 証拠金: <b>¥{cfg['margin']:,}</b>　
    期間: <b>{cfg['start_date']} 〜 {cfg['end_date']}</b><br>
    対象：<b>個別株のみ（3000〜9999番台）　ETF・投資信託・REIT 除外済み</b>
  </div>
</header>

<div class="summary-grid">
  <div class="summary-card"><div class="label">上位3合算純利益</div><div class="value pos">{sum_profit:,}円</div></div>
  <div class="summary-card"><div class="label">平均CAGR</div><div class="value {'pos' if avg_cagr>=0 else 'neg'}">{avg_cagr}%</div></div>
  <div class="summary-card"><div class="label">平均PF</div><div class="value">{avg_pf}</div></div>
  <div class="summary-card"><div class="label">平均勝率</div><div class="value">{avg_wr}%</div></div>
  <div class="summary-card"><div class="label">平均最大DD</div><div class="value neg">{avg_dd}%</div></div>
  <div class="summary-card"><div class="label">平均シャープ</div><div class="value">{avg_sharpe}</div></div>
</div>

<div class="toggle-bar">
  <span class="label">表示銘柄数：</span>
  <button class="toggle-btn" onclick="setTopN(3,this)">3</button>
  <button class="toggle-btn" onclick="setTopN(5,this)">5</button>
  <button class="toggle-btn" onclick="setTopN(10,this)">10</button>
  <button class="toggle-btn" onclick="setTopN(20,this)">20</button>
  <input type="number" id="customN" min="1" max="{detail_max}" placeholder="任意">
  <button class="toggle-btn" onclick="setTopN(parseInt(document.getElementById('customN').value)||3,this)">適用</button>
</div>

<div class="period-bar">
  <span class="label">表示年月：</span>
  <select id="periodSelect" onchange="applyPeriodFilter(this.value)">
    <option value="all">全期間</option>
  </select>
  <span id="periodStatus" class="period-note">グラフ・トレード一覧・選択ポートフォリオは全期間を表示中</span>
</div>

<h2 style="margin:24px 0 8px">ランキング（全{len(ranked_results)}銘柄）</h2>
<p style="font-size:13px;color:#656d76;margin:0 0 12px">
  チェックボックスで銘柄を選択すると、下部パネルに合算ポートフォリオが表示されます
</p>
<div class="table-wrap">
  <table class="ranking">
    <thead><tr>
      <th>選択</th><th>順位</th><th>コード</th><th>会社名</th><th>PF</th>
      <th>CAGR</th><th>最大DD</th><th>シャープ</th>
      <th>勝率</th><th>回数</th><th>GU頻度</th><th>純利益</th><th>スコア</th>
    </tr></thead>
    <tbody>{"".join(rank_rows)}</tbody>
  </table>
</div>

<div class="note-box">
  詳細チャート・月次マトリクスは上位{detail_max}銘柄のみ（軽量化のため）
</div>
{"".join(detail_sections)}

<footer>
  小澤ファイナンス　ギャップアップ全銘柄スキャナー v1.6　|
  対象：個別株のみ（ETF・投資信託・REIT除外）　|　データ：yfinance<br>
  ※バックテストは過去データです。将来の利益を保証しません。
</footer>
</div>

<!-- ポートフォリオパネル -->
<div id="pf-panel" class="collapsed">
  <div id="pf-header" onclick="togglePanel()">
    <h3>ポートフォリオ</h3>
    <span id="pf-count-badge">0銘柄選択中</span>
    <div class="pf-btns" onclick="event.stopPropagation()">
      <button class="btn-clear" onclick="clearAll()">クリア</button>
      <button class="btn-collapse" id="btn-collapse-text">▲ 閉じる</button>
    </div>
  </div>
  <div id="pf-body">
    <div id="pf-tickers"></div>
    <div id="pf-empty">銘柄を選択するとポートフォリオが表示されます</div>
    <div id="pf-kpis" style="display:none"></div>
    <div id="pf-chart-wrap" style="display:none;margin-top:16px">
      <h4 style="margin:0 0 8px;font-size:14px;color:#656d76">合算資産曲線</h4>
      <div style="height:220px;position:relative"><canvas id="pf-chart"></canvas></div>
    </div>
  </div>
</div>

<script>
const ALL_KPIS   = {all_kpis_json};
const TRADE_DATA = {trade_data_json};
const CFG_MARGIN = {cfg['margin']};
const DEFAULT_TOP_N = {top_n};
const DETAIL_MAX = {detail_max};

let selected = new Set();
let pfChart  = null;
let panelOpen = false;
let activePeriod = 'all';
const DETAIL_CHARTS = {{}};

function periodTrades(ticker) {{
  const trades = TRADE_DATA[ticker] || [];
  return activePeriod === 'all' ? trades : trades.filter(tr => tr.d.startsWith(activePeriod));
}}

function formatTradeRows(trades) {{
  let cumulative = 0;
  return trades.slice(-50).map(tr => {{
    cumulative += tr.p;
    const cls = tr.p >= 0 ? 'pos' : 'neg';
    return `<tr><td>${{tr.d}}</td><td>${{tr.x}}</td>` +
      `<td>${{Number(tr.ep).toLocaleString(undefined, {{minimumFractionDigits:2, maximumFractionDigits:2}})}}</td>` +
      `<td>${{Number(tr.xp).toLocaleString(undefined, {{minimumFractionDigits:2, maximumFractionDigits:2}})}}</td>` +
      `<td>${{Number(tr.s).toLocaleString()}}</td>` +
      `<td class="${{cls}}">${{tr.g >= 0 ? '+' : ''}}${{Number(tr.g).toFixed(2)}}%</td>` +
      `<td class="${{cls}}">${{tr.p >= 0 ? '+' : ''}}${{Number(tr.p).toLocaleString()}}</td>` +
      `<td>${{cumulative >= 0 ? '+' : ''}}${{cumulative.toLocaleString()}}</td></tr>`;
  }}).join('');
}}

function updateDetailViews() {{
  document.querySelectorAll('.detail-section').forEach(section => {{
    const ticker = section.dataset.ticker;
    const trades = periodTrades(ticker);
    const chart = DETAIL_CHARTS[ticker];
    if(chart) {{
      let equity = CFG_MARGIN;
      chart.data.labels = trades.map(tr => tr.d);
      chart.data.datasets[0].data = trades.map(tr => equity += tr.p);
      chart.update();
    }}
    const body = section.querySelector('tbody');
    if(body) {{
      body.innerHTML = trades.length ? formatTradeRows(trades)
        : '<tr><td colspan="8" style="color:#656d76">選択した年月のトレードはありません</td></tr>';
    }}
  }});
}}

function applyPeriodFilter(period) {{
  activePeriod = period;
  const label = period === 'all' ? '全期間' : period.replace('-', '年') + '月';
  document.getElementById('periodStatus').textContent =
    `グラフ・トレード一覧・選択ポートフォリオは${{label}}を表示中（ランキング・長期KPIは全期間）`;
  updateDetailViews();
  updatePortfolioPanel();
}}

function setupPeriodSelector() {{
  const periods = new Set();
  Object.values(TRADE_DATA).forEach(trades => trades.forEach(tr => periods.add(tr.d.slice(0, 7))));
  const select = document.getElementById('periodSelect');
  Array.from(periods).sort().reverse().forEach(period => {{
    const option = document.createElement('option');
    option.value = period;
    option.textContent = period.replace('-', '年') + '月';
    select.appendChild(option);
  }});
}}

function setTopN(n, btn) {{
  document.querySelectorAll('.rank-row').forEach((el,i) => el.style.display = i < n ? '' : 'none');
  document.querySelectorAll('.detail-section').forEach((el,i) => el.style.display = i < n ? '' : 'none');
  document.querySelectorAll('.toggle-btn').forEach(b => b.classList.remove('active'));
  if(btn) btn.classList.add('active');
}}

function onCheck(chk) {{
  const ticker = chk.dataset.ticker;
  const row    = chk.closest('tr');
  if(chk.checked) {{ selected.add(ticker); row.classList.add('selected'); }}
  else {{ selected.delete(ticker); row.classList.remove('selected'); }}
  updatePortfolioPanel();
  if(selected.size > 0 && !panelOpen) openPanel();
  if(selected.size === 0) closePanel();
}}

function clearAll() {{
  selected.clear();
  document.querySelectorAll('.stock-chk').forEach(c => {{
    c.checked = false; c.closest('tr').classList.remove('selected');
  }});
  updatePortfolioPanel(); closePanel();
}}

function togglePanel() {{ panelOpen ? closePanel() : openPanel(); }}
function openPanel() {{
  panelOpen = true;
  document.getElementById('pf-panel').classList.remove('collapsed');
  document.getElementById('btn-collapse-text').textContent = '▼ 閉じる';
}}
function closePanel() {{
  panelOpen = false;
  document.getElementById('pf-panel').classList.add('collapsed');
  document.getElementById('btn-collapse-text').textContent = '▲ 開く';
}}

function updatePortfolioPanel() {{
  const tickers = Array.from(selected);
  document.getElementById('pf-count-badge').textContent = `${{tickers.length}}銘柄選択中`;
  const tagsEl = document.getElementById('pf-tickers');
  tagsEl.innerHTML = tickers.map(t => {{
    const nm = ALL_KPIS[t] ? ALL_KPIS[t].nm : '';
    const label = nm ? `${{nm}}(${{t}})` : t;
    return `<span class="pf-tag">${{label}}<button onclick="removeOne('${{t}}')">×</button></span>`;
  }}).join('');
  const emptyEl   = document.getElementById('pf-empty');
  const kpisEl    = document.getElementById('pf-kpis');
  const chartWrap = document.getElementById('pf-chart-wrap');
  if(tickers.length === 0) {{
    emptyEl.style.display = ''; kpisEl.style.display = 'none'; chartWrap.style.display = 'none'; return;
  }}
  emptyEl.style.display = 'none'; kpisEl.style.display = '';
  let totalProfit = 0, wins = 0, grossProfit = 0, grossLoss = 0;
  const allPnls = [];
  for(const t of tickers) {{
    if(TRADE_DATA[t]) {{
      for(const tr of periodTrades(t)) {{
        totalProfit += tr.p;
        if(tr.p > 0) {{ wins++; grossProfit += tr.p; }}
        else if(tr.p < 0) {{ grossLoss += Math.abs(tr.p); }}
        allPnls.push(tr.p);
      }}
    }}
  }}
  const n       = tickers.length;
  const margin  = n * CFG_MARGIN;
  const finalEq = margin + totalProfit;
  const years   = activePeriod === 'all' ? 11.33 : (1 / 12);
  const cagr    = finalEq > 0 ? (Math.pow(finalEq / margin, 1 / years) - 1) * 100 : -100;
  const pf      = grossLoss > 0 ? (grossProfit / grossLoss) : 999.99;
  const winRate = allPnls.length > 0 ? (wins / allPnls.length * 100) : 0;
  const worstDD = Math.min(...tickers.map(t => ALL_KPIS[t]?.dd || 0));
  const avgSharpe = tickers.reduce((s,t) => s + (ALL_KPIS[t]?.sh||0), 0) / n;
  kpisEl.innerHTML = `
    <div class="pf-kpi-card"><div class="kpi-label">合算純利益</div>
      <div class="kpi-value ${{totalProfit>=0?'pos':'neg'}}">${{totalProfit.toLocaleString()}}円</div></div>
    <div class="pf-kpi-card"><div class="kpi-label">合算CAGR</div>
      <div class="kpi-value ${{cagr>=0?'pos':'neg'}}">${{cagr.toFixed(2)}}%</div></div>
    <div class="pf-kpi-card"><div class="kpi-label">合算PF</div>
      <div class="kpi-value">${{pf >= 999 ? '999.99' : pf.toFixed(3)}}</div></div>
    <div class="pf-kpi-card"><div class="kpi-label">合算勝率</div>
      <div class="kpi-value">${{winRate.toFixed(1)}}%</div></div>
    <div class="pf-kpi-card"><div class="kpi-label">最悪DD銘柄</div>
      <div class="kpi-value neg">${{worstDD.toFixed(2)}}%</div></div>
    <div class="pf-kpi-card"><div class="kpi-label">平均シャープ</div>
      <div class="kpi-value">${{avgSharpe.toFixed(2)}}</div></div>
    <div class="pf-kpi-card"><div class="kpi-label">合算証拠金</div>
      <div class="kpi-value">${{(margin/10000).toFixed(0)}}万円</div></div>
    <div class="pf-kpi-card"><div class="kpi-label">選択銘柄数</div>
      <div class="kpi-value">${{n}}銘柄</div></div>`;
  const tradeable = tickers.filter(t => TRADE_DATA[t]);
  if(tradeable.length > 0) {{ chartWrap.style.display = ''; drawPortfolioChart(tradeable, n * CFG_MARGIN); }}
  else {{ chartWrap.style.display = 'none'; }}
}}

function removeOne(ticker) {{
  selected.delete(ticker);
  const chk = document.querySelector(`.stock-chk[data-ticker="${{ticker}}"]`);
  if(chk) {{ chk.checked = false; chk.closest('tr').classList.remove('selected'); }}
  updatePortfolioPanel();
  if(selected.size === 0) closePanel();
}}

function drawPortfolioChart(tickers, totalMargin) {{
  const byDate = {{}};
  for(const t of tickers) {{
    for(const tr of periodTrades(t)) {{ byDate[tr.d] = (byDate[tr.d] || 0) + tr.p; }}
  }}
  const dates = Object.keys(byDate).sort();
  const curve = []; let equity = totalMargin;
  for(const d of dates) {{ equity += byDate[d]; curve.push(equity); }}
  const ctx = document.getElementById('pf-chart').getContext('2d');
  if(pfChart) pfChart.destroy();
  pfChart = new Chart(ctx, {{
    type:'line',
    data:{{labels:dates,datasets:[{{label:'合算資産曲線',data:curve,
      borderColor:'#2da44e',backgroundColor:'rgba(45,164,78,0.12)',
      borderWidth:2,pointRadius:0,tension:0.1,fill:true}}]}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{x:{{ticks:{{maxTicksLimit:8}}}},y:{{ticks:{{callback:v=>(v/10000).toFixed(0)+'万'}}}}}}}}
  }});
}}

document.addEventListener('DOMContentLoaded', () => {{
  {"".join(chart_scripts)}
  setupPeriodSelector();
  const btn = Array.from(document.querySelectorAll('.toggle-btn')).find(b => b.textContent.trim() === String(DEFAULT_TOP_N));
  setTopN(DEFAULT_TOP_N, btn);
}});
</script>
</body></html>"""

    with open(cfg["output_file"], "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  -> {cfg['output_file']} を出力しました")


# ==============================================================
# 【親】orchestrator
# ==============================================================
def orchestrator() -> None:
    print("=" * 60)
    print("小澤ファイナンス ギャップアップ全銘柄スキャナー v1.8 起動")
    print("対象：個別株のみ（ETF・投資信託・REIT 除外）")
    print("=" * 60)

    name_map = load_name_map()

    print("\n[STEP 1/5] 銘柄リスト取得中...")
    tickers  = stock_list_agent()
    name_map = load_name_map()
    if not tickers:
        print("[FATAL] 銘柄リスト取得失敗")
        return

    print(f"\n[STEP 2/5] 株価データ取得中... ({len(tickers)}銘柄)")
    price_fetch_agent(tickers)

    print("\n[STEP 3/5] バックテスト実行中...")
    results = backtest_agent(tickers)
    if not results:
        print("[FATAL] バックテスト結果が0件")
        return

    print(f"\n[STEP 4/5] スコアリング中... ({len(results)}銘柄)")
    ranked_results = scoring_agent(results)

    print("\n[STEP 5/5] HTMLレポート生成中...")
    report_agent(ranked_results, name_map)

    top1 = ranked_results[0]
    k    = top1["kpi"]
    name = name_map.get(top1["ticker"], top1["ticker"])
    print("\n" + "=" * 60)
    print(f"[完了] -> {CONFIG['output_file']}")
    print(f"   有効銘柄: {len(ranked_results)} 銘柄（個別株のみ）")
    print(f"   1位: {name}（{top1['ticker']}）")
    print(f"   PF:{k['pf']}  CAGR:{k['cagr']}%  勝率:{k['win_rate']}%  DD:{k['max_dd']}%")
    if not name_map:
        print("\n   ★ 会社名が取得できませんでした。再実行してみてください")
    print("=" * 60)


# ==============================================================
# Yahoo Finance 実データ検証用ローカルAPI
# ==============================================================
def _prototype_metrics(df: pd.DataFrame, ticker: str) -> dict:
    """Yahoo Finance日足から、プロトタイプ画面用の実データを生成する。"""
    work = df[["Open", "Close"]].dropna().copy()
    margin = 455_000
    position_size = 1_500_000
    trades = []
    for i in range(len(work) - 1):
        close_price = float(work["Close"].iloc[i])
        open_price = float(work["Open"].iloc[i + 1])
        if close_price <= 0 or open_price <= 0:
            continue
        gap_pct = (open_price - close_price) / close_price * 100
        if abs(gap_pct) > CONFIG["max_gap_filter"]:
            continue
        shares = int(position_size / close_price)
        pnl = (open_price - close_price) * shares
        trades.append({
            "date": work.index[i + 1],
            "pnl": float(pnl),
            "gap_pct": gap_pct,
        })
    if not trades:
        raise ValueError("有効なオーバーナイトデータがありません")

    trade_df = pd.DataFrame(trades).set_index("date").sort_index()
    pnl = trade_df["pnl"]
    wins, losses = pnl[pnl > 0], pnl[pnl < 0]
    gross_profit, gross_loss = float(wins.sum()), abs(float(losses.sum()))
    total_pnl = float(pnl.sum())
    equity = margin + pnl.cumsum()
    running_peak = equity.cummax()
    drawdown_jpy = equity - running_peak
    dd_date = drawdown_jpy.idxmin()
    max_dd_jpy = float(drawdown_jpy.min())
    peak_value = float(running_peak.loc[dd_date])
    max_dd_peak_pct = max_dd_jpy / peak_value * 100 if peak_value else 0.0
    min_date = equity.idxmin()
    start, end = trade_df.index.min(), trade_df.index.max()
    years = max((end - start).days / 365.25, 1 / 365.25)
    final_equity = margin + total_pnl
    cagr = ((final_equity / margin) ** (1 / years) - 1) * 100 if final_equity > 0 else -100.0

    monthly_pnl = pnl.resample("ME").sum()
    equity_curve = [
        {"date": idx.strftime("%Y-%m"), "equity": round(float(margin + monthly_pnl.loc[:idx].sum()), 2)}
        for idx in monthly_pnl.index
    ]
    monthly_matrix = {}
    for idx, value in monthly_pnl.items():
        monthly_matrix.setdefault(str(idx.year), {})[str(idx.month)] = float(value / margin * 100)
    for year in monthly_matrix:
        for month in range(1, 13):
            monthly_matrix[year].setdefault(str(month), 0.0)

    yearly = []
    for year, group in trade_df.groupby(trade_df.index.year):
        gpnl = group["pnl"]
        gprofit = float(gpnl[gpnl > 0].sum())
        gloss = abs(float(gpnl[gpnl < 0].sum()))
        yearly.append({
            "year": int(year),
            "n": int(len(group)),
            "pnl": round(float(gpnl.sum()), 2),
            "pnl_pct": float(gpnl.sum() / margin * 100),
            "win_rate": float((gpnl > 0).mean() * 100),
            "pf": gprofit / gloss if gloss else 9999.99,
        })
    yearly_returns = np.array([row["pnl"] / margin for row in yearly], dtype=float)
    sharpe = float(yearly_returns.mean() / yearly_returns.std(ddof=1)) if len(yearly_returns) > 1 and yearly_returns.std(ddof=1) > 0 else 0.0

    def max_streak(values: list, positive: bool) -> int:
        best = current = 0
        for value in values:
            if (value > 0) == positive:
                current += 1
                best = max(best, current)
            else:
                current = 0
        return best

    metrics = {
        "label": ticker.replace(".T", ""),
        "n_trades": int(len(pnl)),
        "n_wins": int((pnl > 0).sum()),
        "n_losses": int((pnl < 0).sum()),
        "win_rate": float((pnl > 0).mean() * 100),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "biggest_win": float(pnl.max()),
        "biggest_loss": float(pnl.min()),
        "total_pnl": total_pnl,
        "pf": gross_profit / gross_loss if gross_loss else 9999.99,
        "rf": total_pnl / abs(max_dd_jpy) if max_dd_jpy else 9999.99,
        "expectancy": float(pnl.mean()),
        "cagr": cagr,
        "annual_return": total_pnl / margin / years * 100,
        "total_return_pct": total_pnl / margin * 100,
        "sharpe": sharpe,
        "years": years,
        "start": start.strftime("%Y-%m-%d"),
        "end": end.strftime("%Y-%m-%d"),
        "final_equity": final_equity,
        "initial_margin": float(margin),
        "max_dd_peak_pct": max_dd_peak_pct,
        "max_dd_peak_jpy": max_dd_jpy,
        "max_dd_peak_date": dd_date.strftime("%Y-%m-%d"),
        "peak_peak_date": running_peak.loc[:dd_date].idxmax().strftime("%Y-%m-%d"),
        "peak_peak_value": peak_value,
        "recovery_peak_date": None,
        "recovery_peak_days": None,
        "max_dd_init_pct": max_dd_jpy / margin * 100,
        "max_dd_init_jpy": max_dd_jpy,
        "max_dd_init_date": dd_date.strftime("%Y-%m-%d"),
        "max_dd_jpy": max_dd_jpy,
        "max_dd_jpy_pct_peak": max_dd_peak_pct,
        "max_dd_jpy_pct_init": max_dd_jpy / margin * 100,
        "max_dd_jpy_date": dd_date.strftime("%Y-%m-%d"),
        "peak_jpy_date": running_peak.loc[:dd_date].idxmax().strftime("%Y-%m-%d"),
        "peak_jpy_value": peak_value,
        "recovery_jpy_date": None,
        "recovery_jpy_days": None,
        "min_equity": float(equity.min()),
        "min_equity_date": min_date.strftime("%Y-%m-%d"),
        "min_equity_loss_jpy": float(equity.min() - margin),
        "min_equity_loss_pct": float((equity.min() - margin) / margin * 100),
        "max_win_streak": max_streak(pnl.tolist(), True),
        "max_loss_streak": max_streak(pnl.tolist(), False),
    }
    return {"metrics": metrics, "equity_curve": equity_curve, "monthly_matrix": monthly_matrix, "yearly": yearly}


def _load_yahoo_prototype_data(code: str) -> dict:
    """キャッシュ優先でYahoo Finance実データを読み、画面用データを返す。"""
    ticker = f"{code}.T"
    cache_path = os.path.join("price_cache", f"{ticker}.pkl")
    source = "Yahoo Finance cache"
    if os.path.exists(cache_path):
        df = pd.read_pickle(cache_path)
    else:
        import yfinance as yf
        df = yf.download(ticker, start=CONFIG["start_date"], end=CONFIG["end_date"],
                         progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        if df is None or len(df) < CONFIG["min_data_days"]:
            raise ValueError("Yahoo Financeから十分なデータを取得できませんでした")
        df = df[["Open", "High", "Low", "Close", "Volume"]].dropna()
        os.makedirs("price_cache", exist_ok=True)
        df.to_pickle(cache_path)
        source = "Yahoo Finance download"
    result = _prototype_metrics(df, ticker)
    result["source"] = source
    result["ticker"] = ticker
    return result


def _calc_technical_signal(df: pd.DataFrame, ticker: str) -> dict:
    """最新日足の陽線・陰線とMACDによる方向判断を返す。"""
    work = df[["Open", "Close"]].dropna().copy()
    if len(work) < CONFIG["macd_slow"] + CONFIG["macd_signal"]:
        raise ValueError("MACD判定に必要な日足データが不足しています")

    close = work["Close"].astype(float)
    macd = close.ewm(span=CONFIG["macd_fast"], adjust=False).mean() - close.ewm(
        span=CONFIG["macd_slow"], adjust=False
    ).mean()
    signal = macd.ewm(span=CONFIG["macd_signal"], adjust=False).mean()
    histogram = macd - signal
    latest = work.iloc[-1]
    latest_macd = float(macd.iloc[-1])
    latest_signal = float(signal.iloc[-1])
    latest_histogram = float(histogram.iloc[-1])
    previous_histogram = float(histogram.iloc[-2])

    if float(latest["Close"]) > float(latest["Open"]):
        candle = "陽線"
        candle_direction = "bullish"
    elif float(latest["Close"]) < float(latest["Open"]):
        candle = "陰線"
        candle_direction = "bearish"
    else:
        candle = "十字線"
        candle_direction = "neutral"

    macd_direction = "bullish" if latest_macd > latest_signal else "bearish"
    macd_label = "強気" if macd_direction == "bullish" else "弱気"
    if candle_direction == macd_direction == "bullish":
        overall = "上昇優勢"
        overall_direction = "bullish"
    elif candle_direction == macd_direction == "bearish":
        overall = "下降優勢"
        overall_direction = "bearish"
    else:
        overall = "方向不一致・様子見"
        overall_direction = "neutral"

    return {
        "ticker": ticker,
        "date": work.index[-1].strftime("%Y-%m-%d"),
        "open": round(float(latest["Open"]), 2),
        "close": round(float(latest["Close"]), 2),
        "candle": candle,
        "candle_direction": candle_direction,
        "macd": round(latest_macd, 4),
        "signal": round(latest_signal, 4),
        "histogram": round(latest_histogram, 4),
        "histogram_trend": "拡大" if abs(latest_histogram) > abs(previous_histogram) else "縮小",
        "macd_label": macd_label,
        "macd_direction": macd_direction,
        "overall": overall,
        "overall_direction": overall_direction,
        "params": {
            "fast": CONFIG["macd_fast"],
            "slow": CONFIG["macd_slow"],
            "signal": CONFIG["macd_signal"],
        },
    }


def _load_technical_signal(code: str) -> dict:
    """Yahoo Financeの直近日足を優先してMACD判断を返す。"""
    ticker = f"{code}.T"
    try:
        import yfinance as yf
        df = yf.download(ticker, period="6mo", progress=False, auto_adjust=True)
        if isinstance(df.columns, pd.MultiIndex):
            df = df.droplevel(1, axis=1)
        if df is not None and len(df) >= CONFIG["macd_slow"] + CONFIG["macd_signal"]:
            return _calc_technical_signal(df, ticker)
    except Exception:
        pass

    cache_path = os.path.join("price_cache", f"{ticker}.pkl")
    if not os.path.exists(cache_path):
        raise ValueError("MACD判定用の日足データを取得できませんでした")
    return _calc_technical_signal(pd.read_pickle(cache_path), ticker)


class PrototypeApiHandler(BaseHTTPRequestHandler):
    """銘柄名検索とYahoo Finance分析を提供するローカルAPI。"""

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        if parsed.path in ("/", "/index.html"):
            html_path = "ギャップアップ戦略_銘柄変更UIプロトタイプ.html"
            try:
                with open(html_path, "rb") as file:
                    body = file.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError as exc:
                self._json({"error": str(exc)}, 500)
            return
        if parsed.path == "/health":
            self._json({"status": "ok"})
            return
        if parsed.path == "/api/technical":
            code = params.get("code", [""])[0].strip()
            if not code.isdigit() or len(code) != 4:
                self._json({"error": "証券コードは4桁の数字で指定してください"}, 400)
                return
            try:
                self._json(_load_technical_signal(code))
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return
        if parsed.path == "/api/search":
            query = params.get("q", [""])[0].strip().lower()
            name_map = load_name_map()
            matches = [
                {"code": ticker.replace(".T", ""), "ticker": ticker, "name": name}
                for ticker, name in name_map.items()
                if query and (query in str(name).lower() or query in ticker.lower())
            ][:20]
            self._json({"results": matches, "count": len(matches)})
            return
        if parsed.path == "/api/analyze":
            code = params.get("code", [""])[0].strip()
            if not code.isdigit() or len(code) != 4:
                self._json({"error": "証券コードは4桁の数字で指定してください"}, 400)
                return
            try:
                payload = _load_yahoo_prototype_data(code)
                payload["name"] = load_name_map().get(f"{code}.T", f"{code} 銘柄")
                self._json(payload)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)
            return
        self._json({"status": "ok", "usage": "/api/search?q=ソフトバンク or /api/analyze?code=9434"})

    def log_message(self, format: str, *args) -> None:
        return


def run_prototype_api() -> None:
    """Yahoo Finance実データ検証Webアプリを起動する。"""
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8765"))
    server = ThreadingHTTPServer((host, port), PrototypeApiHandler)
    print(f"Yahoo Finance 実データWebアプリ: http://{host}:{port}")
    print("ブラウザでURLを開き、銘柄名または証券コードを入力してください。")
    server.serve_forever()


if __name__ == "__main__":
    if "--prototype-api" in sys.argv:
        run_prototype_api()
    else:
        orchestrator()
