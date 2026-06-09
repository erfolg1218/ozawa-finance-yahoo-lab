# Render公開手順

このWebアプリは、銘柄名検索とYahoo Finance日足データによる検証レポートを提供します。

## 公開に含めるファイル

- `gap_up_scanner.py`
- `ギャップアップ戦略_銘柄変更UIプロトタイプ.html`
- `stock_list.csv`
- `requirements.txt`
- `render.yaml`

`price_cache/` は約490MBあるため公開リポジトリには含めません。公開先ではYahoo Financeからオンデマンド取得します。

## 公開手順

1. このフォルダをGitHubリポジトリへ登録します。
2. Renderへログインし、`New` → `Blueprint` を選択します。
3. GitHubリポジトリを接続します。
4. `render.yaml` の内容が読み込まれたらデプロイを実行します。
5. 発行された `https://...onrender.com` URLを共有します。

## 注意点

- Render無料Webサービスは、アクセスが15分間ないと停止します。次回アクセス時の起動に約1分かかる場合があります。
- 無料サービスのファイルシステムは一時的です。取得したYahoo Financeキャッシュは再起動時に消えます。
- Yahoo Financeは非公式データソースです。最終的な投資判断には公式データでの再検証が必要です。
- 多人数が大量検索するとYahoo Finance側の制限を受ける可能性があります。

## ローカル起動

```powershell
pip install -r requirements.txt
python gap_up_scanner.py --prototype-api
```

ブラウザで `http://127.0.0.1:8765` を開きます。
