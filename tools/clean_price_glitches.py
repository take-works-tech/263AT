#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
価格キャッシュの**分割誤調整バー**を売買不能にする（事前登録 第5回 SPL-1）。

何が起きているか
----------------
yfinance の分割調整が、一部の銘柄で**数日だけ二重（または未）調整の島**を残す。
島の前後で |日次リターン| = ln(分割比率) ちょうどの偽ジャンプが出る。

検出器（prices.verify の検査1）は既にあったが、
**fetch_prices.py が保存時に呼んでいなかった。** 実測で 115 バーが残っている。

どう直すか
----------
価格を「正しい値に修正」はしない（推測になる）。
**該当バーを halted=True / volume=0 にして、**
- 約定できない（portfolio.next_open が飛ばす）
- リターンが欠損になる（bars.log_return が None を返す）
ようにする。偽の ±ln(f) がボラ推定・fwd ラベル・裾の測定に入らなくなる。

    .venv/Scripts/python.exe tools/clean_price_glitches.py          # 検査のみ
    .venv/Scripts/python.exe tools/clean_price_glitches.py --write  # 書き込む
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PRICES = ROOT / "data" / "prices"

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def find_glitches(bars: list[dict], splits: list) -> list[int]:
    """誤調整の島に属するバーの添字を返す。

    判定は prices.verify の検査1と同じ:
    **分割日の±3日で、|リターン| が |ln(比率)| と 0.15 以内で一致**。
    入口のジャンプを見つけたら、5営業日以内の逆向きジャンプまでを島とする。
    """
    hit: set[int] = set()
    n = len(bars)
    for i in range(1, n):
        c0, c1 = bars[i - 1]["close"], bars[i]["close"]
        if c0 <= 0 or c1 <= 0:
            continue
        r = math.log(c1 / c0)
        if abs(r) < 0.35:
            continue
        d = dt.date.fromisoformat(bars[i]["date"])
        for sd, f in splits:
            if f <= 0:
                continue
            if abs((d - dt.date.fromisoformat(sd)).days) > 3:
                continue
            if abs(abs(r) - abs(math.log(f))) >= 0.15:
                continue
            # 入口ジャンプ。島の終わり（逆向きジャンプ）を探す
            j_end = i           # 既定は1バーの島
            for j in range(i + 1, min(i + 6, n)):
                cj0, cj1 = bars[j - 1]["close"], bars[j]["close"]
                if cj0 <= 0 or cj1 <= 0:
                    continue
                rj = math.log(cj1 / cj0)
                if (abs(abs(rj) - abs(math.log(f))) < 0.15
                        and rj * r < 0):
                    j_end = j - 1
                    break
            for k in range(i, j_end + 1):
                hit.add(k)
            break
    return sorted(hit)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true",
                    help="実際に書き込む（既定は検査のみ）")
    a = ap.parse_args()

    files = sorted(PRICES.glob("*.json"))
    total_bars, total_files = 0, 0
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        splits = d.get("splits") or []
        if not splits:
            continue
        bars = d.get("bars") or []
        idx = find_glitches(bars, splits)
        # 既に清掃済みのバーは数えない（冪等にする）
        idx = [i for i in idx if not bars[i].get("glitch")]
        if not idx:
            continue
        total_files += 1
        total_bars += len(idx)
        print("%-8s %d バー: %s" % (d.get("ticker", p.stem), len(idx),
                                    ", ".join(bars[i]["date"] for i in idx[:6])
                                    + ("…" if len(idx) > 6 else "")))
        if a.write:
            for i in idx:
                bars[i]["halted"] = True
                bars[i]["volume"] = 0.0
                bars[i]["glitch"] = True   # 清掃の印（冪等性）
            d["note"] = (d.get("note") or "") + \
                " / 分割誤調整バー %d 本を halted 化（clean_price_glitches）" % len(idx)
            p.write_text(json.dumps(d), encoding="utf-8")

    print("-" * 60)
    print("**%d 銘柄 / %d バー** が分割誤調整の島" % (total_files, total_bars))
    if not a.write and total_bars:
        print("--write で halted 化する")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
