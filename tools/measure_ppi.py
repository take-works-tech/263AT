#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
事前登録（第2回）の Y01-Y04 を**一度だけ**測る。

docs/08_preregistration_ppi.md §4 の通り、
遅れ（2ヶ月）も窓（12/6ヶ月）も変えない。評価は3つ。
  (a) 上位20%の平均リターン（250日）
  (b) 10倍株の出現率（5年、**符号なしと予測**）
  (c) 時価総額の下位半分だけで測った (a)（小型でより強いという予測）
"""
from __future__ import annotations
import argparse, datetime as dt, json, pathlib, statistics as st, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
import bars as BR, ppi as PP, prices as PR

PRED = {"Y01": {"mean": -1, "tail": 0}, "Y02": {"mean": +1, "tail": 0},
        "Y03": {"mean": +1, "tail": 0}, "Y04": {"mean": +1, "tail": 0}}


def fwd_mult(rows, t, years):
    a = [x for x in rows if x["date"] > t]
    if not a: return None
    e = a[0]["open"]
    end = (dt.date.fromisoformat(a[0]["date"]) + dt.timedelta(days=int(365.25*years))).isoformat()
    b = [x for x in rows if x["date"] > end]
    return (b[0]["open"]/e) if (b and e > 0) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="gate")
    ap.add_argument("--every", type=int, default=3)
    a = ap.parse_args()
    ser = json.loads((ROOT/"data"/"ppi.json").read_text(encoding="utf-8"))
    d = ROOT/"data"/"panel"/a.panel
    panel = {}
    for f in sorted(d.glob("*_h250.json")):
        r = json.loads(f.read_text(encoding="utf-8"))
        if r: panel[r[0]["date"]] = r
    dates = sorted(panel)
    lim5 = (dt.date.fromisoformat(dates[-1]) - dt.timedelta(days=int(365.25*5))).isoformat()

    print("="*78); print("事前登録（第2回）の測定 — 物価によるマージン圧力"); print("="*78)
    print("**docs/08_preregistration_ppi.md の予測と照合する。一度だけ測る。**"); print()

    obs = {p: [] for p in PRED}        # (値, 250日リターン, adv, 5年倍率 or None)
    n_row = n_made = 0
    for T in [t for i, t in enumerate(dates) if i % a.every == 0]:
        for r in panel[T]:
            n_row += 1
            v = PP.compute(ser, r.get("sector"), T)
            if all(x.value is None for x in v.values()): continue
            fwd = r.get("fwd")
            if fwd is None: continue
            m5 = None
            if T <= lim5:
                s = PR.load([r["ticker"]]).get(r["ticker"])
                if s: m5 = fwd_mult(BR.adjust(s.bars), T, 5.0)
            n_made += 1
            for p, x in v.items():
                if x.value is not None:
                    obs[p].append((x.value, fwd, r.get("adv_jpy") or 0.0, m5))
    print("パネル %d 行 / 物価が作れた %d 行（**%.0f%%**）"
          % (n_row, n_made, 100.0*n_made/max(1, n_row)))
    print()
    print("%-5s %8s %9s %9s %9s %10s %s"
          % ("ID","観測","上位20%","下位20%","**差**","10倍+差","小型のみの差"))
    print("-"*92)
    res = {}
    for p in ("Y01","Y02","Y03","Y04"):
        v = obs[p]
        if len(v) < 500:
            print("%-5s %8d （観測が足りない）" % (p, len(v))); continue
        v.sort(key=lambda x: -x[0]); k = max(1, len(v)//5)
        top, bot = v[:k], v[-k:]
        diff = st.fmean([x[1] for x in top]) - st.fmean([x[1] for x in bot])
        t5 = [x[3] for x in top if x[3] is not None]
        b5 = [x[3] for x in bot if x[3] is not None]
        d10 = ((100*sum(1 for m in t5 if m>=10)/len(t5)) - (100*sum(1 for m in b5 if m>=10)/len(b5))) if (t5 and b5) else None
        # 小型のみ（adv の下位半分）
        med = st.median([x[2] for x in v])
        sm = [x for x in v if x[2] <= med]
        sm.sort(key=lambda x: -x[0]); ks = max(1, len(sm)//5)
        dsm = st.fmean([x[1] for x in sm[:ks]]) - st.fmean([x[1] for x in sm[-ks:]]) if len(sm) >= 100 else None
        res[p] = {"diff": diff, "d10": d10, "dsm": dsm}
        print("%-5s %8d %+8.2f%% %+8.2f%% %+8.2f%% %s %s"
              % (p, len(v), 100*st.fmean([x[1] for x in top]),
                 100*st.fmean([x[1] for x in bot]), 100*diff,
                 ("%+9.2fpp" % d10) if d10 is not None else "        —",
                 ("%+9.2f%%" % (100*dsm)) if dsm is not None else "        —"))
    print(); print("-"*92); print("**予測との照合**"); print("-"*92)
    for p in ("Y01","Y02","Y03","Y04"):
        if p not in res: continue
        pr = PRED[p]; r = res[p]
        def j(pred, got, thr):
            if got is None: return "測れない"
            if pred == 0: return "**予測どおり効かず**" if abs(got) < thr else "**予測は「効かない」だったが動いた**"
            return "**予測どおり**" if (got > thr if pred > 0 else got < -thr) else "**予測と逆または無**"
        print("  %-5s 平均: %-32s 裾: %s" % (p, j(pr["mean"], r["diff"], 0.01), j(pr["tail"], r["d10"], 0.5)))
    if "Y03" in res and "Y04" in res:
        print()
        print("  **Y04 が Y03 より強いか**（予測は「弱い」）: Y03 %+.2f%% / Y04 %+.2f%% → %s"
              % (100*res["Y03"]["diff"], 100*res["Y04"]["diff"],
                 "**予測どおり Y03 の方が強い**" if abs(res["Y03"]["diff"]) >= abs(res["Y04"]["diff"])
                 else "**驚き: Y04 の方が強い**"))
    if "Y03" in res and res["Y03"]["dsm"] is not None:
        print("  **小型でより強いか**（予測は「そう」）: 全体 %+.2f%% / 小型 %+.2f%% → %s"
              % (100*res["Y03"]["diff"], 100*res["Y03"]["dsm"],
                 "**予測どおり**" if res["Y03"]["dsm"] > res["Y03"]["diff"] else "**予測と逆**"))
    print()
    print("  " + "!"*60)
    print("  **Y03 は業種内で一定である**（事前登録 §5 で予想した通り）。")
    print("  業種内正規化を通すと z=0 になって消えるので、**業種を跨いだ比較**である。")
    print("  **生存者バイアスは消えていない。** 観測は重なっており独立ではない。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
