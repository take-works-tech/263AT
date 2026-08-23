#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
価格を取り直す。**期間を明示的に指定する。**

なぜこの道具が要るか
--------------------
価格のキャッシュは、これまで**その場限りの呼び出しで作られていた。**
結果として **2年ぶんしか無く**（2024-08 〜 2026-08、実測）、
そのことが**パネル全体の制約になっていた。**

    パネルの期間 427日 ÷ 将来リターン 90日 = **独立観測 4.7 個**
    → 何を測っても「偶然と区別できない」という結論にしかならない

さらに、**季節性パラメータ（G38-G45）が1本も作れない。**
G38 は優先度 9.675 で**全 770 本の中で最上位**であるにもかかわらず、
2年の履歴では前年同月が1回しか無く、G40/G41/G44 は 11〜21年を要求する。

→ **パラメータを増やすことより、履歴を伸ばす方が効く局面がある。**
  実装は 10 → 29 本に増えたが、独立観測は 4.7 個のままだった。

使い方
    .venv/Scripts/python.exe tools/fetch_prices.py                 # 既存銘柄を20年
    .venv/Scripts/python.exe tools/fetch_prices.py --period max
    .venv/Scripts/python.exe tools/fetch_prices.py --only-short 2000
        # **すでに十分長いものは飛ばす。** 途中で止まっても再開できる
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import prices as PR        # noqa: E402

CACHE = ROOT / "data" / "prices"


def cached_tickers() -> list[str]:
    return sorted(p.stem for p in CACHE.glob("*.json"))


def n_bars(t: str) -> int:
    f = CACHE / (t + ".json")
    if not f.exists():
        return 0
    try:
        return len(json.loads(f.read_text(encoding="utf-8")).get("bars") or [])
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--period", default="20y",
                    help="yfinance の period（20y / max など）")
    ap.add_argument("--only-short", type=int, default=2500,
                    help="この本数未満の銘柄だけ取り直す（**再開できるように**）")
    ap.add_argument("--batch", type=int, default=40)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--tickers", nargs="*")
    args = ap.parse_args()

    ts = args.tickers or cached_tickers()
    todo = [t for t in ts if n_bars(t) < args.only_short]
    if args.limit:
        todo = todo[: args.limit]

    print("=" * 78)
    print("価格の取り直し（period=%s）" % args.period)
    print("=" * 78)
    print("キャッシュ %d 銘柄 / **取り直す %d 銘柄**（%d 本未満のもの）"
          % (len(ts), len(todo), args.only_short))
    if not todo:
        print("取り直すものは無い。")
        return 0

    done = 0
    for k in range(0, len(todo), args.batch):
        chunk = todo[k: k + args.batch]
        try:
            got = PR.from_yfinance(chunk, period=args.period,
                                   batch=args.batch)
        except Exception as e:
            # **失敗を握りつぶさない。** ただし1バッチの失敗で全部を捨てない
            print("  [%4d/%4d] **失敗** %s" % (k, len(todo), e))
            continue
        if got:
            PR.save(got)
            done += len(got)
        lens = sorted(len(s.bars) for s in got.values())
        print("  [%4d/%4d] %2d 銘柄  本数 中央値 %s"
              % (min(k + args.batch, len(todo)), len(todo), len(got),
                 lens[len(lens) // 2] if lens else "-"))

    print("-" * 78)
    print("**取り直した %d 銘柄**" % done)
    after = [n_bars(t) for t in ts]
    after = [x for x in after if x]
    if after:
        after.sort()
        print("  本数: 最小 %d / 中央値 %d / 最大 %d"
              % (after[0], after[len(after) // 2], after[-1]))
        yrs = after[len(after) // 2] / 252.0
        print("  → 中央値で **約 %.1f 年**" % yrs)
        for need, what in ((2, "G38 前年同月"), (6, "G39 2-5年"),
                           (11, "G40 6-10年"), (16, "G41 11-15年"),
                           (21, "G44 16-20年")):
            ok = sum(1 for x in after if x >= need * 252)
            print("     %-14s（%2d年必要）: %4d / %d 銘柄"
                  % (what, need, ok, len(after)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
