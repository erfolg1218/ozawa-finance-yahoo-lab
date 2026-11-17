"""GitHub Pages 用の静的サイトを生成する。

Renderを起動しなくても閲覧できるように、watchlist.txt の銘柄について
Yahoo Financeデータから検証結果を事前計算し、JSONとして書き出す。

使い方:
    python build_static_site.py              # watchlist.txt の銘柄だけ
    python build_static_site.py --out _site  # 出力先を指定（既定: _site）

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
from datetime import datetime, timedelta, timezone

import gap_up_scanner as scanner

HTML_SOURCE = "ギャップアップ戦略_銘柄変更UIプロトタイプ.html"
WATCHLIST = "watchlist.txt"
JST = timezone(timedelta(hours=9))


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


def build(out_dir: str) -> int:
    codes = read_watchlist()
    name_map = scanner.load_name_map()
    stock_dir = os.path.join(out_dir, "data", "stocks")
    if os.path.exists(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(stock_dir)

    print(f"{len(codes)}銘柄を計算します")
    ok, failed = [], []
    for i, code in enumerate(codes, 1):
        try:
            payload = scanner._load_yahoo_prototype_data(code)
            payload["name"] = name_map.get(f"{code}.T", f"{code} 銘柄")
            body = json.dumps(payload, ensure_ascii=False, allow_nan=False)
            with open(os.path.join(stock_dir, f"{code}.json"), "w", encoding="utf-8") as file:
                file.write(body)
            ok.append(code)
            print(f"  [{i}/{len(codes)}] {code} {payload['name']} OK")
        except Exception as exc:  # 1銘柄の失敗で全体を止めない
            failed.append({"code": code, "error": str(exc)})
            print(f"  [{i}/{len(codes)}] {code} 失敗: {exc}")
        time.sleep(0.5)  # Yahoo Finance側の制限対策

    available = set(ok)
    stocks = [
        {"code": ticker.replace(".T", ""), "name": str(name), "available": ticker.replace(".T", "") in available}
        for ticker, name in name_map.items()
    ]
    index = {
        "generated_at": datetime.now(JST).strftime("%Y-%m-%d %H:%M JST"),
        "available": ok,
        "failed": failed,
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

    print(f"完了: 成功 {len(ok)} / 失敗 {len(failed)} -> {out_dir}")
    if not ok:
        print("1銘柄も取得できませんでした。Yahoo Financeへの接続を確認してください。")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="GitHub Pages用の静的サイトを生成")
    parser.add_argument("--out", default="_site")
    args = parser.parse_args()
    sys.exit(build(args.out))
