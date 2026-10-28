# 食べ歩きプランの最適化

使える時間・食事の回数・満腹度・疲労度・予算を設定すると、1日で食べ歩くプランの上位案を出します（サンプルは大阪キタ・ミナミの30店）。決まりごとは AGENTS.md、モデルの中身は docs/model.md を参照してください。

## 起動

[uv](https://docs.astral.sh/uv/) が必要です。このフォルダで次を実行します（初回は依存が自動で入ります）。

```
uv run uvicorn app.main:app --reload
```

- 画面: http://127.0.0.1:8000/
- API の説明: http://127.0.0.1:8000/docs

停止は起動したターミナルで Ctrl+C を押します。

## データ

- サンプル: `data/sample_osaka.xlsx`（店・駅・移動手段の3シート）
- 自分のデータは、同じ形の Excel を画面から読み込みます。見本は画面右上の「Excel の見本」からダウンロードできます。
- サンプルを作り直す: `uv run python tools/make_sample.py`

## テスト

```
uv run pytest
```
