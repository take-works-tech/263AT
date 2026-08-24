#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**評価額の推移から、転がり窓の年率を測る。**

なぜ道具にするか
----------------
「**3年以上の窓でマイナスにならなかった（12/12、最悪 +3.2%）**」は
現在の設計方針そのものになっている主張だが、
**これまで手で計算していた。**

構成を変えるたびに手で計算し直していては、
**変えた結果この主張が壊れたことに気づかない。**

    .venv/Scripts/python.exe tools/run_system.py --panel common --horizon 250 \
        --dump data/eq_common.json
    .venv/Scripts/python.exe tools/measure_windows.py data/eq_common.json \
        --compare data/eq_d13.json

何を測るか
----------
| 期間 | 最悪 / 中央値 / 最良 / **マイナスの窓の数** |

**平均ではなく最悪と中央値を見る。**
「マイナスにしない」が目標なので、**平均が良くても最悪が負ければ意味がない。**

窓は月末ごとに1本ずつ取る。**窓どうしは重なっている**ので、
「12本中0本」は独立な12回の試行ではない。
13.5年で3年窓なら、**独立なのは実質4〜5本**である。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import statistics as st
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load(p: str) -> list[tuple[str, float]]:
    rows = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
    return [(r["date"], float(r["value"])) for r in rows
            if r.get("value")]


def windows(eq: list[tuple[str, float]], years: float) -> list[float]:
    """`years` 年の窓ごとの**年率**を返す。

    始点を月末ごとに1本ずつずらす。終点はその始点から `years` 年後
    **以降で最初に見つかる月末**。無ければその窓は作らない。
    """
    out = []
    for i, (d0, v0) in enumerate(eq):
        if v0 <= 0:
            continue
        target = (dt.date.fromisoformat(d0)
                  + dt.timedelta(days=int(round(years * 365.25)))).isoformat()
        nxt = [(d, v) for d, v in eq[i + 1:] if d >= target]
        if not nxt:
            continue
        d1, v1 = nxt[0]
        n = (dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days / 365.25
        if n <= 0:
            continue
        out.append((v1 / v0) ** (1.0 / n) - 1.0)
    return out


def table(eq: list[tuple[str, float]], yrs: list[float]) -> list[tuple]:
    rows = []
    for y in yrs:
        w = windows(eq, y)
        if not w:
            rows.append((y, None, None, None, 0, 0))
            continue
        neg = sum(1 for x in w if x < 0)
        rows.append((y, min(w), st.median(w), max(w), neg, len(w)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("equity", help="run_system.py --dump が書いた JSON")
    ap.add_argument("--compare", default="", help="比較する側の JSON")
    ap.add_argument("--years", type=float, nargs="*",
                    default=[1.0, 3.0, 5.0])
    a = ap.parse_args()

    eq = load(a.equity)
    if len(eq) < 13:
        print("**推移が短すぎる**: %d 点" % len(eq))
        return 1

    def show(name: str, rows: list[tuple]):
        print("  %s" % name)
        print("    %-6s %9s %9s %9s   %s"
              % ("期間", "最悪", "中央値", "最良", "マイナスの窓"))
        for y, lo, md, hi, neg, n in rows:
            if lo is None:
                print("    %-6s **窓が作れない**" % ("%.0f年" % y))
                continue
            mark = "**%d / %d**" % (neg, n) if neg else "%d / %d" % (neg, n)
            print("    %-6s %+8.1f%% %+8.1f%% %+8.1f%%   %s"
                  % ("%.0f年" % y, 100 * lo, 100 * md, 100 * hi, mark))

    print("=" * 78)
    print("転がり窓の年率（**平均ではなく最悪を見る**）")
    print("=" * 78)
    print("  %s 〜 %s / %d 点" % (eq[0][0], eq[-1][0], len(eq)))
    print("  **最終 %s円（元本 %s円）**"
          % ("{:,.0f}".format(eq[-1][1]), "{:,.0f}".format(eq[0][1])))
    print()
    rows = table(eq, a.years)
    show(pathlib.Path(a.equity).name, rows)

    if a.compare:
        p = pathlib.Path(a.compare)
        if not p.exists():
            print("\n  **比較先が無い**: %s" % a.compare)
            return 0
        eq2 = load(a.compare)
        rows2 = table(eq2, a.years)
        print()
        show(p.name, rows2)
        print()
        print("  差（%s − %s）" % (pathlib.Path(a.equity).name, p.name))
        for (y, lo, md, hi, neg, n), (_, lo2, md2, hi2, neg2, _n2) \
                in zip(rows, rows2):
            if lo is None or lo2 is None:
                continue
            print("    %-6s 最悪 %+.1fpp / 中央値 %+.1fpp / マイナスの窓 %+d"
                  % ("%.0f年" % y, 100 * (lo - lo2), 100 * (md - md2),
                     neg - neg2))

    print()
    print("  **窓どうしは重なっている。** 13.5年で3年窓なら、")
    print("  見かけ 12本でも**独立なのは実質4〜5本**である。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
