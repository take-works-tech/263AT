#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**証券種別の索引を作る。** `data/security_types.json`

出所
----
Nasdaq Trader の銘柄ディレクトリ。**無料・登録不要。**

    https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt
    https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt

`Security Name` に取引所が付けた正式名称が入っている。

    ATLC |Atlanticus Holdings Corporation - **Common Stock**
    ATLCL|Atlanticus Holdings Corporation - **6.125% Senior Notes due 2026**

**取得したファイルはそのまま `data/listing/` に保存する。**
分類が後から変わったとき、入力が変わったのか規則が変わったのか
分からなくなるのを防ぐ。

**一覧に無い銘柄を「除外」にしない**
------------------------------------
一覧は**現在**上場しているものだけを含む。
上場廃止銘柄は載っていない。だから

    載っていない → `UNKNOWN` → **除外しない**（ティッカーの形と CIK で判定する）

これを逆にすると、**生存者バイアスが今より悪化する。**

使い方
    .venv/Scripts/python.exe tools/build_sectypes.py            # 取得して作る
    .venv/Scripts/python.exe tools/build_sectypes.py --offline  # 保存済みから作る
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import security_type as ST     # noqa: E402

UA = {"User-Agent": "263AT/1.0 (tzero30208@gmail.com)"}
BASE = "https://www.nasdaqtrader.com/dynamic/SymDir/"
FILES = ("nasdaqlisted.txt", "otherlisted.txt")

LISTING = ROOT / "data" / "listing"
OUT = ROOT / "data" / "security_types.json"
TICKERS = LISTING / "company_tickers.json"


def fetch(name: str, timeout: int = 60) -> str:
    req = urllib.request.Request(BASE + name, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def parse(text: str) -> dict[str, str]:
    """`{シンボル: 証券名}`。**末尾の File Creation 行を捨てる。**"""
    out: dict[str, str] = {}
    for ln in text.splitlines()[1:]:
        if ln.startswith("File Creation"):
            continue
        p = ln.split("|")
        if len(p) < 2:
            continue
        sym, nm = p[0].strip(), p[1].strip()
        if sym and nm:
            out.setdefault(sym, nm)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true",
                    help="保存済みのファイルだけを使う")
    a = ap.parse_args()

    LISTING.mkdir(parents=True, exist_ok=True)
    names: dict[str, str] = {}
    for f in FILES:
        p = LISTING / f
        if not a.offline:
            try:
                txt = fetch(f)
                p.write_text(txt, encoding="utf-8")
                print("  取得 %s（%d 行）" % (f, txt.count("\n")))
            except Exception as e:
                # **失敗を保存済みで黙って埋めない。** 何が起きたか出す
                print("  **取得に失敗** %s: %s" % (f, str(e)[:80]))
        if not p.exists():
            print("  **%s が無い。** --offline なら先に取得が要る" % f)
            return 1
        names.update(parse(p.read_text(encoding="utf-8", errors="replace")))
    print("  上場一覧 **%d 銘柄**" % len(names))

    mine = sorted(x.stem for x in (ROOT / "data" / "prices").glob("*.json"))
    if not mine:
        print("  **価格データが無い。**")
        return 1

    kinds: dict[str, str] = {}
    src = collections.Counter()
    cnt = collections.Counter()
    for tk in mine:
        nm = names.get(tk)
        k = ST.classify_name(nm)
        if k is ST.Kind.UNKNOWN:
            k2 = ST.classify_shape(tk)
            src["形" if k2 is not ST.Kind.UNKNOWN else "判定できず"] += 1
            k = k2
        else:
            src["証券名"] += 1
        kinds[tk] = k.name
        cnt[k] += 1

    # CIK ごとの重複を数える（規則3 はパネル生成時に時点ごとに適用する）
    dup = 0
    if TICKERS.exists():
        d = json.loads(TICKERS.read_text(encoding="utf-8"))
        t2c = {v["ticker"]: v["cik_str"] for v in d.values()}
        g = collections.defaultdict(list)
        for tk in mine:
            if tk in t2c:
                g[t2c[tk]].append(tk)
        keep = {c: [t for t in v
                    if not ST.is_excluded(ST.Kind[kinds[t]])] for c, v in g.items()}
        dup = sum(max(0, len(v) - 1) for v in keep.values())

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"source": BASE, "n_listing": len(names), "n_tickers": len(mine),
         "kinds": kinds}, ensure_ascii=False), encoding="utf-8")

    print()
    print("=" * 78)
    print("証券種別の内訳（**COMMON と UNKNOWN 以外はユニバースから外す**）")
    print("=" * 78)
    tot_ex = 0
    for k in ST.Kind:
        n = cnt.get(k, 0)
        if not n:
            continue
        mark = "**除外**" if ST.is_excluded(k) else ""
        if ST.is_excluded(k):
            tot_ex += n
        print("  %-10s %-34s %5d  %s" % (k.name, k.value, n, mark))
    print("  " + "-" * 74)
    print("  **種別で外れる: %d 銘柄（%.1f%%）**"
          % (tot_ex, 100 * tot_ex / len(mine)))
    print("  判定の出所: %s" % dict(src))
    print()
    print("  **規則3（1発行体1銘柄）で さらに最大 %d 銘柄が外れる。**" % dup)
    print("  これは売買代金に依るので、**時点ごとにパネル生成時に判定する。**")
    print()
    print("  → %s" % OUT.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
