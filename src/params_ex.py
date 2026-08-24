#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外部データのパラメータ X01-X06。**docs/07_preregistration.md の定義そのまま。**

**この実装は事前登録の後に書いている。**
定義を変えたくなっても変えない。変えたら事前登録の意味が消える。
（`tools/freeze_prereg.py` が文書の書き換えを検出する）

| ID | 定義 | **予測した符号** |
|---|---|---|
| X01 | `ln(1 + 累計論文数)` | **効かない**（規模の代理でしかない） |
| X02 | `累計(t) / 累計(t−3年) − 1` | 弱い + |
| X03 | `年間論文数 / 売上（百万ドル）` | 弱い +（X02 より弱い） |
| X04 | `ln(1 + 登録済み試験数)` | **裾に +、平均には符号なし** ← 本命 |
| X05 | `第3相 / 全試験` | 平均に +、**裾には −** |
| X06 | `ln(1 + 第1相の数)` | 裾に +、X04 より強い |

**時点の扱い**
--------------
- 論文は**年次**なので、`t` の年が終わっていなければその年は含めない
- 臨床試験は**登録日**で切る（`studyFirstSubmitDate <= t`）
- **成否・引用数は使わない**（`extern.PIT_UNSAFE`）

**業種の扱い**
--------------
臨床試験（X04-X06）は**医薬・バイオの業種内でのみ**測る。
半導体企業の試験数を製薬企業と比べても意味がない。
全体で測ると、実質的に「医薬品業種かどうか」のダミー変数になる。

自己テスト
    python src/params_ex.py
"""
from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from params_us import Value  # type: ignore  # noqa: E402

# 臨床試験を比べてよい業種（Fama-French 49）
BIO_INDUSTRIES = {"Drugs", "MedEq", "Hlth", "LabEq", "Chems"}

# 事前登録した予測。**測定の後にここを書き換えない。**
PREDICTED = {
    "X01": {"mean": 0, "tail": 0, "note": "規模の代理。効かないと予測"},
    "X02": {"mean": +1, "tail": 0, "note": "弱い +"},
    "X03": {"mean": +1, "tail": 0, "note": "X02 より弱い +"},
    "X04": {"mean": 0, "tail": +1, "note": "**本命。** 裾に +、平均には符号なし"},
    "X05": {"mean": +1, "tail": -1, "note": "確度は高いが伸びしろが小さい"},
    "X06": {"mean": 0, "tail": +1, "note": "X04 より裾への効きが強い"},
}


def papers_asof(rec: dict, t: str) -> int | None:
    """その日までの累計論文数。**年が終わっていなければその年は含めない。**"""
    by = rec.get("papers_by_year") or {}
    if not by:
        return None
    y = int(t[:4])
    done = y if t[5:] >= "12-31" else y - 1
    return sum(v for k, v in by.items() if int(k) <= done)


def trials_asof(rec: dict, t: str) -> dict | None:
    """その日までに**登録された**試験を相ごとに数える。

    `trials_rows`（1件ごとの登録日と相）が要る。
    **集計済みの古い形式は使わない** — それは今日の姿なので。
    """
    rows = rec.get("trials_rows")
    if rows is None:
        return None
    out = {"total": 0, "p1": 0, "p2": 0, "p3": 0}
    for r in rows:
        if r.get("submit", "9999") > t:
            continue
        out["total"] += 1
        ph = r.get("phase") or ""
        if "PHASE3" in ph:
            out["p3"] += 1
        if ph == "PHASE1" or ph.startswith("PHASE1"):
            out["p1"] += 1
        if "PHASE2" in ph:
            out["p2"] += 1
    return out


def compute(rec: dict | None, t: str, sector: str | None,
            revenue_musd: float | None) -> dict[str, Value]:
    """X01-X06 を作る。**作れないものは理由つきで None。**"""
    out: dict[str, Value] = {}

    def put(pid, v, reason=""):
        if v is None:
            out[pid] = Value(pid, None, reason or "入力が無い")
        elif not math.isfinite(v):
            out[pid] = Value(pid, None, "有限でない値")
        else:
            out[pid] = Value(pid, float(v))

    if rec is None:
        for p in PREDICTED:
            out[p] = Value(p, None, "外部データが無い")
        return out

    # --- 論文 ---------------------------------------------------------------
    cum = papers_asof(rec, t)
    put("X01", math.log1p(cum) if cum is not None else None,
        "論文データが無い")

    if cum is None:
        put("X02", None, "論文データが無い")
        put("X03", None, "論文データが無い")
    else:
        y3 = "%04d%s" % (int(t[:4]) - 3, t[4:])
        prev = papers_asof(rec, y3)
        # **分母がゼロなら作らない。** 0 から 1 になった企業を
        # 「無限大の成長」として扱うと、断面の順位が壊れる
        put("X02", (cum / prev - 1.0) if (prev and prev > 0) else None,
            "3年前の累計がゼロ（成長率が定義できない）")
        by = rec.get("papers_by_year") or {}
        yy = int(t[:4]) if t[5:] >= "12-31" else int(t[:4]) - 1
        annual = by.get(str(yy))
        put("X03",
            (annual / revenue_musd)
            if (annual is not None and revenue_musd and revenue_musd > 0)
            else None,
            "その年の論文数か売上が無い")

    # --- 臨床試験（**医薬・バイオの業種内でのみ**）--------------------------
    tr = trials_asof(rec, t)
    if tr is None:
        for p in ("X04", "X05", "X06"):
            put(p, None, "臨床試験データが時点別の形になっていない")
    elif sector not in BIO_INDUSTRIES:
        for p in ("X04", "X05", "X06"):
            put(p, None, "医薬・バイオ以外なので比べない（業種ダミーになる）")
    else:
        put("X04", math.log1p(tr["total"]))
        put("X05", (tr["p3"] / tr["total"]) if tr["total"] else None,
            "試験が1件も無い")
        put("X06", math.log1p(tr["p1"]))
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

    print("src/params_ex.py 自己テスト")
    print("-" * 80)

    rec = {
        "papers_by_year": {"2010": 10, "2011": 10, "2012": 10,
                           "2013": 20, "2014": 20, "2015": 30},
        "trials_rows": [
            {"submit": "2011-05-01", "phase": "PHASE1"},
            {"submit": "2013-05-01", "phase": "PHASE1"},
            {"submit": "2014-05-01", "phase": "PHASE2"},
            {"submit": "2015-05-01", "phase": "PHASE3"},
            {"submit": "2019-05-01", "phase": "PHASE3"},
        ],
    }

    # --- 時点で切れること ---------------------------------------------------
    check("**年末なら その年を含める**", papers_asof(rec, "2015-12-31") == 100)
    check("**年の途中ならその年を含めない**",
          papers_asof(rec, "2015-06-30") == 70)
    check("**基準日より後に登録された試験を数えない**",
          trials_asof(rec, "2015-12-31")["total"] == 4)
    check("今日まで数えれば全部", trials_asof(rec, "2030-01-01")["total"] == 5)
    check("相ごとに分かれる",
          trials_asof(rec, "2015-12-31")["p3"] == 1
          and trials_asof(rec, "2015-12-31")["p1"] == 2)

    # --- 定義が事前登録どおりか --------------------------------------------
    v = compute(rec, "2015-12-31", "Drugs", 50.0)
    check("**X01 = ln(1+累計)**", near(v["X01"].value, math.log1p(100)))
    # 2012年末までの累計は 10+10+10 = **30**（最初 40 と書いて検査が落ちた。
    # **実装ではなく期待値の方が誤っていた**）
    check("**X02 = 累計(t)/累計(t-3) - 1**",
          near(v["X02"].value, 100 / 30.0 - 1.0))
    check("3年前の基準日も年末なら その年を含む",
          papers_asof(rec, "2012-12-31") == 30)
    check("**X03 = その年の論文数 / 売上**", near(v["X03"].value, 30 / 50.0))
    check("**X04 = ln(1+試験数)**", near(v["X04"].value, math.log1p(4)))
    check("**X05 = 第3相 / 全試験**", near(v["X05"].value, 1 / 4.0))
    check("**X06 = ln(1+第1相)**", near(v["X06"].value, math.log1p(2)))

    # --- 業種で切ること -----------------------------------------------------
    w = compute(rec, "2015-12-31", "Chips", 50.0)
    check("**医薬以外では臨床試験を作らない**",
          all(w[p].value is None for p in ("X04", "X05", "X06")))
    check("理由に業種ダミーの話が書いてある", "業種ダミー" in w["X04"].reason)
    check("論文は業種を問わず作る", w["X01"].value is not None)

    # --- 作れないときは理由つきで None --------------------------------------
    n = compute(None, "2015-12-31", "Drugs", 50.0)
    check("**外部データが無ければ全部 None**",
          all(x.value is None for x in n.values()))
    check("6本すべてを返す", len(n) == 6)

    z = compute({"papers_by_year": {"2015": 5}}, "2015-12-31", "Drugs", 50.0)
    check("**3年前がゼロなら成長率を作らない**", z["X02"].value is None)
    check("理由を書く", "ゼロ" in z["X02"].reason)
    check("**時点別の形が無ければ臨床試験を作らない**", z["X04"].value is None)

    r0 = compute(rec, "2015-12-31", "Drugs", 0.0)
    check("売上ゼロなら X03 を作らない", r0["X03"].value is None)

    # --- 事前登録の予測が記録されていること --------------------------------
    check("**X04 の予測は「裾に +、平均は符号なし」**",
          PREDICTED["X04"]["tail"] == +1 and PREDICTED["X04"]["mean"] == 0)
    check("**X01 の予測は「効かない」**",
          PREDICTED["X01"]["mean"] == 0 and PREDICTED["X01"]["tail"] == 0)
    check("**X05 は平均と裾で符号が逆**",
          PREDICTED["X05"]["mean"] * PREDICTED["X05"]["tail"] < 0)
    check("6本すべてに予測がある", len(PREDICTED) == 6)

    print("-" * 80)
    declared = 25
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
