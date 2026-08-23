# params/ — パラメータレジストリ

769 パラメータの**機械可読な定義**。売買コードはこのレジストリから生成する。

md とコードで定義を二重管理すると必ずずれるので、
[docs/01_parameter_catalog.md](../docs/01_parameter_catalog.md) を人間向けの正典、
このディレクトリをその機械可読な投影として扱う。

---

## 3層構造

```
  docs/01_parameter_catalog.md      … 1. カタログ表（人間が読む正典）
            │                            definition / sign / 買売 / horizon /
            │                            data_sources / evidence_stars を導出
            ▼
  params/_defaults.yaml             … 2. プロジェクト規約（決定事項）
            │                            連結 / 欠損方針 / 正規化母集団 /
            │                            PITラグ / 会計期間の既定
            ▼
  params/_overrides_oap.yaml        … 3. OSAP 突合で機械充填した q11
            │                            tools/crosswalk_oap.py が生成
            ▼
  params/_overrides.yaml            … 4. 人によるレビュー結果【最優先】
            │                            12問に答えた内容。再生成しても失われない
            ▼
  params/A.yaml … Z.yaml            … 生成物
  params/_meta.yaml                      各項目の出所は provenance に記録
```

> **A.yaml 〜 Z.yaml と _meta.yaml は生成物である。直接編集しない。**
> 直すべき対象は、カタログ md か `_defaults.yaml` か `_overrides.yaml` のいずれか。
> `_overrides_oap.yaml` も生成物なので、直すなら `_oap_crosswalk.yaml`（対応表）の方を直す。

---

## コマンド

```bash
python tools/build_registry.py            # 生成 / 更新
python tools/build_registry.py --check    # 書き込まず、md と yaml の乖離だけ検査（CI用）
python tools/validate_registry.py         # スキーマ検証 + 進捗レポート
python tools/validate_registry.py --report  # カテゴリ別・実証度別の内訳も出す
```

`--check` が落ちたら、カタログを編集したのに再生成していない（レジストリが陳腐化している）。

---

## `review.status` の意味

| status | 意味 |
|---|---|
| `draft` | カタログ表と規約から自動生成されただけ。**実装してはいけない** |
| `reviewed` | 12問の一部に答えた途中状態 |
| `verified` | **12問すべてに答えた。** `pending` が空で、必須項目が全部埋まっている |

`pending` は手で書かない。`provenance` から自動計算される。
`verified` なのに `pending` が残っていたら validator がエラーで落とす。

---

## レビューのやり方

1. `params/<CAT>.yaml` で対象パラメータの `review.pending` を見る
2. [docs/02_definition_spec.md §8](../docs/02_definition_spec.md) の12問に答える
3. 答えを `params/_overrides.yaml` に追記する（部分上書きでよい）
4. すべて埋まったら `review: {status: verified}` を付ける
5. `python tools/build_registry.py && python tools/validate_registry.py` を通す

### 12問と対応フィールド

| 問 | フィールド | 規約で決まるか |
|---|---|---|
| q01 | `period_convention` | A〜F・U・V 以外は規約で確定 |
| q02 | `pit_lag_days` | **規約で確定**（データ源から導出） |
| q03 | `consolidation` | **規約で確定** |
| q04 | `accounting_standard_note` | A〜F・U・V 以外は `na` |
| q05 | `zero_denominator_policy` | 除算を含まないものは `na` |
| q06 | `missing_bias` | **常に個別判断**（欠損の偏りは Z03 に直結する） |
| q07 | `normalization` | **規約で確定**（カテゴリ別） |
| q08 | `nonlinear` | ∩/∪ のみ個別判断（x* の決め方） |
| q09 | `correlated_with` | **常に個別判断** |
| q10 | `economic_rationale` | **常に個別判断。書けないものは採用しない** |
| q11 | `evidence_tier` + `references` | **常に個別判断。一次文献にリンクする** |
| q12 | `markets` | K カテゴリのみ導出済み |

加えて、`buy_class` / `sell_class` が `gate` のものは **`gate_policy`（実行可能な閾値）が必須**。
閾値のないゲートは実装できない。

---

## 現在の進捗

| | |
|---|---|
| パラメータ | 769 |
| verified | **46** = ★★★ 20件 + **ゲート 26件（全件）** |
| draft | 723 |
| ゲート | **26/26 完全検証済み**（閾値は Phase 1 で較正: OQ-23） |

残っている問い（`validate_registry.py` が毎回出力する）:

| 問 | 残り | 備考 |
|---|---|---|
| q06 欠損の偏り | 723 | 全件で個別判断が要る |
| q09 相関 | 723 | データを見ないと答えられない。Phase 1 の相関行列（Z12）待ち |
| q10 経済的理由 | 723 | **人が書くしかない。ここが最も重要** |
| q11 実証度と一次文献 | **560** | OSAP 突合で 188件、ゲート検証で 26件を充填済み |
| q12 日米差 | 667 | |
| q05 分母ゼロ | 266 | 除算を含むもの |
| q01 会計期間 | 256 | A〜F・U・V |
| q04 会計基準差 | 256 | 同上 |
| q08 ∩の最適点 | 55 | |

### 推奨するレビュー順

1. **ゲート 26件の完全検証** — **完了**（誤ると致命的な損失に直結するため最優先だった）
2. ★★★ 20件 — **完了**
3. `_defaults.yaml` の初期重み上位カテゴリ（B / A / C / O）の ★★ ← **次はここ**
4. 残り

### 根拠が「構造的判断のみ」の10件

validator が警告として出す。**欠陥ではなく、判断の所在を可視化するための印。**

`D02 D04 D07 D22 E08 E12 J02 J13 K45 T13`

これらは実証論文ではなく論理的必然（債務超過の企業を買わない、監査意見が付かない企業を買わない、
出口が確保できないサイズを建てない）に基づく。**閾値の妥当性は Phase 1 で較正する（OQ-23）。**
「ゲートを通した群 vs 通さない群」の成績差を測れば、どのゲートが実際にエッジを守り、
どれが単にユニバースを痩せさせているだけかが分かる。

---

## レビュー中に見つかった設計上の問題

レジストリを埋める作業自体が検証になっている。現時点で出てきたもの:

- **N02 / N10 の欠損は日本の小型株に集中している。**
  アナリストコンセンサスを前提にした PEAD・リビジョン系は、無カバー62%という
  本プロジェクトの中核ユニバースで丸ごと計算できない。
  会社予想版（GUIDE_CO）と四季報版（K32/K33）の整備が実質的に必須（catalog OQ-22）。
- **B10 の分母（営業利益）が負のときに欠損にしないと、赤字企業が「現金化率が良い」と誤判定される。**
  この手の符号の罠は、12問の q05 を通さないと見つからない。
- **F02（5年株式発行率）は上場5年未満の企業が丸ごと欠損する。**
  結果としてユニバースが暗黙に成熟企業へ偏る。Z03 での監視が必要。
- **ゲートの閾値はすべて初期提案であり、実証的根拠はまだない**（catalog OQ-23）。
