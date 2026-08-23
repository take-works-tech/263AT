#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
日次ファクター（MKT / SMB / HML）を全期間ぶん作って保存する。

`src/params_fx.py` の6本（I26 / I29 / I04 / G10 / I08 / I27）が
これを入力に取る。**一度作ってキャッシュする** — 毎回組み直すと
パネル構築が現実的な時間で終わらない。

PIT の規約 — **組み替えのタイミングがすべて**
--------------------------------------------
    月末 t の断面（時価総額・簿価）で 2×3 の組を決める
      → **その組を、翌月の各営業日に適用する**

**同じ月の中で組を決め直してはいけない。**
月中のリターンで組を決めると、その月のファクター自身に未来が入る。

簿価は `EQ`（自己資本）、時価総額は `SHARES × 終値`。
どちらも `AsOf` 経由なので **filed <= t** が保証される。

**組を決めた翌月に上場廃止した銘柄は、廃止日までのリターンで寄与する。**
月初に生きていた銘柄を月末の生存で選び直すと生存者バイアスが入る。

使い方
    .venv/Scripts/python.exe tools/build_factors.py
    .venv/Scripts/python.exe tools/build_factors.py --start 2012-01-31
"""
from __future__ import annotations

import argparse
import datetime as dt
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

import bars as BR             # noqa: E402
import facts as FA            # noqa: E402
import factors as FC          # noqa: E402
import listing as LS          # noqa: E402
import prices as PR           # noqa: E402

OUT = ROOT / "data" / "factors"
LOOKBACK_YEARS = 3


def month_ends(lo: str, hi: str) -> list[str]:
    out, d = [], dt.date.fromisoformat(lo)
    while d <= dt.date.fromisoformat(hi):
        nxt = dt.date(d.year + (d.month // 12), d.month % 12 + 1, 1)
        out.append((nxt - dt.timedelta(days=1)).isoformat())
        d = nxt
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2011-01-31")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--min-names", type=int, default=40,
                    help="この銘柄数を下回る月はファクターを作らない")
    a = ap.parse_args()

    tickers = sorted(p.stem for p in (ROOT / "data" / "prices").glob("*.json"))
    series = PR.load(tickers)
    by_ticker = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    rows_by = {t: BR.adjust(s.bars) for t, s in series.items()}
    # 日次リターンを**日付で引ける形**にしておく
    ret_by: dict[str, dict[str, float]] = {}
    close_by: dict[str, dict[str, float]] = {}
    for t, rows in rows_by.items():
        lr = BR.log_return(rows)
        ret_by[t] = {r["date"]: v for r, v in zip(rows, lr) if v is not None}
        close_by[t] = {r["date"]: r["close"] for r in rows}
    print("価格 %d 銘柄" % len(series))

    all_dates = sorted({d for m in ret_by.values() for d in m})
    print("営業日 %d 日（%s 〜 %s）"
          % (len(all_dates), all_dates[0], all_dates[-1]))

    out: list[dict] = []
    cur_year, asof = None, None
    skipped = 0

    mes = month_ends(a.start, a.end)
    for k, t in enumerate(mes):
        y = int(t[:4])
        if y != cur_year:
            qs = ["%dq%d" % (yy, q)
                  for yy in range(y - LOOKBACK_YEARS, y + 1)
                  for q in (1, 2, 3, 4)]
            asof = FA.AsOf(FA.load(qs))
            cur_year = y
            print("  --- %d年" % y)

        # --- 月末 t の断面で組を決める --------------------------------------
        mcap: dict[str, float] = {}
        bm: dict[str, float] = {}
        for tk in rows_by:
            m = by_ticker.get(tk)
            if not m or not m.cik:
                continue
            cik = int(m.cik)
            # **t 以前で最後の終値。** t が休場日でも良いように遡る
            px = None
            for d in reversed([d for d in close_by[tk] if d <= t][-5:]):
                px = close_by[tk][d]
                break
            if not px or px <= 0:
                continue
            sh = asof.latest_period(cik, "SHARES", 0, t, max_lag_days=400)
            eq = asof.latest_period(cik, "EQ", 0, t, max_lag_days=400)
            if not sh or sh.value <= 0:
                continue
            cap = sh.value * px
            mcap[tk] = cap
            if eq and eq.value > 0:
                bm[tk] = eq.value / cap        # 簿価 / 時価 = B/M

        groups = FC.assign(mcap, bm)
        if groups is None or len(groups) < a.min_names:
            skipped += 1
            continue

        # --- 翌月の各営業日に適用する ----------------------------------------
        nxt = mes[k + 1] if k + 1 < len(mes) else "9999-12-31"
        days = [d for d in all_dates if t < d <= nxt]
        made = 0
        for d in days:
            rets = {tk: ret_by[tk].get(d) for tk in groups}
            rets = {k2: v for k2, v in rets.items() if v is not None}
            fd = FC.one_day(d, rets, groups, mcap)
            if fd is None:
                continue
            out.append({"date": fd.date, "MKT": fd.mkt, "SMB": fd.smb,
                        "HML": fd.hml, "n": fd.n})
            made += 1
        print("  %s  組 %4d 銘柄 → 翌月 %2d/%2d 日"
              % (t, len(groups), made, len(days)))

    OUT.mkdir(parents=True, exist_ok=True)
    f = OUT / "daily.json"
    f.write_text(json.dumps(out), encoding="utf-8")
    print("-" * 72)
    print("**日次ファクター %d 日**（%s 〜 %s）"
          % (len(out), out[0]["date"] if out else "-",
             out[-1]["date"] if out else "-"))
    if skipped:
        # **飛ばした月を黙らせない。** 静かに減ると「全期間ある」と誤読する
        print("  **断面が薄くて作れなかった月: %d**" % skipped)
    if out:
        import statistics as st
        for key in ("MKT", "SMB", "HML"):
            xs = [r[key] for r in out]
            print("  %-4s 平均 %+.4f%%/日  sd %.3f%%  年率 %+.1f%%"
                  % (key, 100 * st.fmean(xs), 100 * st.pstdev(xs),
                     100 * st.fmean(xs) * 252))
        print("  構成銘柄数 中央値 %d"
              % st.median([r["n"] for r in out]))
    print("→ %s" % f.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
