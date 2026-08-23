#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
市場・サイズ・バリューの3ファクターを、**自分の断面から作る。**

なぜ自前で作るのか
------------------
Ken French のサイトが日次ファクターを無料で配っているが、
**それを使うと3つの問題が起きる。**

1. **日本に対応するものが無い。** 米国だけ他人の系列を使うと、
   日米で残差の定義が変わる（spec §4.1 で業種分類を揃えた理由と同じ）。
2. **ユニバースが違う。** French のユニバースは NYSE/AMEX/NASDAQ 全銘柄。
   263AT のユニバースはゲートを通った後の集合で、**別物である。**
   他人のユニバースで測った市場に対する残差は、**自分の残差ではない。**
3. **更新が遅れる。** 数ヶ月の遅延がある。

→ **自分のユニバースの断面から作る。** そのぶん、
  Fama-French の手続きとの差を**ここに明記する義務**がある。

Fama-French (1993) との違い — **同じだと言ってはいけない**
--------------------------------------------------------
| | Fama-French | 263AT |
|---|---|---|
| サイズの分割点 | **NYSE の中央値** | **ユニバースの中央値**（取引所の別を持っていない） |
| バリューの分割点 | NYSE の 30/70 パーセンタイル | ユニバースの 30/70 |
| 組み替え | **毎年6月末**（12月末の簿価を使う） | **毎月末**（その時点で入手可能な最新の簿価） |
| 市場リターン | 時価総額加重 − 無リスク金利 | 時価総額加重（**無リスク金利を引いていない**） |
| ユニバース | NYSE/AMEX/NASDAQ 全普通株 | **263AT のゲートを通った集合** |

**無リスク金利を引いていない**のは、残差の計算に使う限り
切片が吸収するため実害が小さいから。
**ただし MKT 自体を水準として読むときは誤る。** OQ に残す。

**毎月組み替えは Fama-French より速い。**
簿価は決算が出た時点で使えるので、**PIT には反していない**が、
**French の系列とは数値が一致しない。** 比較するときに忘れないこと。

PIT
---
`build()` は**その月の断面（mcap / 簿価）と、その月の日次リターン**だけを使う。
**将来のリターンで銘柄を選ばない。** 分割点はその月の断面から決める。

自己テスト
    python src/factors.py
"""
from __future__ import annotations

import dataclasses
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

# サイズ・バリューの分割点。**Fama-French の値をそのまま使う**
SIZE_Q = 0.5
VALUE_LO, VALUE_HI = 0.30, 0.70
MIN_PER_PORTFOLIO = 3       # これを下回る組があれば、その日は作らない


@dataclasses.dataclass(frozen=True)
class Day:
    """1日ぶんのファクター。**構成銘柄数も持つ**（薄い日を見分けるため）。"""

    date: str
    mkt: float
    smb: float
    hml: float
    n: int

    def as_dict(self) -> dict:
        return {"MKT": self.mkt, "SMB": self.smb, "HML": self.hml}


def _pct(xs: list[float], q: float) -> float:
    """パーセンタイル（線形補間なし。**下側から数えて越えた値**）。"""
    s = sorted(xs)
    if not s:
        raise ValueError("空")
    k = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return s[k]


def assign(mcap: dict[str, float], bm: dict[str, float]
           ) -> dict[str, str] | None:
    """2×3 の組に振り分ける。**両方の特性がある銘柄だけ。**

    返すのは `{ticker: "SL"|"SN"|"SH"|"BL"|"BN"|"BH"}`。
    **組が薄すぎれば None**（作らない）。
    """
    both = [t for t in mcap if t in bm
            and mcap[t] is not None and bm[t] is not None
            and mcap[t] > 0]
    if len(both) < MIN_PER_PORTFOLIO * 6:
        return None
    size_cut = _pct([mcap[t] for t in both], SIZE_Q)
    lo = _pct([bm[t] for t in both], VALUE_LO)
    hi = _pct([bm[t] for t in both], VALUE_HI)
    out = {}
    for t in both:
        s = "S" if mcap[t] <= size_cut else "B"
        v = "L" if bm[t] <= lo else ("H" if bm[t] >= hi else "N")
        out[t] = s + v
    counts: dict[str, int] = {}
    for g in out.values():
        counts[g] = counts.get(g, 0) + 1
    if any(counts.get(g, 0) < MIN_PER_PORTFOLIO
           for g in ("SL", "SN", "SH", "BL", "BN", "BH")):
        return None
    return out


def one_day(date: str, rets: dict[str, float], groups: dict[str, str],
            mcap: dict[str, float]) -> Day | None:
    """1日ぶんのファクターを組む。

    **各組の中は時価総額加重**（Fama-French と同じ）。
    等加重にすると SMB が小型株のノイズに支配される。
    """
    have = [t for t in rets if t in groups and rets[t] is not None]
    if len(have) < MIN_PER_PORTFOLIO * 6:
        return None

    def vw(members: list[str]) -> float | None:
        w = sum(mcap[t] for t in members if mcap.get(t))
        if w <= 0:
            return None
        return sum(rets[t] * mcap[t] for t in members if mcap.get(t)) / w

    by: dict[str, list[str]] = {}
    for t in have:
        by.setdefault(groups[t], []).append(t)
    p = {g: vw(by.get(g, [])) for g in ("SL", "SN", "SH", "BL", "BN", "BH")}
    if any(v is None for v in p.values()):
        return None

    small = (p["SL"] + p["SN"] + p["SH"]) / 3.0
    big = (p["BL"] + p["BN"] + p["BH"]) / 3.0
    high = (p["SH"] + p["BH"]) / 2.0
    low = (p["SL"] + p["BL"]) / 2.0
    mkt = vw(have)
    if mkt is None:
        return None
    # **無リスク金利を引いていない。** 残差では切片が吸収する（冒頭の注記）
    return Day(date=date, mkt=mkt, smb=small - big, hml=high - low,
               n=len(have))


def regress(y: list[float], xs: list[list[float]]
            ) -> tuple[list[float], list[float]] | None:
    """最小二乗（切片つき）。**戻りは (係数, 残差)。**

    `xs` は説明変数ごとの列。行列が特異なら None
    — **擬似逆行列で無理に解かない。** 解けないことを伝える方が正しい。
    """
    n = len(y)
    k = len(xs) + 1
    if n <= k:
        return None
    X = [[1.0] + [xs[j][i] for j in range(len(xs))] for i in range(n)]
    # 正規方程式 (X'X) b = X'y をガウス消去で解く
    A = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)]
         + [sum(X[i][a] * y[i] for i in range(n))] for a in range(k)]
    for c in range(k):
        piv = max(range(c, k), key=lambda r: abs(A[r][c]))
        if abs(A[piv][c]) < 1e-12:
            return None                      # **特異。解かない**
        A[c], A[piv] = A[piv], A[c]
        d = A[c][c]
        A[c] = [v / d for v in A[c]]
        for r in range(k):
            if r == c:
                continue
            f = A[r][c]
            if f:
                A[r] = [A[r][j] - f * A[c][j] for j in range(k + 1)]
    b = [A[r][k] for r in range(k)]
    resid = [y[i] - sum(b[j] * X[i][j] for j in range(k)) for i in range(n)]
    return b, resid


def moments(xs: list[float]) -> tuple[float, float] | None:
    """(標準偏差, 歪度)。**n<3 では歪度を作らない。**"""
    n = len(xs)
    if n < 3:
        return None
    mu = sum(xs) / n
    m2 = sum((x - mu) ** 2 for x in xs) / n
    if m2 <= 0:
        return 0.0, 0.0
    m3 = sum((x - mu) ** 3 for x in xs) / n
    sd = math.sqrt(m2 * n / (n - 1))
    return sd, m3 / (m2 ** 1.5)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []
    ran = []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    def near(a, b, tol=1e-8):
        return a is not None and abs(a - b) < tol

    print("src/factors.py 自己テスト")
    print("-" * 80)

    # --- 振り分け -----------------------------------------------------------
    n = 60
    mcap = {"T%02d" % i: float(i + 1) for i in range(n)}
    bm = {"T%02d" % i: float((i * 7) % n) for i in range(n)}
    g = assign(mcap, bm)
    check("**2×3 の6組に振り分ける**", g is not None and len(set(g.values())) == 6)
    check("小型が S、大型が B", g["T00"][0] == "S" and g["T59"][0] == "B")
    check("**薄い断面では作らない**", assign({"a": 1.0}, {"a": 1.0}) is None)
    check("特性が片方しか無い銘柄は入らない",
          assign({**mcap, "X": 1.0}, bm) is not None
          and "X" not in assign({**mcap, "X": 1.0}, bm))

    # --- 1日ぶん ------------------------------------------------------------
    # 全銘柄が同じリターンなら SMB も HML も 0
    flat = {t: 0.01 for t in mcap}
    d = one_day("2020-01-02", flat, g, mcap)
    check("**全銘柄同一リターンなら MKT がその値**", near(d.mkt, 0.01))
    check("**同一なら SMB = 0**", near(d.smb, 0.0))
    check("**同一なら HML = 0**", near(d.hml, 0.0))
    check("構成銘柄数を持つ", d.n == n)

    # 小型だけ上げれば SMB > 0
    r2 = {t: (0.05 if g[t][0] == "S" else 0.0) for t in mcap}
    d2 = one_day("2020-01-03", r2, g, mcap)
    check("**小型だけ上がれば SMB > 0**", d2.smb > 0.04)
    check("そのとき HML はほぼ 0", abs(d2.hml) < 1e-9)

    # 高 B/M だけ上げれば HML > 0
    r3 = {t: (0.05 if g[t][1] == "H" else 0.0) for t in mcap}
    d3 = one_day("2020-01-06", r3, g, mcap)
    check("**高B/Mだけ上がれば HML > 0**", d3.hml > 0.04)

    # **時価総額加重であること。** 大型に寄る
    r4 = {t: (1.0 if t == "T59" else 0.0) for t in mcap}
    d4 = one_day("2020-01-07", r4, g, mcap)
    ew = 1.0 / n
    check("**MKT は等加重でなく時価総額加重**", d4.mkt > ew * 1.5)
    check("重みは時価総額の比",
          near(d4.mkt, mcap["T59"] / sum(mcap.values())))

    # --- 回帰 ---------------------------------------------------------------
    x1 = [float(i) for i in range(50)]
    x2 = [float((i * 3) % 11) for i in range(50)]
    y = [2.0 + 3.0 * a - 1.5 * b for a, b in zip(x1, x2)]
    got = regress(y, [x1, x2])
    check("**係数を復元する**", got is not None
          and near(got[0][0], 2.0, 1e-6) and near(got[0][1], 3.0, 1e-6)
          and near(got[0][2], -1.5, 1e-6))
    check("**当てはまれば残差は 0**", all(abs(r) < 1e-6 for r in got[1]))
    check("残差の本数は観測数と同じ", len(got[1]) == 50)

    # 特異なとき
    check("**共線なら解かない（擬似逆行列で誤魔化さない）**",
          regress(y, [x1, x1]) is None)
    check("**観測が足りなければ None**", regress([1.0, 2.0], [[1.0, 2.0]]) is None)

    # 残差が説明変数と直交すること
    import random
    random.seed(1)
    yn = [2.0 + 3.0 * a - 1.5 * b + random.gauss(0, 0.5)
          for a, b in zip(x1, x2)]
    b2, res = regress(yn, [x1, x2])
    cov = sum(r * a for r, a in zip(res, x1))
    check("**残差は説明変数と直交する**", abs(cov) < 1e-6)
    check("残差の平均は 0", abs(sum(res) / len(res)) < 1e-9)

    # --- モーメント ---------------------------------------------------------
    sd, sk = moments([1.0, 2.0, 3.0, 4.0, 5.0])
    check("標準偏差（不偏）", near(sd, math.sqrt(2.5)))
    check("**対称なら歪度 0**", abs(sk) < 1e-9)
    _, sk2 = moments([1.0, 1.0, 1.0, 1.0, 10.0])
    check("**右に裾があれば歪度 > 0**", sk2 > 1.0)
    check("**n<3 では作らない**", moments([1.0, 2.0]) is None)
    check("定数なら sd も歪度も 0", moments([2.0] * 5) == (0.0, 0.0))

    # --- 分割点 -------------------------------------------------------------
    check("中央値", near(_pct([1.0, 2.0, 3.0], 0.5), 2.0))
    check("下側 30%", _pct([float(i) for i in range(11)], 0.3) == 3.0)

    print("-" * 80)
    # **宣言した本数と、実際に走った本数を突き合わせる。**
    # 直書きの total だけだと、検査が黙って減っても
    # 「28/28 通過」と表示されてしまう（実際にそうなった）。
    # 実測だけにすると、逆に「走らなかった検査」に気づけない。
    # **両方を突き合わせるのが唯一の正しい形。**
    declared = 27
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_test())
