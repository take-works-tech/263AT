#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
BLS の生産者物価を取ってキャッシュする。**登録不要。**

無登録の上限は **1回25系列・10年、1日25回**。
必要なのは 33系列 × 15年なので、**系列を分け、10年ごとに分けて**取る。

保存するのは `{系列: {"YYYY-MM": 指数}}`。
**指数そのものを保存し、変化率は読むときに作る。**
変化率を保存すると、窓を変えたときに取り直しになる。

    .venv/Scripts/python.exe tools/build_ppi.py
"""
from __future__ import annotations
import json, pathlib, sys, time, urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
import ppi as PP

OUT = ROOT / "data" / "ppi.json"
API = "https://api.bls.gov/publicAPI/v2/timeseries/data/"
PERIOD = {"M%02d" % i: "%02d" % i for i in range(1, 13)}


def fetch(ids, y0, y1):
    body = json.dumps({"seriesid": ids, "startyear": str(y0),
                       "endyear": str(y1)}).encode()
    req = urllib.request.Request(API, data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent": "263AT/1.0 (tzero30208@gmail.com)"})
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read())


def main() -> int:
    ids = PP.needed_series()
    print("必要な系列 %d 本（品目 %d / 産出 %d）"
          % (len(ids), sum(1 for x in ids if x.startswith("WPU")),
             sum(1 for x in ids if x.startswith("PCU"))))
    out = {}
    if OUT.exists():
        out = json.loads(OUT.read_text(encoding="utf-8"))
        print("既存のキャッシュ %d 系列" % len(out))
    calls = 0
    # **10年ごと・20系列ずつ。** 上限に触れないように分ける
    for y0, y1 in ((2008, 2017), (2017, 2026)):
        for k in range(0, len(ids), 20):
            chunk = ids[k:k+20]
            try:
                d = fetch(chunk, y0, y1); calls += 1
            except Exception as e:
                print("  NG %d-%d [%d]: %s" % (y0, y1, k, str(e)[:70]))
                continue
            if d.get("status") != "REQUEST_SUCCEEDED":
                print("  **拒否** %s" % str(d.get("message"))[:120])
                continue
            for s in d["Results"]["series"]:
                sid = s["seriesID"]
                out.setdefault(sid, {})
                for row in s.get("data") or []:
                    p = row.get("period", "")
                    if p not in PERIOD:      # M13（年平均）は捨てる
                        continue
                    out[sid]["%s-%s" % (row["year"], PERIOD[p])] = \
                        float(row["value"])
            print("  %d-%d [%2d..%2d] 取得 %d 系列（呼び出し %d 回目）"
                  % (y0, y1, k, min(k+20, len(ids)),
                     len(d["Results"]["series"]), calls))
            time.sleep(1.0)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out), encoding="utf-8")
    have = {k: len(v) for k, v in out.items()}
    miss = [x for x in ids if not out.get(x)]
    print("-" * 70)
    print("**%d 系列を保存**（月数の中央値 %d）"
          % (len(out), sorted(have.values())[len(have)//2] if have else 0))
    if miss:
        print("  **取れなかった系列 %d: %s**" % (len(miss), miss[:8]))
        print("  → その業種のパラメータは作られない（無理に埋めない）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
