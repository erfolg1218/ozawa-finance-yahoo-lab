"""GitHub Pages 用の静的サイトを生成する。

Renderを起動しなくても閲覧できるように、watchlist.txt の銘柄について
Yahoo Financeデータから検証結果を事前計算し、JSONとして書き出す。

使い方:
    python build_static_site.py              # watchlist.txt の銘柄だけ
    python build_static_site.py --out _site  # 出力先を指定（既定: _site）

毎月1日に GitHub Actions が自動実行し、前月分までのデータで作り直す。

出力構成:
    _site/index.html             画面（静的モード）
    _site/data/stocks.json       検索用の銘柄一覧（収録済みフラグ付き）
    _site/data/stocks/XXXX.json  銘柄ごとの検証結果
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import gap_up_scanner as scanner

HTML_SOURCE = "ギャップアップ戦略_銘柄変更UIプロトタイプ.html"
WATCHLIST = "watchlist.txt"
JST = timezone(timedelta(hours=9))
CARRY_OVER_MAX_MONTHS = 2  # 前回分を引き継ぐのは、データが最大何か月遅れまでか


def months_between(older: str, newer: str) -> int:
    """'YYYY-MM' 同士の月数差（newer - older）。"""
    oy, om = map(int, older.split("-"))
    ny, nm = map(int, newer.split("-"))
    return (ny - oy) * 12 + (nm - om)


def read_watchlist(path: str = WATCHLIST) -> list:
    """watchlist.txt から4桁の証券コードを読む（# 以降はコメント）。"""
    codes = []
    with open(path, encoding="utf-8") as file:
        for line in file:
            code = line.split("#", 1)[0].strip()
            if not code:
                continue
            if not (code.isdigit() and len(code) == 4):
                print(f"  ! 無視: 4桁の証券コードではありません -> {code!r}")
                continue
            if code not in codes:
                codes.append(code)
    return codes


def fetch_previous(code: str):
    """前回公開分のJSONを取得する（PREVIOUS_SITE_URL が設定されている場合のみ）。"""
    base = os.environ.get("PREVIOUS_SITE_URL", "").rstrip("/")
    if not base:
        return None
    try:
        with urllib.request.urlopen(f"{base}/data/stocks/{code}.json", timeout=20) as res:
            return json.loads(res.read().decode("utf-8"))
    except Exception:
        return None


def prepare_out_dir(out_dir: str) -> None:
    """出力先を作り直す。誤ってリポジトリ等を消さないよう、生成物と分かる場合だけ削除する。"""
    if os.path.exists(out_dir):
        is_generated = os.path.basename(os.path.normpath(out_dir)) == "_site" or \
            os.path.exists(os.path.join(out_dir, ".nojekyll"))
        if not is_generated:
            raise SystemExit(f"出力先 {out_dir} は生成物ではないため削除しません。_site など空のフォルダを指定してください")
        shutil.rmtree(out_dir)


def build(out_dir: str) -> int:
    codes = read_watchlist()
    name_map = scanner.load_name_map()
    stock_dir = os.path.join(out_dir, "data", "stocks")
    prepare_out_dir(out_dir)
    os.makedirs(stock_dir)

    end_date = scanner.default_end_date()  # 読み込み側と同じ基準日を使う
    expected_month = (datetime.strptime(end_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m")
    print(f"{len(codes)}銘柄を計算します（{scanner.CONFIG['start_date']} 〜 {expected_month} 末）")
    ok, failed, stale, carried = [], [], [], []
    for i, code in enumerate(codes, 1):
        try:
            payload = scanner._load_yahoo_prototype_data(code)
            payload["name"] = name_map.get(f"{code}.T", f"{code} 銘柄")
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            with open(os.path.join(stock_dir, f"{code}.json"), "w", encoding="utf-8") as file:
                file.write(body)
            ok.append(code)
            last = payload.get("metrics", {}).get("end", "")[:7]
            if last < expected_month:
                stale.append({"code": code, "last_month": last})
                print(f"  [{i}/{len(codes)}] {code} {payload['name']} OK（注意: {last} までしかデータがありません）")
            else:
                print(f"  [{i}/{len(codes)}] {code} {payload['name']} OK")
        except Exception as exc:  # 1銘柄の失敗で全体を止めない
            print(f"  [{i}/{len(codes)}] {code} 失敗: {exc}")
            failed.append({"code": code, "error": type(exc).__name__})
            # 一時的な取得失敗で公開中の銘柄が消えないよう、前回分を引き継ぐ（古すぎるものは除く）
            previous = fetch_previous(code)
            prev_last = ""
            if isinstance(previous, dict) and isinstance(previous.get("metrics"), dict):
                prev_last = str(previous["metrics"].get("end", ""))[:7]
            if len(prev_last) == 7 and months_between(prev_last, expected_month) <= CARRY_OVER_MAX_MONTHS:
                try:
                    with open(os.path.join(stock_dir, f"{code}.json"), "w", encoding="utf-8") as file:
                        json.dump(previous, file, ensure_ascii=False, allow_nan=False)
                    ok.append(code)
                    carried.append(code)
                    if prev_last < expected_month:
                        stale.append({"code": code, "last_month": prev_last})
                    print(f"      -> 前回公開分（{prev_last} まで）を引き継ぎました")
                except Exception as write_exc:
                    print(f"      -> 前回公開分を書き込めませんでした: {write_exc}")
            elif previous is not None:
                print(f"      -> 前回公開分が古い（{prev_last or '不明'}）ため引き継ぎません")
        time.sleep(0.5)  # Yahoo Finance側の制限対策

    available = set(ok)
    stocks = [
        {"code": ticker.replace(".T", ""), "name": str(name), "available": ticker.replace(".T", "") in available}
        for ticker, name in name_map.items()
    ]
    index = {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "data_through": expected_month,
        "available": ok,
        "failed": failed,
        "stale": stale,
        "carried_over": carried,
        "stocks": stocks,
    }
    with open(os.path.join(out_dir, "data", "stocks.json"), "w", encoding="utf-8") as file:
        json.dump(index, file, ensure_ascii=False)

    with open(HTML_SOURCE, encoding="utf-8") as file:
        html = file.read()
    marker = "<script>"
    html = html.replace(marker, "<script>window.STATIC_SITE = true;</script>\n" + marker, 1)
    with open(os.path.join(out_dir, "index.html"), "w", encoding="utf-8") as file:
        file.write(html)
    # GitHub Pages の Jekyll 処理を無効化
    open(os.path.join(out_dir, ".nojekyll"), "w").close()

    status = {k: index[k] for k in ("generated_at", "data_through", "available", "failed", "stale", "carried_over")}
    with open(os.path.join(out_dir, "data_status.json"), "w", encoding="utf-8") as file:
        json.dump(status, file, ensure_ascii=False, indent=2)

    print(f"完了: 公開 {len(ok)} / 取得失敗 {len(failed)}（うち前回分引き継ぎ {len(carried)}） / 前月分なし {len(stale)} -> {out_dir}")
    fresh = len(ok) - len(carried)
    if failed or stale:
        # GitHub Actions の実行結果画面に注意として表示
        print(f"::warning::取得失敗 {len(failed)} 銘柄、前月分なし {len(stale)} 銘柄。data_status.json を確認してください")
    if fresh == 0:
        # 前回分の引き継ぎだけでは「更新成功」としない（公開もしない＝前回のサイトがそのまま残る）
        print("::error::1銘柄も新しく取得できませんでした。Yahoo Financeへの接続を確認してください。")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GitHub Pages用の静的サイトを生成")
    parser.add_argument("--out", default="_site")
    args = parser.parse_args()
    sys.exit(build(args.out))
