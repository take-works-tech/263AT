#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**右の裾を測る。** スコアは「平均より少し良い銘柄」ではなく、
**数倍〜数十倍になる銘柄**を当てられるのか。

なぜこれを測るか
----------------
263AT の設計方針は「勝率ではなく期待値」である。
個別株はポートフォリオの1割なので、**9割が負けても、
残り1割が何十倍になれば全体は勝つ。**
Bessembinder (JFE 2018) の「上位4%の企業が全ての富を生む」がその根拠。

**ところが、これまで測っていたのは分位の平均リターンだった。**
平均は「少し良い銘柄を大量に持つ」戦略を高く評価する。
**10倍株を1本当てる能力は、平均ではほとんど見えない。**

    上位20%の平均が +6% でも、
    その中に 10倍株が1本あるのか、+6% の銘柄が並んでいるのかで、
    **取るべき設計がまったく違う。**
      前者 → 少数を長く持ち、勝ち馬を削らない
      後者 → 広く持ち、定期的に入れ替える

**今のシステムは後者の作りである。** 月次で入れ替え、
勝った銘柄は目標ウェイトまで削る。**10倍株を持ち続けられない。**

測るもの
--------
スコアの十分位ごとに、**3年後・5年後**のリターン分布を出す。
平均や中央値ではなく、**2倍以上 / 5倍以上 / 10倍以上になった割合。**

    もし上位十分位で10倍株の出現率が高ければ → 方針は正しく、設計を変える
    もし十分位で差が無ければ → **スコアに裾を当てる力は無い**

使い方
    .venv/Scripts/python.exe tools/measure_tail.py
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import statistics as st
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import bars as BR             # noqa: E402
import prices as PR           # noqa: E402
import prior as PRIOR         # noqa: E402
import shrink as SH           # noqa: E402


def usable_at(row: dict, T: str, horizon: int) -> bool:
    if row["date"] >= T or row.get("fwd") is None:
        return False
    resolved = (dt.date.fromisoformat(row["date"])
                + dt.timedelta(days=horizon)).isoformat()
    return resolved <= T


def fwd_mult(rows: list[dict], t: str, years: float) -> float | None:
    """t の翌営業日の始値で買い、`years` 年後の始値で売った**倍率**。

    **リターンではなく倍率**にするのは、10倍を「+900%」と書くと
    分布の形が見えにくいから。
    """
    a = [x for x in rows if x["date"] > t]
    if not a:
        return None
    entry = a[0]["open"]
    end = (dt.date.fromisoformat(a[0]["date"])
           + dt.timedelta(days=int(365.25 * years))).isoformat()
    b = [x for x in rows if x["date"] > end]
    if not b or entry <= 0:
        # **期間が足りないものは None。** 最後の値で代用しない
        # （代用すると、途中で上場廃止した銘柄が「途中まで」で評価される）
        return None
    return b[0]["open"] / entry


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="gate")
    ap.add_argument("--horizon", type=int, default=250)
    ap.add_argument("--every", type=int, default=6, help="何ヶ月ごとに測るか")
    ap.add_argument("--years", type=float, nargs="*", default=[3.0, 5.0])
    ap.add_argument("--deciles", type=int, default=10)
    a = ap.parse_args()

    d = ROOT / "data" / "panel" / a.panel
    panel: dict[str, list[dict]] = {}
    for f in sorted(d.glob("*_h%d.json" % a.horizon)):
        rows = json.loads(f.read_text(encoding="utf-8"))
        if rows:
            panel[rows[0]["date"]] = rows
    if not panel:
        print("パネルが無い")
        return 1
    dates = sorted(panel)
    flat = [r for t in dates for r in panel[t]]
    names = [n for n in sorted({k for r in flat for k in r["z"]})
             if n in PRIOR.ADOPTED]

    # 5年先が測れる日付だけ
    limit = (dt.date.fromisoformat(dates[-1])
             - dt.timedelta(days=int(365.25 * max(a.years)))).isoformat()
    sample = [t for i, t in enumerate(dates)
              if i % a.every == 0 and t <= limit]

    print("=" * 78)
    print("右の裾の測定（%s / %d本 / %d 時点）" % (a.panel, len(names),
                                                  len(sample)))
    print("=" * 78)
    print("**測るのは平均ではなく「何倍になったか」の分布。**")
    print("期待値を狙う設計なら、**上位でこそ10倍株の出現率が高い**はず。")
    print()

    # --- 1) スコアを付ける（歩進みで重みを推定）--------------------------
    scored: dict[str, list[tuple[str, float]]] = {}
    for T in sample:
        train = [r for r in flat if usable_at(r, T, a.horizon)]
        if len(train) < 2000:
            continue
        fit = SH.fit(train, names)
        xs = []
        for r in panel[T]:
            s = SH.score(r["z"], fit)
            if s is not None:
                xs.append((r["ticker"], s))
        if len(xs) >= 200:
            xs.sort(key=lambda x: -x[1])
            scored[T] = xs
    if not scored:
        print("スコアが付けられなかった")
        return 0
    print("スコアを付けた時点 %d（%s 〜 %s）"
          % (len(scored), min(scored), max(scored)))

    # --- 2) 倍率を測る（**銘柄ごとに1回だけ読む**）------------------------
    need: dict[str, set[str]] = {}
    for T, xs in scored.items():
        for tk, _ in xs:
            need.setdefault(tk, set()).add(T)
    print("価格を読む銘柄 %d" % len(need))
    mult: dict[tuple[str, str, float], float] = {}
    for i, (tk, ts) in enumerate(need.items()):
        s = PR.load([tk]).get(tk)
        if not s:
            continue
        rows = BR.adjust(s.bars)
        for T in ts:
            for y in a.years:
                m = fwd_mult(rows, T, y)
                if m is not None:
                    mult[(tk, T, y)] = m
        if (i + 1) % 2000 == 0:
            print("  %d/%d" % (i + 1, len(need)))

    # --- 3) 十分位ごとに分布 ---------------------------------------------
    for y in a.years:
        print()
        print("-" * 78)
        print("**%.0f年後の倍率**（%d時点をまとめたもの）" % (y, len(scored)))
        print("-" * 78)
        print("%-6s %7s %8s %8s %8s %8s %8s %8s"
              % ("十分位", "銘柄数", "中央値", "平均", "**2倍+**",
                 "**5倍+**", "**10倍+**", "0.5倍-"))
        rowsD: list[list[float]] = [[] for _ in range(a.deciles)]
        for T, xs in scored.items():
            n = len(xs)
            for rank, (tk, _) in enumerate(xs):
                m = mult.get((tk, T, y))
                if m is None:
                    continue
                dgrp = min(a.deciles - 1, rank * a.deciles // n)
                rowsD[dgrp].append(m)
        tot = sum(len(v) for v in rowsD)
        for k, v in enumerate(rowsD):
            if not v:
                continue
            print("%-6s %7d %8.2f %8.2f %7.1f%% %7.1f%% %7.1f%% %7.1f%%"
                  % ("上位%d" % (k + 1) if k == 0 else "%d" % (k + 1),
                     len(v), st.median(v), st.fmean(v),
                     100 * sum(1 for x in v if x >= 2) / len(v),
                     100 * sum(1 for x in v if x >= 5) / len(v),
                     100 * sum(1 for x in v if x >= 10) / len(v),
                     100 * sum(1 for x in v if x < 0.5) / len(v)))
        allv = [x for v in rowsD for x in v]
        if allv:
            print("%-6s %7d %8.2f %8.2f %7.1f%% %7.1f%% %7.1f%% %7.1f%%"
                  % ("全体", len(allv), st.median(allv), st.fmean(allv),
                     100 * sum(1 for x in allv if x >= 2) / len(allv),
                     100 * sum(1 for x in allv if x >= 5) / len(allv),
                     100 * sum(1 for x in allv if x >= 10) / len(allv),
                     100 * sum(1 for x in allv if x < 0.5) / len(allv)))
        # **上位に10倍株が集中しているか**が判定の核心
        top = rowsD[0]
        rest = [x for v in rowsD[1:] for x in v]
        if top and rest:
            a10 = 100 * sum(1 for x in top if x >= 10) / len(top)
            b10 = 100 * sum(1 for x in rest if x >= 10) / len(rest)
            print()
            print("  **上位十分位の10倍株出現率 %.2f%% / その他 %.2f%%（倍率 %.2f）**"
                  % (a10, b10, (a10 / b10) if b10 > 0 else float("inf")))
            if a10 <= b10 * 1.2:
                print("  → **スコアに右の裾を当てる力は見当たらない。**")
                print("     期待値を狙う設計にするなら、**別の材料が要る。**")
            else:
                print("  → **上位に裾が偏っている。**")
                print("     勝ち馬を削らず長く持つ設計に変える価値がある。")

    print()
    print("  " + "!" * 60)
    print("  生存者バイアスは**この測定で特に強く効く。**")
    print("  倒産・上場廃止した銘柄は価格が取れないので**最初から入らない。**")
    print("  → **下振れが過小評価され、10倍株の出現率は過大に出る。**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
