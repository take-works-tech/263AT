#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
事前登録した X01-X06 を**一度だけ**測る。

**この道具は繰り返し実行してはいけない。**
閾値や窓を変えて測り直せば、それは事前登録ではなくなる。
docs/07_preregistration.md §3 の通り、測るのは一度だけ。

評価は2つ。
  (a) **上位N銘柄の平均リターン**（平均への効き）
  (b) **5年で10倍になった割合**（裾への効き）
**X04 は (a) に効かず (b) に効く**と予測している。両方を測る。
"""
from __future__ import annotations
import argparse, datetime as dt, json, math, pathlib, statistics as st, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass

import bars as BR, facts as FA, ff49, listing as LS, params_ex as PX, periods as PE, prices as PR  # noqa

EXT = ROOT / "data" / "extern"


def load_ext(tk):
    f = EXT / (tk.replace("/", "_") + ".json")
    if not f.exists(): return None
    try: return json.loads(f.read_text(encoding="utf-8"))
    except Exception: return None


def fwd_mult(rows, t, years):
    a = [x for x in rows if x["date"] > t]
    if not a: return None
    entry = a[0]["open"]
    end = (dt.date.fromisoformat(a[0]["date"]) + dt.timedelta(days=int(365.25*years))).isoformat()
    b = [x for x in rows if x["date"] > end]
    if not b or entry <= 0: return None
    return b[0]["open"] / entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="gate")
    ap.add_argument("--every", type=int, default=6)
    ap.add_argument("--years", type=float, default=5.0)
    a = ap.parse_args()

    d = ROOT / "data" / "panel" / a.panel
    panel = {}
    for f in sorted(d.glob("*_h250.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        if r: panel[r[0]["date"]] = r
    dates = sorted(panel)
    limit = (dt.date.fromisoformat(dates[-1]) - dt.timedelta(days=int(365.25*a.years))).isoformat()
    sample = [t for i, t in enumerate(dates) if i % a.every == 0 and t <= limit]

    by = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    sic_asof = LS.SicAsOf.from_dera()
    print("=" * 78)
    print("事前登録の測定（%s / %d 時点 / %.0f年後）" % (a.panel, len(sample), a.years))
    print("=" * 78)
    print("**docs/07_preregistration.md に書いた予測と照合する。**")
    print("測るのは一度だけ。閾値や窓を変えて測り直さない。")
    print()

    obs = {p: [] for p in PX.PREDICTED}      # (値, 倍率)
    n_rows = n_ext = 0
    cur_year, asof = None, None
    for T in sample:
        y = int(T[:4])
        if y != cur_year:
            qs = ["%dq%d" % (yy, q) for yy in range(y-3, y+1) for q in (1,2,3,4)]
            need = {int(by[r["ticker"]].cik) for r in panel[T]
                    if by.get(r["ticker"]) and by[r["ticker"]].cik}
            asof = FA.AsOf(FA.load(qs, ciks=need)); cur_year = y
        for r in panel[T]:
            tk = r["ticker"]; n_rows += 1
            rec = load_ext(tk)
            if rec is None: continue
            n_ext += 1
            m = by.get(tk); cik = int(m.cik) if m and m.cik else None
            rev = None
            if cik:
                v = PE.ttm(asof, cik, "REV", T)
                if v: rev = v.value / 1e6
            vals = PX.compute(rec, T, r.get("sector"), rev)
            s = PR.load([tk]).get(tk)
            if not s: continue
            mult = fwd_mult(BR.adjust(s.bars), T, a.years)
            if mult is None: continue
            for p, x in vals.items():
                if x.value is not None: obs[p].append((x.value, mult))

    print("パネル %d 行 / 外部データあり %d 行（**%.0f%%**）"
          % (n_rows, n_ext, 100.0*n_ext/max(1, n_rows)))
    print()
    print("%-5s %7s %9s %9s %9s %9s %-30s" % ("ID","観測","上位20%","下位20%","差","10倍+差","事前登録の予測"))
    print("-" * 96)
    res = {}
    for p in sorted(PX.PREDICTED):
        v = obs[p]
        if len(v) < 200:
            print("%-5s %7d  （観測が足りない）  %s" % (p, len(v), PX.PREDICTED[p]["note"]))
            continue
        v.sort(key=lambda x: -x[0])
        k = max(1, len(v)//5)
        top = [m for _, m in v[:k]]; bot = [m for _, m in v[-k:]]
        t10 = 100*sum(1 for m in top if m >= 10)/len(top)
        b10 = 100*sum(1 for m in bot if m >= 10)/len(bot)
        res[p] = {"n": len(v), "top": st.fmean(top), "bot": st.fmean(bot),
                  "diff": st.fmean(top)-st.fmean(bot), "t10": t10, "b10": b10,
                  "d10": t10-b10}
        print("%-5s %7d %9.2f %9.2f %+9.2f %+8.2fpp %s"
              % (p, len(v), st.fmean(top), st.fmean(bot),
                 st.fmean(top)-st.fmean(bot), t10-b10, PX.PREDICTED[p]["note"][:30]))

    print()
    print("-" * 96)
    print("**予測との照合**")
    print("-" * 96)
    for p in sorted(res):
        pr = PX.PREDICTED[p]; r = res[p]
        def judge(pred, got, thr):
            if pred == 0: return "**予測どおり効かず**" if abs(got) < thr else "**予測は「効かない」だったが動いた**"
            return "**予測どおり**" if (got > thr if pred > 0 else got < -thr) else "**予測と逆または無**"
        print("  %-5s 平均: %-34s 裾: %s"
              % (p, judge(pr["mean"], r["diff"], 0.15), judge(pr["tail"], r["d10"], 0.5)))
    print()
    print("  " + "!"*60)
    print("  **生存者バイアスはここでも消えない。** 廃止銘柄は最初から入らない。")
    print("  観測は重なっている（%d 時点、%.0f年後）。**独立ではない。**" % (len(sample), a.years))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
