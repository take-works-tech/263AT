#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**継続企業の前提に関する疑義**の索引を作る（D13 ゲート）。

`data/going_concern.json` に `{cik: [提出日, ...]}` を保存する。
**保存するのは提出日つきの生データ。** 判定は読むときに行う
（`stale_months` を変えても取り直しにならない）。

    .venv/Scripts/python.exe tools/build_gc.py
"""
from __future__ import annotations
import argparse, json, pathlib, sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "src"))
if hasattr(sys.stdout, "reconfigure"):
    try: sys.stdout.reconfigure(encoding="utf-8")
    except Exception: pass
import edgar_fts as FT

OUT = ROOT / "data" / "going_concern.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-year", type=int, default=2009)
    ap.add_argument("--to-year", type=int, default=2026)
    ap.add_argument("--sleep", type=float, default=0.25)
    a = ap.parse_args()
    idx: dict[str, list[str]] = {}
    if OUT.exists():
        idx = json.loads(OUT.read_text(encoding="utf-8"))
        print("既存の索引 %d 社" % len(idx))
    years = list(range(a.from_year, a.to_year + 1))
    print("**継続企業の前提の索引を作る**（%d〜%d年）" % (years[0], years[-1]))
    for y in years:
        try:
            rs = FT.search_year(y, sleep=a.sleep)
        except FT.FtsError as e:
            # **失敗を0件と混同しない。** 混同すると疑義のある企業が健全に化ける
            print("  %d年 **失敗**: %s" % (y, str(e)[:70]))
            continue
        n = 0
        for r in rs:
            if r.get("file_date"):
                idx.setdefault(str(r["cik"]), []).append(r["file_date"])
                n += 1
        print("  %d年: 書類 %4d 件" % (y, n))
    for k in idx:
        idx[k] = sorted(set(idx[k]))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(idx), encoding="utf-8")
    tot = sum(len(v) for v in idx.values())
    print("-" * 66)
    print("**%d 社 / 延べ %d 件を保存**" % (len(idx), tot))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
