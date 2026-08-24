#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
業種レベルのパラメータが、**業種ダミー以上のものを測っているか**を確かめる。

業種を跨いで正規化すると、**必然的に「良い業種にいるか」を測る。**
それ自体は情報だが、L01 の採用根拠（Hou 2007 の業種内リードラグ =
**大型株が先に動き、小型株が後から追う**）とは別物である。

分離できなければ、L01 は「業種モメンタムに別の名前を付けたもの」であり、
**再現 t=9.93 とは違うものを測っていることになる。**

分け方
------
リードラグが本物なら、**同じ業種の中でも、小型株ほど強く効くはず**である
（大型株自身は「自分の直近リターン」を見ているだけなので効かない）。

  (a) 全体で L01 の上位/下位を比べる         … 業種ダミーを含む
  (b) **各業種の中で**、大型株と小型株に分けて比べる … リードラグの検証
  (c) 業種の直近リターンそのもの（＝業種ダミー）と比べる
"""
from __future__ import annotations
import argparse, json, pathlib, statistics as st, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass


def spread(rows, key, q=0.2):
    v = [r for r in rows if r.get(key) is not None and r.get("fwd") is not None]
    if len(v) < 200: return None, len(v)
    v.sort(key=lambda r: -r[key])
    k = max(1, int(len(v)*q))
    return (st.fmean([r["fwd"] for r in v[:k]])
            - st.fmean([r["fwd"] for r in v[-k:]])), len(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="gate")
    a = ap.parse_args()
    d = ROOT/"data"/"panel"/a.panel
    rows = []
    for f in sorted(d.glob("*_h250.json")):
        for r in json.loads(f.read_text(encoding="utf-8")):
            if r.get("fwd") is None: continue
            rows.append({"date": r["date"], "sector": r.get("sector"),
                         "adv": r.get("adv_jpy") or 0.0, "fwd": r["fwd"],
                         "L01": r["z"].get("L01"), "G04": r["z"].get("G04")})
    print("="*78); print("業種レベルのパラメータの検証（L01）"); print("="*78)
    print("観測 %d"%len(rows)); print()

    s, n = spread(rows, "L01")
    print("(a) **全体での L01 の上位-下位** : %s（%d 観測）"
          % (("%+.2f%%" % (100*s)) if s is not None else "測れない", n))
    print("    ← **業種ダミーを含む。** 「良い業種にいるか」がそのまま入る")
    print()

    # (b) 各業種の中で、大型/小型に分ける
    print("(b) **業種の中で、大型株と小型株に分ける**")
    print("    リードラグが本物なら、**小型株ほど強く効く**はず")
    big, small = [], []
    by = {}
    for r in rows:
        by.setdefault((r["date"], r["sector"]), []).append(r)
    for k, v in by.items():
        if len(v) < 20: continue
        v.sort(key=lambda x: -x["adv"])
        h = len(v)//2
        big += v[:h]; small += v[h:]
    for nm, grp in (("大型（売買代金 上位半分）", big), ("**小型（下位半分）**", small)):
        s2, n2 = spread(grp, "L01")
        print("    %-26s %s（%d 観測）"
              % (nm, ("%+.2f%%" % (100*s2)) if s2 is not None else "測れない", n2))

    # (c) 業種ダミーそのもの（同業の平均 fwd）と比べる
    print()
    print("(c) **業種の直近リターン（G04 の業種平均）と比べる**")
    sec_g04 = {}
    for k, v in by.items():
        g = [x["G04"] for x in v if x["G04"] is not None]
        if g: sec_g04[k] = st.fmean(g)
    for r in rows:
        r["SECMOM"] = sec_g04.get((r["date"], r["sector"]))
    s3, n3 = spread(rows, "SECMOM")
    print("    業種モメンタム（G04 の業種平均）: %s（%d 観測）"
          % (("%+.2f%%" % (100*s3)) if s3 is not None else "測れない", n3))
    if s is not None and s3 is not None:
        print()
        if abs(s3) >= abs(s) * 0.8:
            print("    → **業種モメンタムだけでほぼ同じ差が出る。**")
            print("      **L01 は業種ダミーに別の名前を付けたものである疑いが強い。**")
        else:
            print("    → **業種モメンタムでは説明しきれない。**")
            print("      L01 は業種を超えた情報を持っている。")
    print()
    print("  " + "!"*60)
    print("  観測は重なっており独立ではない。生存者バイアスも消えていない。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
