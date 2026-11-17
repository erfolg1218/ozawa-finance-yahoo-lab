# Ozawa Finance Yahoo Lab — Claude向け作業メモ

日本株ギャップアップ（オーバーナイト）戦略を Yahoo Finance 日足で検証するWebアプリ。

## 構成
- `gap_up_scanner.py` … バックテスト本体＋API（`--prototype-api`、`/api/search`・`/api/analyze`）
- `ギャップアップ戦略_銘柄変更UIプロトタイプ.html` … 画面（Chart.js）。データ取得は `apiSearch()`/`apiAnalyze()` 経由
- `build_static_site.py` … watchlist.txt の銘柄を事前計算し `_site/` に静的サイトを生成
- `.github/workflows/pages.yml` … main へのpush、および**毎月1日 9:23 JST** に前月分までで再計算し GitHub Pages に公開（Render不要）。結果は `data_status.json` に記録
- `render.yaml` … 任意銘柄をその場で計算したい場合のRender版（任意）

## 閲覧方法（3通り）
1. GitHub Pages：`https://erfolg1218.github.io/ozawa-finance-yahoo-lab/`（watchlist.txt の銘柄のみ）
2. ローカル：`python gap_up_scanner.py --prototype-api` → http://127.0.0.1:8765（全銘柄検索可）
3. Render：render.yaml でデプロイ（全銘柄検索可）

## AI社員（.claude/agents/）
Claude本体が「番頭」として作業を割り振る。
| 社員 | 役割 | 呼ぶ場面 |
|---|---|---|
| backtest-verifier | 検証担当 | 計算ロジック変更・テスト追加・数値が怪しい |
| data-publisher | データ・公開担当 | 銘柄追加、Pages公開失敗、Render設定 |
| quality-reviewer | 品質・安全担当（読み取り専用） | コミット/PR前のレビュー、セキュリティ確認 |
| ui-designer | 画面担当 | 画面・グラフ改修、スマホ対応 |

標準の段取り：変更 → 担当社員で実装 → backtest-verifier で検証 → quality-reviewer でレビュー → push。

## 注意
- この開発用クラウド環境からは Yahoo Finance に接続できない。動作確認は合成データで行う
- 分析終了日 CONFIG end_date は自動（今月1日＝前月末まで）。`END_DATE` 環境変数で固定可。start_date は利用者が決める。勝手に変えない
- 手数料・税・スリッページは未考慮（研究用途）
