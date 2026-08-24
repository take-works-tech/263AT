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


def select_top(cands: list[Candidate], n_target: int) -> list[Candidate]:
    """**スコア上位 N 本を選んでからサイジングする。**

    §1.8 の N_target。**ユニバース全体に Kelly を配ってはいけない。**

    実データで踏んだ（2026-08-23）: 619銘柄にそのまま配ると
    各 0.16% になり、**最小ポジション 2% を全員が下回って
    保有が2〜3銘柄まで落ちた。**

    §1.8 の議論（Bessembinder: 4%の銘柄が全リターンを作る）は
    「N を小さくしすぎるな」と言うが、**大きくしすぎても
    最小ポジションの制約で結局少数しか持てない。**
    → **N は明示的に選ぶ。** 選ばないと制約が勝手に決めてしまう。
    """
    return sorted([c for c in cands if c.score > 0],
                  key=lambda c: -c.score)[:n_target]


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


def _cap(w: dict[str, float], sector: dict[str, str | None],
         limits: "RiskLimits") -> dict[str, float]:
    """銘柄上限と業種上限を満たすまで**下げる。上げはしない。**

    **1つずつ適用すると順序で結果が変わる**ので、収束するまで繰り返す。
    銘柄上限で切ると業種合計も下がり、業種上限で縮めると
    銘柄上限を再び満たすので、通常は2〜3周で収束する。
    """
    w = dict(w)
    for _ in range(50):
        changed = False
        for k, v in list(w.items()):
            if v > limits.max_per_name + 1e-12:
                w[k] = limits.max_per_name
                changed = True
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
        if not changed:
            break
    return w


def _fill(w: dict[str, float], sector: dict[str, str | None],
          limits: "RiskLimits") -> dict[str, float]:
    """**上限を守りつつ、投資比率を `max_invested` まで埋める。**

    `max_invested` は**上限であって目標でもある。**
    2026-08-25 まで「超えたら縮める」だけだったので、
    段1・段2 が削った分が戻らず、**合計は 90% に一度も届かなかった。**

    やり方は素朴な不動点反復である。

        合計が目標に足りない → 全体を目標/合計 倍する
        → 上限を超えたものを抑える（`_cap`）
        → まだ足りなければ繰り返す

    **上限に達した銘柄は抑えられ、余力のある銘柄が余りを吸う。**
    全員が上限に達したら合計が増えなくなるので、そこで止める。
    **上限の組み合わせで 90% に届かないなら、届かないままでよい。**
    """
    w = _cap(w, sector, limits)
    for _ in range(100):
        tot = sum(w.values())
        if tot <= 0:
            return w
        if tot > limits.max_invested + 1e-12:
            return {k: v * limits.max_invested / tot for k, v in w.items()}
        if tot >= limits.max_invested - 1e-9:
            return w
        nxt = _cap({k: v * limits.max_invested / tot for k, v in w.items()},
                   sector, limits)
        # **増えなくなったら止める。** 全員が上限に張り付いている
        if sum(nxt.values()) - tot < 1e-9:
            return nxt
        w = nxt
    return w


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

    w = _fill(w, sector, limits)

    # 4) **小さすぎるポジションを落とし、空いた分を配り直す。**
    #
    # **2026-08-25 まで、落とした分を配り直していなかった。**
    # 段1（銘柄上限）と段2（業種上限）も減らすだけだったので、
    # 合計は 1.00 から一方的に減り、**投資上限 90% には一度も届かなかった。**
    # 13.5年の実測で **投資比率の中央値は 53%** だった（docs/11）。
    #
    # 「配り直すと上限を再び超えるので現金として残す」と書いていたが、
    # **上限を超えたらまた抑えて、増えなくなるまで繰り返せばよい。**
    # `_fill` がそれをやる。
    #
    # **一度に全部落とさない。** 同一業種20銘柄が各 1.75% で全員が
    # 最小 2% を下回るとき、全部落とすと**空のポートフォリオ**になるが、
    # **17銘柄なら各 2.06% で成立する**（業種上限 35% ÷ 2%）。
    # → **一番小さいものから1つずつ落として、そのつど配り直す。**
    #
    # **まず、構造的に入りきらない本数を一気に落とす。**
    # 投資上限 90% ÷ 最小 2% = **45銘柄が絶対の上限**である。
    # 600銘柄を1つずつ落とすと 555 回まわることになり、
    # **反復上限で打ち切られて「最小未満が残る」不変条件違反が出た**
    # （2026-08-25、実際にこの実装で踏んだ）。
    n_dropped = 0
    max_fit = int(limits.max_invested / limits.min_position)
    if len(w) > max_fit:
        keep = sorted(w, key=lambda k: -w[k])[:max_fit]
        n_dropped += len(w) - len(keep)
        w = {k: w[k] for k in keep}
        w = _fill(w, sector, limits)
    # 残りは業種上限が絡むので、**1つずつ落として そのつど配り直す。**
    for _ in range(len(w) + 5):
        small = [k for k, v in w.items() if v < limits.min_position]
        if not small:
            break
        w.pop(min(small, key=lambda k: w[k]))
        n_dropped += 1
        if not w:
            break
        w = _fill(w, sector, limits)
    if n_dropped:
        notes.append("最小ポジション未満で除外: %d 銘柄（**残りに配り直した**）"
                     % n_dropped)
    if n_dropped and not w:
        notes.append(
            "**上限の組み合わせで全銘柄が除外された。**"
            " 業種上限 %.0f%% ÷ 最小ポジション %.0f%% = 同一業種 %d 銘柄が構造的な上限。"
            " 候補がそれを超えて同じ業種に偏っている"
            % (100 * limits.max_per_sector, 100 * limits.min_position,
               int(limits.max_per_sector / limits.min_position)))

    # 5) 銘柄数の上限。**スコアではなく重みの大きい順に残す**
    if limits.max_names is not None and len(w) > limits.max_names:
        before = sum(w.values())
        keep = sorted(w, key=lambda k: -w[k])[:limits.max_names]
        w = {k: w[k] for k in keep}
        # **切り詰めたら投資比率を戻す。**
        #
        # 戻さないと、上限5銘柄で「各2%×5 = 10%だけ投資、90%は現金」になる。
        # 実測でそれが起きた（2026-08-24）: 上限を 5/10/20/30 と振ったら
        # CAGR が +1.67/+4.91/+7.83/+8.09% と単調に増え、
        # **「集中は不利」という結論が出かけた。**
        # 実際に測っていたのは集中度ではなく**現金比率**だった。
        #
        # 1銘柄あたりの上限（max_per_name）は超えない。
        # 超える場合は投資比率が戻りきらないが、**それは上限の意味そのもの。**
        after = sum(w.values())
        if after > 0 and before > after:
            scale = min(before / after,
                        limits.max_per_name / max(w.values()))
            w = {k: v * scale for k, v in w.items()}
        notes.append("銘柄数の上限 %d に切り詰め、投資比率を %.0f%% に戻した"
                     % (limits.max_names, 100 * sum(w.values())))

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
                     participation: float = 0.10, days: int = 3,
                     n_target: int | None = None
                     ) -> tuple[dict[str, float], list[str]]:
    """入口。**Kelly → 流動性 → 上限 の順で適用する。**

    順序が重要:
    - 流動性を先に当てると、削った分を上限が配り直してしまう
    - 上限を先に当てても、流動性で削った後に上限が緩む

    → **流動性で削ってから上限を当て、上限で削った分は現金にする。**
    """
    notes0 = []
    if n_target is None:
        # **明示されなければ、最小ポジションから逆算した上限を使う。**
        # 投資比率 90% ÷ 最小 2% = 45銘柄が構造的な上限。
        # これを超えて配ると、按分後に最小を下回って落ちるだけ。
        n_target = int(limits.max_invested / limits.min_position)
        notes0.append("n_target を最小ポジションから逆算した: %d 銘柄" % n_target)
    picked = select_top(cands, n_target)
    if len(picked) < len(cands):
        notes0.append("スコア上位 %d / %d 銘柄を選んだ" % (len(picked), len(cands)))
    cands = picked
    w = kelly_weights(cands, fraction=fraction)
    w, n1 = cap_by_liquidity(w, cands, capital_jpy, participation, days)
    w, n2 = apply_limits(w, cands, limits)
    return w, notes0 + n1 + n2


# ---------------------------------------------------------------- self-test
def _c(t, sec, score, vol=0.30, adv=1e9) -> Candidate:
    return Candidate(ticker=t, sector=sec, score=score, volatility=vol, adv_jpy=adv)


def _test() -> int:
    fails = []
    ran = []

    def check(nm, cond):

        ran.append(nm)
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
    # **2026-08-25 まで、ここは空の辞書を返していた。**
    # 20銘柄を按分すると各 1.75% で全員が最小 2% を下回るので、
    # **全部落として何も持たなかった。**
    # 3銘柄だけ落とせば **17銘柄が各 2.06% で成立する**（35% ÷ 2% = 17.5）。
    check("**17銘柄が残る（全滅させない）**", len(w3b) == 17)
    check("**残った分に配り直して業種上限まで使う**",
          abs(sum(w3b.values()) - 0.35) < 1e-6)
    check("**全員が最小ポジション以上**",
          all(v >= L.min_position - 1e-12 for v in w3b.values()))
    check("落としたことを記録する", any("最小ポジション" in x for x in n3b))

    mixed = [_c("A", "X", 3.0), _c("B", "Y", 1.0), _c("C", "Z", 1.0)]
    w4, _ = apply_limits(kelly_weights(mixed), mixed, L)
    check("**1銘柄の上限が効く**", w4["A"] <= L.max_per_name + 1e-9)

    diverse = [_c("T%02d" % i, "S%d" % (i % 10), 1.0) for i in range(30)]
    w5, _ = apply_limits(kelly_weights(diverse), diverse, L)
    check("**投資比率の上限が効く（現金 10% を残す）**",
          abs(sum(w5.values()) - L.max_invested) < 1e-6)

    tiny = [_c("A", "X", 100.0)] + [_c("T%02d" % i, "S%d" % i, 0.01) for i in range(20)]
    w6, n6 = apply_limits(kelly_weights(tiny), tiny, L)
    check("**最小ポジション未満は落とす**",
          all(v >= L.min_position - 1e-12 for v in w6.values()))
    # **2026-08-25 に方針を変えた。** 以前は「落とした分は配り直さない」で、
    # その結果 13.5年の実測で **投資比率の中央値が 53%** だった（docs/11）。
    # 段1・段2 も減らすだけだったので、**投資上限 90% に一度も届かなかった。**
    check("**落とした分を配り直して投資上限まで使う**",
          abs(sum(w6.values()) - L.max_invested) < 1e-6)
    check("**スコアが桁違いに小さくても、上限に達した分は次点が吸う**",
          w6["A"] <= L.max_per_name + 1e-9 and len(w6) > 1)

    lim_n = RiskLimits(max_names=5)
    w7, n7 = apply_limits(kelly_weights(diverse), diverse, lim_n)
    check("銘柄数の上限が効く", len(w7) == 5)
    check("上限を記録する", any("銘柄数の上限" in x for x in n7))
    # **切り詰めた後に投資比率が戻ること。**
    # 戻さないと「上限5銘柄 = 10%だけ投資、90%現金」になり、
    # 集中度ではなく現金比率を測ることになる（2026-08-24 に実際に踏んだ）。
    w7b, _ = apply_limits(kelly_weights(diverse), diverse,
                          RiskLimits(max_names=None))
    check("**切り詰めても投資比率が落ちない**",
          sum(w7.values()) >= sum(w7b.values()) - 1e-9
          or max(w7.values()) >= lim_n.max_per_name - 1e-9)
    check("**1銘柄あたりの上限は超えない**",
          all(v <= lim_n.max_per_name + 1e-9 for v in w7.values()))

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

    # **上位 N 本の選択。**
    #
    # 2026-08-25 の修正前は、これが無いと**全滅**していた（600銘柄が
    # 各 0.15% になり、全員が最小 2% を下回って空になる）。
    # 修正後は **投資上限 90% ÷ 最小 2% = 45銘柄**に自動で収まるので
    # 全滅はしない。だが**スコアの低い銘柄まで入る**ので、
    # **select_top で先に絞る意味は残る。**
    huge = [_c("T%03d" % i, "S%d" % (i % 30), 1.0 + i * 0.001) for i in range(600)]
    w_no = apply_limits(kelly_weights(huge), huge, L)
    check("**600銘柄でも全滅せず、構造上限の45銘柄に収まる**",
          0 < len(w_no[0]) <= int(L.max_invested / L.min_position))
    check("**そのときも投資上限を使い切る**",
          abs(sum(w_no[0].values()) - L.max_invested) < 1e-6)
    w_top, n_top = target_positions(huge, 3_000_000, L)
    check("**上位 N を選べば実際に保有できる本数になる**", 20 <= len(w_top) <= 45)
    check("選んだことを記録する", any("上位" in x for x in n_top))
    check("**n_target は最小ポジションから逆算される（90% / 2% = 45）**",
          any("45 銘柄" in x for x in n_top))
    check("スコアの高い順に選ばれる",
          "T599" in select_top(huge, 5)[0].ticker or
          select_top(huge, 5)[0].score >= select_top(huge, 5)[-1].score)
    check("明示した n_target が優先される",
          len(target_positions(huge, 3_000_000, L, n_target=10)[0]) <= 10)
    check("スコアが正のものだけ選ぶ",
          all(c.score > 0 for c in select_top(huge + [_c("X", "S", -1.0)], 999)))

    print("-" * 78)
    declared = 38
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
