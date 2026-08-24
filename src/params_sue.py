#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**決算サプライズ（SUE）**と、その業種波及。N02 / L02。

なぜ優先するか
--------------
    N02 SUE          **再現 t = 9.32**  ← 実装済み35本の中で最強クラス
    L02 同業大型株のSUE **再現 t = 3.21**  ← N02 を入力に取る

**どちらも既存の採用規則（他者の再現 t >= 3.0）を満たす。**
事前登録は要らない — 規則に当てはめるだけである。

「予想 EPS」をどう作るか — **有料データを使わない**
--------------------------------------------------
カタログの定義は
    `(EPS_actual − EPS_expected) / sd(surprise, 直近8四半期)`

`EPS_expected` は普通アナリスト予想（有料）だが、
**季節ランダムウォーク**（前年同期の EPS）で代用できる。
これは PEAD の原典（Foster-Olsen-Shevlin 1984）が使った定義そのもので、
**代用ではなく、むしろ元の形**である。

    サプライズ = EPS(四半期 q) − EPS(四半期 q−4)
    SUE        = サプライズ / sd(過去8四半期のサプライズ)

**前年同期と比べるのは、季節性を除くため。**
小売業の第4四半期を第3四半期と比べても意味がない。

なぜ分母が標準偏差か
--------------------
**「いくら外したか」ではなく「いつもと比べてどれだけ外したか」を測る。**
利益が安定している企業の +5% と、
毎期乱高下する企業の +5% は、意味がまったく違う。

分母がゼロ（サプライズが毎回同じ）なら**作らない。**
無限大になるし、そもそも「驚き」が定義できない。

PIT
---
四半期 EPS は `periods.quarter_series` が YTD を分解して作る。
**Q4 は `FY − (Q1+Q2+Q3)` で復元される**ので、
復元された四半期を使ったかどうかを持ち回る（`derived_q4`）。

自己テスト
    python src/params_sue.py
"""
from __future__ import annotations

import pathlib
import statistics as st
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import periods as PE          # type: ignore  # noqa: E402
from params_us import Value   # type: ignore  # noqa: E402

MIN_HISTORY = 8       # サプライズの標準偏差に要る四半期数
TOP_K = 10            # L02 の「大型株」
MIN_MEMBERS = 15

PARAMS = ("N02", "L02")
REPLICATED_T = {"N02": 9.32, "L02": 3.21}

# L02 は業種内で一定なので**市場全体で正規化する**（params_ind と同じ）
MARKET_SCOPE = ("L02",)


def sue(eps_by_quarter: list[tuple[str, float]]) -> float | None:
    """**標準化サプライズ。** 新しい順でも古い順でも受ける。

    `eps_by_quarter` は `(期末日, 1株利益)` の並び。

    サプライズ = 当四半期 − **前年同期**（4つ前）
    SUE       = 直近のサプライズ / 過去のサプライズの標準偏差
    """
    rows = sorted(eps_by_quarter, key=lambda x: x[0])
    if len(rows) < MIN_HISTORY + 4:
        return None
    eps = [v for _, v in rows]
    # **4つ前との差。** 前年同期と比べて季節性を除く
    sur = [eps[i] - eps[i - 4] for i in range(4, len(eps))]
    if len(sur) < MIN_HISTORY:
        return None
    latest = sur[-1]
    hist = sur[-(MIN_HISTORY + 1):-1]      # **直近を分母に含めない**
    if len(hist) < MIN_HISTORY - 1:
        return None
    sd = st.pstdev(hist)
    if sd <= 0:
        # **驚きが定義できない。** 無限大にせず作らない
        return None
    return latest / sd


def n02(asof, cik: int, t: str) -> Value:
    """N02 = SUE。**1株利益ではなく利益そのもので計算する。**

    株数で割ると、自社株買いや増資が「サプライズ」に化ける。
    **標準化するので水準は問わない**（分子と分母の両方に効くだけ）。
    """
    q = PE.quarter_series(asof, cik, "NI", t)
    if not q or len(q.periods) < MIN_HISTORY + 4:
        return Value("N02", None, "四半期利益が %d 期に満たない"
                     % (MIN_HISTORY + 4))
    rows = [(p.ddate, p.value) for p in q.periods]
    v = sue(rows)
    if v is None:
        return Value("N02", None,
                     "サプライズの標準偏差がゼロ、または履歴が足りない")
    return Value("N02", float(v), derived_q4=getattr(q, "derived_q4", False))


def l02(members: list[dict]) -> float | None:
    """**同業大型株の SUE の平均。**

    `members` は `{"mcap": 時価総額, "sue": N02}`。
    L01 と同じく**時価総額上位10社の単純平均**。
    加重すると最大の1社で決まり、「大型株が驚いた」ではなく
    「その1社が驚いた」を測ることになる。
    """
    ok = [m for m in members
          if m.get("mcap") is not None and m.get("sue") is not None]
    if len(ok) < MIN_MEMBERS:
        return None
    ok.sort(key=lambda m: -m["mcap"])
    top = ok[:TOP_K]
    if len(top) < 3:
        return None
    return st.fmean([m["sue"] for m in top])


def compute_l02(by_sector: dict[str, list[dict]]) -> dict[str, Value]:
    out = {}
    for sec, mem in by_sector.items():
        v = l02(mem)
        out[sec] = (Value("L02", float(v)) if v is not None
                    else Value("L02", None,
                               "業種の銘柄数が %d 未満、または SUE が3銘柄未満"
                               % MIN_MEMBERS))
    return out


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    def near(a, b, tol=1e-9):
        return a is not None and abs(a - b) < tol

    print("src/params_sue.py 自己テスト")
    print("-" * 80)

    # 季節性のある利益（Q4 が大きい小売業の形）+ 最後だけ大きく上振れ
    base = [10, 12, 11, 30] * 4              # 16四半期
    rows = [("2020-%02d-01" % (i % 12 + 1), float(v))
            for i, v in enumerate(base)]
    rows = [("%04d-%02d-01" % (2016 + i // 4, (i % 4) * 3 + 3), float(v))
            for i, v in enumerate(base)]
    check("**季節性があってもサプライズは0**（毎年同じなら）",
          near(sue(rows), 0.0) or sue(rows) is None)

    # 前年同期比で毎期 +1 ずつ増える → サプライズは一定 → sd=0 → 作らない
    inc = [(r[0], r[1] + i // 4) for i, r in enumerate(rows)]
    check("**サプライズが一定なら作らない（sd=0）**", sue(inc) is None)

    # 最後だけ大きく上振れ
    up = list(rows)
    up[-1] = (up[-1][0], up[-1][1] + 20.0)
    # 過去のサプライズにばらつきを入れる
    up[4] = (up[4][0], up[4][1] + 1.0)
    up[9] = (up[9][0], up[9][1] - 1.0)
    v = sue(up)
    check("**最後だけ上振れれば SUE > 0**", v is not None and v > 1.0)

    check("並び順に依存しない", near(sue(list(reversed(up))), v))
    check("**履歴が足りなければ作らない**", sue(rows[:8]) is None)

    # **直近を分母に含めない**ことの確認
    # 直近が極端でも、分母（過去の sd）はそれに影響されない
    up2 = list(up); up2[-1] = (up2[-1][0], up2[-1][1] + 1000.0)
    v2 = sue(up2)
    check("**直近が極端なら SUE も極端になる（分母に含めていない）**",
          v2 is not None and abs(v2) > abs(v) * 10)

    # L02
    mem = ([{"mcap": 1e10 - i, "sue": 2.0} for i in range(10)]
           + [{"mcap": 1e8 - i, "sue": -2.0} for i in range(10)])
    check("**上位10社の SUE の平均**", near(l02(mem), 2.0))
    check("**最大の1社に支配されない**",
          near(l02([{"mcap": 1e12, "sue": 10.0}]
                   + [{"mcap": 1e9 - i, "sue": 0.0} for i in range(19)]), 1.0))
    check("銘柄数が少なければ作らない", l02(mem[:10]) is None)
    check("SUE が無い銘柄は数えない",
          l02([{"mcap": 1e9, "sue": None}] * 20) is None)

    r = compute_l02({"A": mem, "B": mem[:5]})
    check("業種ごとに返す", set(r) == {"A", "B"})
    check("**作れない業種は理由を持つ**",
          r["B"].value is None and bool(r["B"].reason))

    check("**N02 の再現 t は 9.32（採用規則を満たす）**",
          REPLICATED_T["N02"] >= 3.0)
    check("L02 の再現 t は 3.21", REPLICATED_T["L02"] >= 3.0)
    check("**L02 は市場全体で正規化する**", "L02" in MARKET_SCOPE)
    check("**N02 は業種内で正規化する**（銘柄ごとに違う値なので）",
          "N02" not in MARKET_SCOPE)
    check("分母に使う四半期数は8", MIN_HISTORY == 8)

    print("-" * 80)
    declared = 17
    if len(ran) != declared:
        fails.append("本数が宣言と違う")
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
