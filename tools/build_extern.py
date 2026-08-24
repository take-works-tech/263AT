#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
外部データ（論文・臨床試験）を**銘柄ごとに1回だけ**取ってキャッシュする。

なぜ「1回だけ」が重要か
------------------------
パネルは 173日付 × 数千銘柄ある。日付ごとに API を叩けば数十万回になり、
**どの API でも許されない。**

**日付つきの生データを1回取って保存し、時点での集計はローカルで行う。**

    OpenAlex     `group_by=publication_year` で**年次系列が1回で取れる**
    ClinicalTrials  登録日つきの一覧を取る（ページングのみ）

**保存するのは「その日までの累計」ではなく、日付つきの生データ。**
累計を保存すると、基準日を変えたときに取り直しになる。
**生データを持っていれば、どの基準日でも後から作れる。**

PIT の扱い
----------
保存するのは `publication_year` と `studyFirstSubmitDate` だけ。
**成否・引用数・現在の状態は保存しない**（`extern.PIT_UNSAFE`）。
**保存しなければ、後から誤って使うこともない。**

使い方
    .venv/Scripts/python.exe tools/build_extern.py --from-panel
    .venv/Scripts/python.exe tools/build_extern.py --tickers NVDA MRNA
    .venv/Scripts/python.exe tools/build_extern.py --from-panel --limit 200
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import entity as EN           # noqa: E402
import extern as EX           # noqa: E402
import listing as LS          # noqa: E402

OUT = ROOT / "data" / "extern"


def cache_path(tk: str) -> pathlib.Path:
    safe = tk.replace("/", "_").replace("\\", "_")
    return OUT / (safe + ".json")


def fetch_one(tk: str, legal: str, sleep: float = 0.15) -> dict:
    """1銘柄ぶん取る。**失敗した源は None にして、残りは残す。**

    片方が落ちたときに全部捨てると、**再実行のたびに全部取り直し**になる。
    """
    rec = {"ticker": tk, "legal_name": legal, "rors": [], "papers_by_year": {},
           "trials": [], "errors": []}

    # --- 論文（機関IDに解決してから、年次系列を1回で） ---------------------
    try:
        r = EN.resolve_openalex(legal)
        rec["rors"] = r["rors"]
        rec["matched"] = [c["name"] for c in r["matched"]]
        if r["rors"]:
            q = urllib.parse.urlencode({
                "filter": "institutions.ror:" + "|".join(r["rors"]),
                "group_by": "publication_year", "per-page": "200",
                "mailto": EN.MAILTO})
            d = EN._get("https://api.openalex.org/works?" + q)
            rec["papers_by_year"] = {
                x["key"]: x["count"] for x in (d.get("group_by") or [])
                if str(x.get("key", "")).isdigit()}
    except Exception as e:
        rec["errors"].append("openalex: %s" % str(e)[:90])
    time.sleep(sleep)

    # --- 臨床試験（**登録日と相だけ**。成否は取らない） --------------------
    try:
        # **正規化した社名で引く。**
        # SEC のマスタは "VERTEX PHARMACEUTICALS INC / MA" のような形で、
        # そのまま ClinicalTrials に渡すと **0件になる**（実際にそうなった）。
        sponsor = EN.normalize(legal) or legal
        t = EX.trials_asof(sponsor, "2100-01-01", page_size=200, max_pages=6)
        rec["trials_sponsor"] = sponsor
        rec["trials"] = [{"phase": k, "n": v} for k, v in
                         sorted(t["by_phase"].items())]
        rec["trials_total"] = t["total"]
        # 年次にしたいので、もう一度だけ日付つきで取る必要がある場合に備え
        rec["trials_latest"] = t.get("latest_submit")
    except Exception as e:
        rec["errors"].append("trials: %s" % str(e)[:90])
    time.sleep(sleep)
    return rec


def papers_asof(rec: dict, t: str) -> int | None:
    """**その日までの累計論文数。** 年次系列から作る。

    年単位なので、**その年が終わっていなければその年は含めない。**
    含めると、12月の論文を1月に知っていたことになる。

    ただし `t` が年末（12-31）なら、その年は終わっているので含める。
    **一律に除くと、常に最大1年ぶん情報を捨てることになる。**
    """
    by = rec.get("papers_by_year") or {}
    if not by:
        return None
    y = int(t[:4])
    done = y if t[5:] >= "12-31" else y - 1
    return sum(v for k, v in by.items() if int(k) <= done)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--from-panel", action="store_true")
    ap.add_argument("--panel", default="gate")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=0.15)
    ap.add_argument("--refresh", action="store_true")
    a = ap.parse_args()

    if a.from_panel:
        seen = set()
        d = ROOT / "data" / "panel" / a.panel
        for f in sorted(d.glob("*_h250.json")):
            for r in json.loads(f.read_text(encoding="utf-8")):
                seen.add(r["ticker"])
        ts = sorted(seen)
    else:
        ts = a.tickers or []
    if not ts:
        print("対象がない（--from-panel か --tickers）")
        return 1

    by = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    OUT.mkdir(parents=True, exist_ok=True)
    todo = [t for t in ts if a.refresh or not cache_path(t).exists()]
    if a.limit:
        todo = todo[: a.limit]

    print("パネルの銘柄 %d / **取得する %d**（残りはキャッシュ済み）"
          % (len(ts), len(todo)))
    ok = err = 0
    for i, tk in enumerate(todo):
        m = by.get(tk)
        legal = getattr(m, "name", None) if m else None
        if not legal:
            legal = tk
        try:
            rec = fetch_one(tk, legal, a.sleep)
        except Exception as e:
            print("    NG %s: %s" % (tk, str(e)[:70]))
            err += 1
            continue
        cache_path(tk).write_text(json.dumps(rec, ensure_ascii=False),
                                  encoding="utf-8")
        ok += 1
        if rec["errors"]:
            err += 1
        if (i + 1) % 100 == 0:
            print("  %d/%d（成功 %d / 一部失敗 %d）" % (i + 1, len(todo), ok,
                                                        err))
    print("-" * 72)
    print("**取得 %d 銘柄**（一部失敗 %d）" % (ok, err))

    # まとめ
    have = sorted(OUT.glob("*.json"))
    n_pap = n_tri = 0
    for f in have:
        r = json.loads(f.read_text(encoding="utf-8"))
        if r.get("papers_by_year"):
            n_pap += 1
        if r.get("trials_total"):
            n_tri += 1
    print("  キャッシュ %d 銘柄 / 論文あり **%d** / 臨床試験あり **%d**"
          % (len(have), n_pap, n_tri))
    if have:
        print("  → **論文が取れたのは %.0f%%。**"
              " 取れない銘柄は研究発表をしていないか、照合できていない。"
              % (100.0 * n_pap / len(have)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
