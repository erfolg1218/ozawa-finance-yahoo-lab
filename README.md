# Ozawa Finance Yahoo Lab

Yahoo Financeの日足データを使い、日本株のオーバーナイト値動きを検証するWebアプリです。

## 主な機能

- 銘柄名または証券コードによる検索
- Yahoo Financeデータのオンデマンド取得
- 銘柄別KPI・資産曲線・年次損益・月次ヒートマップ
- 複数銘柄の選択と合算比較

## ローカル起動

```powershell
pip install -r requirements.txt
python gap_up_scanner.py --prototype-api
```

ブラウザで `http://127.0.0.1:8765` を開きます。

## Renderへの公開

このリポジトリには `render.yaml` が含まれています。RenderでBlueprintとして読み込むとWebサービスを作成できます。

## データと注意事項

- データ出典: Yahoo Finance (`yfinance`)
- 初期表示の3銘柄もYahoo Financeデータから計算しています。
- Yahoo Financeは非公式データソースです。
- 手数料、税金、スリッページ、実際の約定可能性は考慮していません。
- 表示結果は検証・研究用途であり、投資判断は利用者自身で行ってください。
