#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
サイジングとリスク上限（docs/05_backtest_protocol.md §4.4、catalog §1.8）。

263AT の設計が要求すること
--------------------------
**「9つが負けても1つで賄う」**（Bessembinder: 4%の銘柄が全リターンを作る）。
これは **1銘柄あたりを小さくしすぎてはいけない**ことを意味する。
100銘柄に等分すると、勝つ1銘柄が10倍になっても全体は +9% にしかならない。

一方で **1銘柄に集中すると、その1つが外れたときに全部失う。**

→ **§1.8 の N_target は、この2つの綱引きで決まる。**
  実測 IC から N を決めるのであって、rho から決めるのではない（§1.8.1-1.8.3）。

この層が守る規約
----------------
1. **上限は「率」で持ち、金額で持たない。** 資金が変われば金額は変わる
2. **上限に当たったら削る。上限を理由に買わないのではない**
   — 削った結果ゼロになるなら買わない、という順序
3. **現金比率の下限を守る**（P16 のドライパウダー）
4. **すべての上限を同時に満たす解を返す。** 1つずつ適用すると順序で結果が変わる

自己テスト
    python src/sizing.py
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class RiskLimits:
    """リスク上限。**すべて個別株枠に対する率。**

    金額で持たないのは、資金が変われば金額が変わるから。
    """

    max_per_name: float = 0.15          # 1銘柄あたりの上限
    max_per_sector: float = 0.35        # 同一業種あたりの上限
    max_invested: float = 0.90          # 投資比率の上限（＝現金比率の下限 10%）
    min_position: float = 0.02          # これ未満のポジションは作らない
    max_names: int | None = None        # 銘柄数の上限（None なら無制限）

    def __post_init__(self):
        if not 0 < self.max_per_name <= 1:
            raise ValueError("max_per_name は 0〜1")
        if self.max_per_sector < self.max_per_name:
            raise ValueError("業種上限が銘柄上限より小さいのは矛盾している")
        if not 0 < self.max_invested <= 1:
            raise ValueError("max_invested は 0〜1")
        if self.min_position >= self.max_per_name:
            raise ValueError("最小ポジションが銘柄上限以上なのは矛盾している")


@dataclasses.dataclass(frozen=True)
class Candidate:
    """サイジングの入力。**スコアとリスクを分けて持つ。**"""

    ticker: str
    sector: str | None
    score: float                # 買いスコア（大きいほど良い）
    volatility: float | None    # 予測ボラ（I03）。**Kelly の分母**
    adv_jpy: float | None = None   # 平均売買代金。**建てられる上限を決める**


def kelly_weights(cands: list[Candidate], fraction: float = 0.25,
                  min_vol: float = 0.10) -> dict[str, float]:
    """スコアをボラで割った比例配分（**分数 Kelly**）。

    §1.8 の通り、**E[log W] の最大化はフル Kelly だが、実務では分数を使う。**
    推定誤差があるとフル Kelly は破産確率が跳ね上がる。

    **ボラが取れない銘柄は中央値で代用しない。** 除外する
    — ボラが分からない銘柄のサイズを決められるはずがない。

    `min_vol` は下限。**低ボラ銘柄に無限大の重みが乗るのを防ぐ**
    （実測ボラが極端に低いのは、たいてい売買が薄いだけ）。
    """
    usable = [c for c in cands if c.volatility is not None and c.score > 0]
    if not usable:
        return {}
    raw = {c.ticker: fraction * c.score / max(c.volatility, min_vol) for c in usable}
    tot = sum(raw.values())
    if tot <= 0:
        return {}
    return {k: v / tot for k, v in raw.items()}


def apply_limits(weights: dict[str, float], cands: list[Candidate],
                 limits: RiskLimits) -> tuple[dict[str, float], list[str]]:
    """すべての上限を**同時に**満たすよう調整する。

    **1つずつ適用すると順序で結果が変わる**ので、収束するまで繰り返す。

    Returns
    -------
    (調整後の重み, 効いた上限の説明)
    """
    sector = {c.ticker: c.sector for c in cands}
    w = dict(weights)
    notes = []

    for _ in range(50):                    # 収束しなければ打ち切る
        changed = False

        # 1) 銘柄上限
        for k, v in list(w.items()):
            if v > limits.max_per_name:
                w[k] = limits.max_per_name
                changed = True
        # 2) 業種上限
        by_sec: dict[str | None, float] = {}
        for k, v in w.items():
            by_sec[sector.get(k)] = by_sec.get(sector.get(k), 0.0) + v
        for sec, tot in by_sec.items():
            if tot > limits.max_per_sector + 1e-12:
                scale = limits.max_per_sector / tot
                for k in w:
                    if sector.get(k) == sec:
                        w[k] *= scale
                changed = True
        # 3) 投資比率の上限（現金比率の下限）
        tot = sum(w.values())
        if tot > limits.max_invested + 1e-12:
            for k in w:
                w[k] *= limits.max_invested / tot
            changed = True

        if not changed:
            break
    else:
        notes.append("**上限の適用が収束しなかった。** 上限の組み合わせが矛盾している可能性")

    # 4) 小さすぎるポジションを落とす。**落とした分は配り直さない**
    #    配り直すと上限を再び超えるので、現金として残す
    #
    # **ここで「上限の組み合わせが何も持てない解を生む」ことがある。**
    # 業種上限 35% ÷ 最小ポジション 2% = **同一業種に17銘柄が構造的な上限。**
    # それを超える候補が同じ業種に並ぶと、按分後に全員が最小を下回って全滅する。
    # 自己テストで実際に踏んだ（同一業種20銘柄 → 各 1.75% → 全部消える）。
    #
    # **黙って空のポートフォリオを返してはいけない。** 必ず警告する。
    n_before = len(w)
    dropped = [k for k, v in w.items() if v < limits.min_position]
    for k in dropped:
        del w[k]
    if dropped:
        notes.append("最小ポジション未満で除外: %d 銘柄" % len(dropped))
    if n_before and not w:
        notes.append(
            "**上限の組み合わせで全銘柄が除外された。**"
            " 業種上限 %.0f%% ÷ 最小ポジション %.0f%% = 同一業種 %d 銘柄が構造的な上限。"
            " 候補がそれを超えて同じ業種に偏っている"
            % (100 * limits.max_per_sector, 100 * limits.min_position,
               int(limits.max_per_sector / limits.min_position)))

    # 5) 銘柄数の上限。**スコアではなく重みの大きい順に残す**
    if limits.max_names is not None and len(w) > limits.max_names:
        keep = sorted(w, key=lambda k: -w[k])[:limits.max_names]
        w = {k: w[k] for k in keep}
        notes.append("銘柄数の上限 %d に切り詰めた" % limits.max_names)

    if sum(w.values()) > 0:
        by_sec2: dict[str | None, float] = {}
        for k, v in w.items():
            by_sec2[sector.get(k)] = by_sec2.get(sector.get(k), 0.0) + v
        hit = [s for s, t in by_sec2.items() if t >= limits.max_per_sector - 1e-9]
        if hit:
            notes.append("業種上限に到達: %s" % ", ".join(str(x) for x in hit))
    return w, notes


def cap_by_liquidity(weights: dict[str, float], cands: list[Candidate],
                     capital_jpy: float, participation: float = 0.10,
                     days: int = 3) -> tuple[dict[str, float], list[str]]:
    """**建てられる量を流動性で制限する。**

    J01 の根拠（60万円の建玉を参加率10%で3日以内に処分できる）をそのまま使う。
    建玉が `adv * participation * days` を超えるなら、そこまで削る。

    **これはバックテストで最も忘れられやすい制約。**
    忘れると「小型株を大量に買えたことにして」成績が良く出る。
    """
    adv = {c.ticker: c.adv_jpy for c in cands}
    w = dict(weights)
    notes = []
    for k, v in list(w.items()):
        a = adv.get(k)
        if a is None:
            continue
        cap_jpy = a * participation * days
        want_jpy = v * capital_jpy
        if want_jpy > cap_jpy:
            w[k] = cap_jpy / capital_jpy
            notes.append("%s: 流動性で %.1f%% → %.1f%%"
                         % (k, 100 * v, 100 * w[k]))
    return w, notes


def target_positions(cands: list[Candidate], capital_jpy: float,
                     limits: RiskLimits, fraction: float = 0.25,
                     participation: float = 0.10, days: int = 3
                     ) -> tuple[dict[str, float], list[str]]:
    """入口。**Kelly → 流動性 → 上限 の順で適用する。**

    順序が重要:
    - 流動性を先に当てると、削った分を上限が配り直してしまう
    - 上限を先に当てても、流動性で削った後に上限が緩む

    → **流動性で削ってから上限を当て、上限で削った分は現金にする。**
    """
    w = kelly_weights(cands, fraction=fraction)
    w, n1 = cap_by_liquidity(w, cands, capital_jpy, participation, days)
    w, n2 = apply_limits(w, cands, limits)
    return w, n1 + n2


# ---------------------------------------------------------------- self-test
def _c(t, sec, score, vol=0.30, adv=1e9) -> Candidate:
    return Candidate(ticker=t, sector=sec, score=score, volatility=vol, adv_jpy=adv)


def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-64s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/sizing.py 自己テスト")
    print("-" * 78)
    L = RiskLimits()

    # Kelly
    cs = [_c("A", "X", 2.0), _c("B", "X", 1.0)]
    w = kelly_weights(cs)
    check("スコアに比例して配分する", w["A"] > w["B"])
    check("合計が1になる", abs(sum(w.values()) - 1.0) < 1e-9)
    cs2 = [_c("A", "X", 1.0, vol=0.20), _c("B", "X", 1.0, vol=0.40)]
    w2 = kelly_weights(cs2)
    check("**ボラが低いほど大きく持つ**", w2["A"] > w2["B"])
    check("**ボラが取れない銘柄は除外する（中央値で代用しない）**",
          "C" not in kelly_weights(cs + [Candidate("C", "X", 5.0, None)]))
    check("スコアが負なら持たない", "D" not in kelly_weights(cs + [_c("D", "X", -1.0)]))
    check("**低ボラに無限大の重みが乗らない（下限がある）**",
          kelly_weights([_c("A", "X", 1.0, vol=1e-6), _c("B", "X", 1.0, vol=0.30)])["A"]
          < 0.95)

    # 上限
    # 同一業種10銘柄 → 各 3.5%。最小 2% を上回るので残る
    many = [_c("T%02d" % i, "X", 1.0) for i in range(10)]
    w3, n3 = apply_limits(kelly_weights(many), many, L)
    check("**業種上限が効く（全部同じ業種なので 35%）**",
          abs(sum(w3.values()) - 0.35) < 1e-6)
    check("上限に当たったことを記録する", any("業種上限" in x for x in n3))

    # **上限の組み合わせが「何も持てない」解を生む場合。**
    # 業種上限 35% ÷ 最小 2% = 同一業種17銘柄が構造的な上限
    too_many = [_c("T%02d" % i, "X", 1.0) for i in range(20)]
    w3b, n3b = apply_limits(kelly_weights(too_many), too_many, L)
    check("**同一業種20銘柄では按分後に全員が最小を下回る**", w3b == {})
    check("**空になったことを黙らない（警告する）**",
          any("全銘柄が除外" in x for x in n3b))

    mixed = [_c("A", "X", 3.0), _c("B", "Y", 1.0), _c("C", "Z", 1.0)]
    w4, _ = apply_limits(kelly_weights(mixed), mixed, L)
    check("**1銘柄の上限が効く**", w4["A"] <= L.max_per_name + 1e-9)

    diverse = [_c("T%02d" % i, "S%d" % (i % 10), 1.0) for i in range(30)]
    w5, _ = apply_limits(kelly_weights(diverse), diverse, L)
    check("**投資比率の上限が効く（現金 10% を残す）**",
          abs(sum(w5.values()) - L.max_invested) < 1e-6)

    tiny = [_c("A", "X", 100.0)] + [_c("T%02d" % i, "S%d" % i, 0.01) for i in range(20)]
    w6, n6 = apply_limits(kelly_weights(tiny), tiny, L)
    check("**最小ポジション未満は落とす**", all(v >= L.min_position for v in w6.values()))
    check("落としたことを記録する", any("最小ポジション" in x for x in n6))
    check("**落とした分は配り直さない（現金にする）**", sum(w6.values()) < L.max_invested)

    lim_n = RiskLimits(max_names=5)
    w7, n7 = apply_limits(kelly_weights(diverse), diverse, lim_n)
    check("銘柄数の上限が効く", len(w7) == 5)
    check("上限を記録する", any("銘柄数の上限" in x for x in n7))

    # 流動性
    thin = [_c("A", "X", 1.0, adv=1e6)]      # 平均売買代金 100万円
    w8, n8 = cap_by_liquidity({"A": 1.0}, thin, capital_jpy=3_000_000)
    check("**流動性で建玉を制限する（100万×10%×3日=30万 → 10%）**",
          abs(w8["A"] - 0.10) < 1e-9)
    check("削ったことを記録する", any("流動性" in x for x in n8))
    fat = [_c("A", "X", 1.0, adv=1e10)]
    w9, _ = cap_by_liquidity({"A": 1.0}, fat, capital_jpy=3_000_000)
    check("十分な流動性があれば削らない", abs(w9["A"] - 1.0) < 1e-9)
    check("**売買代金が取れなければ削らない（None を理由に制限しない）**",
          cap_by_liquidity({"A": 1.0}, [Candidate("A", "X", 1.0, 0.3, None)],
                           3_000_000)[0]["A"] == 1.0)

    # 入口
    w10, n10 = target_positions(diverse, 3_000_000, L)
    check("**入口が全上限を同時に満たす**",
          sum(w10.values()) <= L.max_invested + 1e-9
          and all(v <= L.max_per_name + 1e-9 for v in w10.values()))
    by = {}
    for k, v in w10.items():
        s = [c.sector for c in diverse if c.ticker == k][0]
        by[s] = by.get(s, 0) + v
    check("業種上限も同時に満たす", all(v <= L.max_per_sector + 1e-9 for v in by.values()))

    # 矛盾した上限
    try:
        RiskLimits(max_per_name=0.5, max_per_sector=0.3)
        check("**業種上限 < 銘柄上限 を拒否する**", False)
    except ValueError:
        check("**業種上限 < 銘柄上限 を拒否する**", True)
    try:
        RiskLimits(min_position=0.5, max_per_name=0.2)
        check("最小 > 最大 を拒否する", False)
    except ValueError:
        check("最小 > 最大 を拒否する", True)

    check("候補が空なら空を返す", target_positions([], 3_000_000, L)[0] == {})

    print("-" * 78)
    total = 25
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
