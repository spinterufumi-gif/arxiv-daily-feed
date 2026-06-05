# arxiv-daily-feed

GitHub Actionsで1日1回だけarXiv APIを呼び、`public/papers.json` を更新するための最小構成です。

iPhoneアプリはarXiv APIを直接呼ばず、次のRaw URLからJSONを取得します。

```text
https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/arxiv-daily-feed/main/public/papers.json
```

## セットアップ

1. GitHubで `arxiv-daily-feed` という新規リポジトリを作る。
2. この `GitHubFeed` フォルダの中身を、そのリポジトリのルートにコピーする。
3. GitHubにpushする。
4. GitHubの `Actions` タブから `Update arXiv feed` を選ぶ。
5. `Run workflow` を押して手動実行する。
6. `public/papers.json` が更新されたら、RawボタンからURLをコピーする。
7. iPhoneアプリの設定画面にそのURLを貼る。

## 時刻

`.github/workflows/update-arxiv-feed.yml` では、毎日 02:05 UTC、つまり日本時間 11:05 に実行する設定にしています。

```yaml
schedule:
  - cron: '5 2 * * *'
```

GitHub ActionsのcronはUTCです。

## カテゴリを変える

`scripts/fetch_arxiv.py` の `CATEGORIES` を編集してください。

```python
CATEGORIES = [
    "cond-mat.mes-hall",
    "cond-mat.mtrl-sci",
    "cond-mat.str-el",
]
```

## 注意

このスクリプトは1回の実行につきarXiv APIを1回だけ呼びます。大量に取得したい場合でも、arXiv APIの利用条件に従い、短時間に何度もアクセスしないでください。
