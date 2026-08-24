#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**普通株だけのパネルを作る。** `data/panel/gate` → `data/panel/common`

なぜパネルを作り直さずに絞れるか
--------------------------------
証券種別による除外は**行の取捨だけ**で、
パラメータの値も正規化も変えない……**わけではない。**

    **業種内の順位正規化は、その業種に誰がいるかに依存する。**
    優先株を19,962行ぶん抜けば、残った銘柄の z 値は本来わずかに動く。

なので**これは近似である。** 影響の大きさを先に測るための道具であり、
**採用するなら `build_panel.py` から作り直す。**
（差が小さければ作り直す価値がないことも分かる。それも結果である。）

適用する規則（`src/security_type.py` の3つ）
--------------------------------------------
  規則1 証券名 … `data/security_types.json`
  規則2 ティッカーの形
  規則3 **1発行体1銘柄** … その断面の `adv_jpy` が最大のもの

**規則3 は断面ごとに判定する。** 売買代金は時点で変わるので、
「2015年は A が主銘柄、2020年は B」も起こりうる。**それが正しい。**

使い方
    .venv/Scripts/python.exe tools/filter_common.py --horizon 250
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import security_type as ST     # noqa: E402

SRC = ROOT / "data" / "panel"
TYPES = ROOT / "data" / "security_types.json"
TICKERS = ROOT / "data" / "listing" / "company_tickers.json"
SUBS = ROOT / "data" / "pit" / "subs"

#: 規則1の補い。**SIC 6221（商品先物）かつ登録名がファンドらしい**なら ETF。
#:
#: 商品 ETF は取引所の証券名が "ProShares Ultra Silver" のように
#: ファンドとわからない。一方 SEC への登録名は "PROSHARES TRUST II" である。
#:
#: **SIC だけでは足りない。** 6221 には暗号資産関連の事業会社も入っている
#: （AI FINANCIAL CORP, ANTALPHA PLATFORM HOLDING など）。
#: だから **SIC と名前の両方**を要求する。
#: REIT は SIC 6798 なので、この規則には一切触れない。
SIC_FUND = "6221"
FUNDISH = re.compile(r"\b(trust|fund|etf|etn|shares)\b", re.I)


def sic_names() -> tuple[dict[int, str], dict[int, str]]:
    try:
        import pandas as pd
    except ImportError:
        return {}, {}
    sic: dict[int, str] = {}
    nm: dict[int, str] = {}
    for f in sorted(SUBS.glob("*.parquet")):
        try:
            df = pd.read_parquet(f)[["cik", "sic", "name"]]
        except Exception:
            continue
        for r in df.dropna(subset=["cik"]).itertuples(index=False):
            c = int(r.cik)
            if r.sic == r.sic and r.sic is not None:
                sic[c] = str(r.sic)
            if r.name == r.name and r.name is not None:
                nm[c] = str(r.name)
    return sic, nm


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=250)
    ap.add_argument("--src", default="gate")
    ap.add_argument("--dst", default="common")
    a = ap.parse_args()

    if not TYPES.exists():
        print("**証券種別の索引が無い。** tools/build_sectypes.py を先に")
        return 1
    kinds = {k: ST.Kind[v] for k, v in
             json.loads(TYPES.read_text(encoding="utf-8"))["kinds"].items()}
    t2c = {v["ticker"]: int(v["cik_str"]) for v in
           json.loads(TICKERS.read_text(encoding="utf-8")).values()}
    sic, nm = sic_names()

    def kind_of(t: str) -> ST.Kind:
        k = kinds.get(t, ST.Kind.UNKNOWN)
        if k is not ST.Kind.UNKNOWN:
            return k
        c = t2c.get(t)
        if c is not None and sic.get(c) == SIC_FUND \
                and FUNDISH.search(nm.get(c, "")):
            return ST.Kind.FUND
        return ST.Kind.UNKNOWN

    src = SRC / a.src
    dst = SRC / a.dst
    dst.mkdir(parents=True, exist_ok=True)
    files = sorted(src.glob("*_h%d.json" % a.horizon))
    if not files:
        print("**元のパネルが無い**: %s" % src)
        return 1

    tot = kept = 0
    drop = collections.Counter()
    dup_examples: list[str] = []
    for f in files:
        rows = json.loads(f.read_text(encoding="utf-8"))
        tot += len(rows)

        # 規則1・2
        stage1 = []
        for r in rows:
            k = kind_of(r["ticker"])
            if ST.is_excluded(k):
                drop[k.name] += 1
            else:
                stage1.append((r, k))

        # 規則3 — **この断面の売買代金で主銘柄を決める**
        by_cik: dict[int, list[dict]] = collections.defaultdict(list)
        loose = []
        for r, k in stage1:
            c = t2c.get(r["ticker"])
            if c is None:
                loose.append(r)          # CIK 不明。**落とさない**
            else:
                by_cik[c].append({"ticker": r["ticker"], "kind": k,
                                  "adv": r.get("adv_jpy"), "_row": r})
        out = list(loose)
        for c, mem in by_cik.items():
            if len(mem) == 1:
                out.append(mem[0]["_row"])
                continue
            pick = ST.primary_by_issuer(mem)
            if pick is None:
                # **主銘柄を決められない。** 全部落とす（当て推量しない）
                drop["主銘柄不明"] += len(mem)
                continue
            for m in mem:
                if m["ticker"] == pick:
                    out.append(m["_row"])
                else:
                    drop["同一発行体の別銘柄"] += 1
                    if len(dup_examples) < 8:
                        dup_examples.append("%s→%s" % (m["ticker"], pick))
        out.sort(key=lambda r: r["ticker"])
        kept += len(out)
        (dst / f.name).write_text(json.dumps(out, ensure_ascii=False),
                                  encoding="utf-8")

    print("=" * 78)
    print("普通株だけのパネル（**近似。採用するなら build_panel から作り直す**）")
    print("=" * 78)
    print("  %s → %s（%d 断面）" % (a.src, a.dst, len(files)))
    print("  **%d 行 → %d 行（−%d, −%.2f%%）**"
          % (tot, kept, tot - kept, 100 * (tot - kept) / tot))
    print()
    for k, v in drop.most_common():
        print("    %-20s %7d 行 (%.2f%%)" % (k, v, 100 * v / tot))
    if dup_examples:
        print()
        print("  同一発行体で落ちた例: %s" % ", ".join(dup_examples))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
