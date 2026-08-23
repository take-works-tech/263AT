#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
断面正規化（docs/02_definition_spec.md §4 / §4.1）。

生の値をスコアに変える層。**770 件の検証で決めた規約をここに集約する。**

§4 で決めたこと（この実装が守るもの）
------------------------------------
| 規約 | 実装 |
|---|---|
| `z = Phi_inv((rank - 0.5) / N)`。同順位は平均順位 | `rank_normal()` |
| **欠損は中央値補完しない。** `z=0` + **欠損フラグを別特徴量に**（Z01） | `normalize()` が2本返す |
| `N_t < 30` のグループではランク化しない | `MIN_GROUP` |
| ∩型は `-abs(x - x*)` に変換してからランク化 | `cap_transform()` |
| **U カテゴリは該当業種内のみ。非該当は欠損** | `rank_industry` |
| **`rank_sector` は市場内で閉じる**（§4.1、2026-08-23 確定） | `rank_sector` が市場でも分割 |
| N<30 のフォールバックは粗い分類 → 市場全体、フラグを立てる | `normalize()` が `fallback` を返す |

**なぜ中央値補完をしないか**（§4 と検証の全体を通じて繰り返し出た論点）
--------------------------------------------------------------
中央値で埋めると「データが無い小型株」が「平均的な大型株」に化ける。
263AT が狙うのは**まさにデータが揃わない小型株**なので、
補完はユニバースを暗黙に大型株へ寄せる — **戦略と正面から衝突する。**

`z=0` + 欠損フラグなら、モデルが「欠損であること自体」を学習できる。
実際、検証では**欠損が情報を持つ**例が何度も出た
（O カテゴリの「開示していないから見られていない」、
U カテゴリの「良い数字の企業ほど開示する」など）。

自己テスト
    python src/normalize.py
"""
from __future__ import annotations

import dataclasses
import math
import statistics
from typing import Callable, Sequence

MIN_GROUP = 30      # §4: これ未満のグループではランク化しない


def _phi_inv(p: float) -> float:
    """標準正規の逆関数（Acklam の有理近似）。

    scipy を持ち込まないのは、**この層の依存を最小に保つため。**
    絶対誤差は 1e-9 未満で、断面ランクの用途には十分。
    """
    if not 0.0 < p < 1.0:
        raise ValueError("p は (0,1) の開区間")
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    pl, ph = 0.02425, 1 - 0.02425
    if p < pl:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > ph:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def average_ranks(xs: Sequence[float]) -> list[float]:
    """同順位は平均順位（§4）。1 始まり。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def rank_normal(xs: Sequence[float]) -> list[float]:
    """`z = Phi_inv((rank - 0.5) / N)`。

    **ランク化するのでウィンザライズは原則不要**（§4）。
    外れ値が1つあっても順位が1つ動くだけで、z は有界。
    """
    n = len(xs)
    if n == 0:
        return []
    if n == 1:
        return [0.0]
    return [_phi_inv((r - 0.5) / n) for r in average_ranks(xs)]


def cap_transform(xs: Sequence[float], x_star: float, invert: bool = False
                  ) -> list[float]:
    """∩型（cap）を `-abs(x - x*)` に変換する。∪型は invert=True。

    **x* の決め方をパラメータごとにレジストリへ記録する**のが §4 の要求で、
    ここは変換だけを担う。理論値が明確なもの
    （B32 の x*=1、G05 の x*=0、U06 の x*=1.0）は個別指定済み。
    """
    d = [-abs(x - x_star) for x in xs]
    return [-v for v in d] if invert else d


def sector_median(xs: Sequence[float]) -> float:
    """x* の既定（業種内中央値）。**欠損を除いてから取る。**"""
    v = [x for x in xs if x is not None]
    if not v:
        raise ValueError("すべて欠損のグループでは x* を決められない")
    return statistics.median(v)


@dataclasses.dataclass
class Normalized:
    """正規化の結果。**スコアと欠損フラグを必ず対で返す。**"""

    z: list[float]                 # 欠損は 0.0（中立）
    missing: list[bool]            # Z01。**別特徴量として使う**
    fallback: list[str | None]     # どの母集団でランクしたか。None は主分類
    n_groups: int


def normalize(values: Sequence[float | None],
              group: Sequence[str | None],
              coarse: Sequence[str | None] | None = None,
              market: Sequence[str] | None = None,
              transform: Callable[[Sequence[float]], list[float]] | None = None,
              ) -> Normalized:
    """断面正規化の本体。

    Parameters
    ----------
    values : 生の値。**None は欠損**（0 ではない）
    group  : 主分類（JP なら東証33業種、US なら FF49）。
             **None はそのパラメータが非該当**（U カテゴリの他業種）→ 欠損扱い
    coarse : 粗い分類（東証17 / FF12）。N<30 のときのフォールバック先
    market : 市場（"JP" / "US"）。**指定すると市場内で閉じる**（§4.1）
    transform : ランク化の前に掛ける変換（∩型なら cap_transform を部分適用）

    Returns
    -------
    Normalized。**z と missing を必ず対で持つ。**
    """
    n = len(values)
    if not (len(group) == n and (coarse is None or len(coarse) == n)
            and (market is None or len(market) == n)):
        raise ValueError("入力の長さが揃っていない")

    z = [0.0] * n
    missing = [v is None or g is None for v, g in zip(values, group)]
    fallback: list[str | None] = [None] * n

    def key(i, level):
        m = (market[i] + "|") if market else ""
        if level == 0:
            return m + str(group[i])
        if level == 1:
            return m + "coarse:" + str(coarse[i]) if coarse else None
        return m + "ALL"

    # レベル0（主分類）→ レベル1（粗い分類）→ レベル2（市場全体）と落とす。
    #
    # **粗い分類に落とすときは、その粗い分類に属する全銘柄と比べる。**
    # 既に主分類でランク済みの銘柄も母集団に含める。
    # そうしないと「主分類で 30 に届かなかった銘柄どうし」という
    # 意味のない集団で順位を付けることになる。
    # 書き込むのは未割当の銘柄だけ。
    assigned = [False] * n
    n_groups = 0
    for level in (0, 1, 2):
        buckets: dict[str, list[int]] = {}
        for i in range(n):
            if missing[i]:
                continue
            if level == 0 and assigned[i]:
                continue
            k = key(i, level)
            if k is None:
                continue
            buckets.setdefault(k, []).append(i)

        for k, idx in buckets.items():
            todo = [i for i in idx if not assigned[i]]
            if not todo:
                continue
            if len(idx) < MIN_GROUP:
                if level < 2:
                    continue                # 次のレベルへ落とす
                # 市場全体でも 30 に届かない → **ランク化しない（欠損扱い）**
                for i in todo:
                    missing[i] = True
                    assigned[i] = True
                continue

            raw = [values[i] for i in idx]
            if transform is not None:
                raw = transform(raw)
            zz = rank_normal(raw)
            for i, v in zip(idx, zz):
                if assigned[i]:
                    continue
                z[i] = v
                assigned[i] = True
                # **落としたことを記録する。** 黙って市場全体でランクすると
                # 「業種調整されている」と誤解したまま使われる（§4.1）
                fallback[i] = None if level == 0 else ("coarse" if level == 1 else "market")
            n_groups += 1

    return Normalized(z=z, missing=missing, fallback=fallback, n_groups=n_groups)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)
        print("  %-58s %s" % (name, "OK" if cond else "**FAIL**"))

    print("src/normalize.py 自己テスト")
    print("-" * 72)

    check("Phi_inv(0.5) = 0", abs(_phi_inv(0.5)) < 1e-9)
    check("Phi_inv(0.975) ~ 1.96", abs(_phi_inv(0.975) - 1.959964) < 1e-4)
    check("同順位は平均順位", average_ranks([1.0, 2.0, 2.0, 3.0]) == [1.0, 2.5, 2.5, 4.0])

    zs = rank_normal([1.0, 2.0, 3.0, 4.0, 5.0])
    check("ランク→正規スコアが単調増加", all(a < b for a, b in zip(zs, zs[1:])))
    check("対称な入力で平均が 0", abs(sum(zs)) < 1e-9)
    check("**外れ値があっても z は有界**（ウィンザライズ不要）",
          max(rank_normal([1.0, 2.0, 3.0, 1e12])) < 2.0)

    c = cap_transform([0.0, 1.0, 2.0], x_star=1.0)
    check("∩型: x* で最大", c[1] > c[0] and c[1] > c[2])
    u = cap_transform([0.0, 1.0, 2.0], x_star=1.0, invert=True)
    check("∪型: x* で最小", u[1] < u[0] and u[1] < u[2])
    check("x* の既定は中央値", sector_median([1.0, 5.0, 3.0]) == 3.0)

    # 欠損の扱い
    vals = [1.0, None, 3.0] + [float(i) for i in range(40)]
    grp = ["A"] * len(vals)
    r = normalize(vals, grp)
    check("**欠損は z=0（中立）**", r.z[1] == 0.0)
    check("**欠損フラグが別に立つ（Z01）**", r.missing[1] is True and r.missing[0] is False)
    check("欠損以外はランク化される", r.z[0] != 0.0)
    check("**中央値補完をしていない**（欠損の z が中央値の z と違う）",
          r.z[1] == 0.0 and sum(1 for m in r.missing if m) == 1)

    # 非該当業種（U カテゴリ）
    r2 = normalize([1.0, 2.0], [None, "U-1"])
    check("**業種が非該当なら欠損**（U カテゴリ）", r2.missing[0] is True)

    # 最小母集団とフォールバック
    small_v = [float(i) for i in range(10)]
    r3 = normalize(small_v, ["TINY"] * 10)
    check("N<30 で粗い分類も市場全体も無ければ欠損扱い", all(r3.missing))

    v = [float(i) for i in range(40)]
    g = ["TINY"] * 10 + ["BIG"] * 30
    co = ["C"] * 40
    r4 = normalize(v, g, coarse=co)
    check("N<30 の TINY は粗い分類に落ちる",
          all(f == "coarse" for f, gg in zip(r4.fallback, g) if gg == "TINY"))
    check("**落としたことが fallback に記録される**",
          r4.fallback[0] == "coarse" and r4.fallback[-1] is None)
    check("N>=30 の BIG は主分類のまま", r4.fallback[-1] is None)

    # 市場内で閉じる（§4.1）
    v2 = [float(i) for i in range(60)]
    g2 = ["S"] * 60
    mk = ["JP"] * 30 + ["US"] * 30
    r5 = normalize(v2, g2, market=mk)
    check("**rank_sector が市場内で閉じる（§4.1）**", r5.n_groups == 2)
    check("市場内で閉じると各市場の最小値が同じ z になる",
          abs(r5.z[0] - r5.z[30]) < 1e-9)
    r6 = normalize(v2, g2)
    check("市場を指定しなければ1グループ", r6.n_groups == 1)

    print("-" * 72)
    total = 21
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
