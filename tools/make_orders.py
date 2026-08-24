#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
記録した生成から、**実際の発注内容**を作る。

なぜ要るか
----------
`record_generation.py` は上位N銘柄と順位を残すが、
**「いくら買うか」は決めない。**

実運用に移すかどうかを判断するには、
**「実際に何をいくら買うことになるのか」**が見えている必要がある。
そして見えた瞬間に、**机上では気づかない制約が出る。**

  - 300万円を30銘柄に分けると1銘柄10万円。**手数料の比率が上がる**
  - 流動性の制約で入りきらない銘柄がある
  - 1株の価格が高い銘柄は、端数が大きな比率になる

**この道具は発注しない。** 発注内容を表示するだけである。

使い方
    .venv/Scripts/python.exe tools/make_orders.py
    .venv/Scripts/python.exe tools/make_orders.py --capital 3000000 --fx 150
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

import bars as BR             # noqa: E402
import prices as PR           # noqa: E402
import security_type as ST    # noqa: E402
import sizing as SZ           # noqa: E402

LOG = ROOT / "data" / "generations.jsonl"
TYPES = ROOT / "data" / "security_types.json"
TICKERS = ROOT / "data" / "listing" / "company_tickers.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--capital", type=float, default=3_000_000.0,
                    help="個別株に配分する円")
    ap.add_argument("--fx", type=float, default=150.0, help="USD/JPY")
    ap.add_argument("--fee-bps", type=float, default=25.0,
                    help="片道の手数料+スプレッド（bps）")
    a = ap.parse_args()

    if not LOG.exists():
        print("**記録が無い。** tools/record_generation.py を先に実行する")
        return 1
    rows = [json.loads(x) for x in
            LOG.read_text(encoding="utf-8").splitlines() if x.strip()]
    rec = rows[-1]
    T = rec["asof"]

    print("=" * 78)
    print("発注内容（**この道具は発注しない。表示するだけ**）")
    print("=" * 78)
    print("  基準日 %s / コード %s / 元本 %s円 / USDJPY %.1f"
          % (T, rec["git_commit"], "{:,.0f}".format(a.capital), a.fx))
    print()

    # 価格とボラを取る（**基準日までの情報だけ**）
    cands = []
    miss = []
    for p in rec["picks"]:
        tk = p["ticker"]
        s = PR.load([tk]).get(tk)
        if not s:
            miss.append(tk)
            continue
        rows_b = [x for x in BR.adjust(s.bars) if x["date"] <= T]
        if len(rows_b) < 60:
            miss.append(tk)
            continue
        px_adj = rows_b[-1]["close"]
        px = px_adj * PR.unadjust_factor(s, T)      # **その時点の実際の株価**
        adv = sum(x["turnover"] for x in rows_b[-20:]) / 20.0 * a.fx
        lr = [x for x in BR.log_return(rows_b[-61:]) if x is not None]
        vol = ((sum(x * x for x in lr) / len(lr)) ** 0.5 * (252 ** 0.5)
               if len(lr) >= 20 else None)
        cands.append(SZ.Candidate(ticker=tk, sector=p["sector"],
                                  score=p["score"], volatility=vol,
                                  adv_jpy=adv))
        p["_px_usd"] = px
        p["_adv_jpy"] = adv
        p["_vol"] = vol

    if miss:
        # **黙って落とさない。** 落ちた銘柄があれば配分が変わる
        print("  **価格が取れなかった: %s**" % ", ".join(miss))
        print()

    w, notes = SZ.target_positions(cands, a.capital, SZ.RiskLimits())
    if not w:
        print("  **配分が作れなかった。**")
        for n in notes:
            print("    %s" % n)
        return 1

    by = {p["ticker"]: p for p in rec["picks"]}
    print("  %-8s %-10s %9s %8s %10s %8s %s"
          % ("銘柄", "業種", "株価$", "比率", "金額(円)", "株数", "備考"))
    print("  " + "-" * 74)
    total = fee = 0.0
    lines = []
    for tk, ww in sorted(w.items(), key=lambda x: -x[1]):
        p = by.get(tk, {})
        px = p.get("_px_usd")
        if not px:
            continue
        amt = a.capital * ww
        shares = int(amt / (px * a.fx))
        real = shares * px * a.fx
        total += real
        fee += real * a.fee_bps / 10000.0
        note = ""
        if shares == 0:
            note = "**1株も買えない**"
        elif real < amt * 0.9:
            note = "端数で %.0f%% 減" % (100 * (1 - real / amt))
        lines.append((tk, p.get("sector") or "", px, ww, real, shares, note))
    for tk, sec, px, ww, real, sh, note in lines:
        print("  %-8s %-10s %9.2f %7.1f%% %10s %8d %s"
              % (tk, sec[:10], px, 100 * ww, "{:,.0f}".format(real), sh, note))

    print("  " + "-" * 74)
    print("  **投資額 %s円（%.0f%%）/ 現金 %s円**"
          % ("{:,.0f}".format(total), 100 * total / a.capital,
             "{:,.0f}".format(a.capital - total)))
    print("  **片道の手数料+スプレッド %s円（%.2f%%）**"
          % ("{:,.0f}".format(fee), 100 * fee / a.capital))
    print("  銘柄数 %d / 1銘柄あたり平均 %s円"
          % (len(lines), "{:,.0f}".format(total / max(1, len(lines)))))

    zero = [x for x in lines if x[5] == 0]
    if zero:
        print()
        print("  **1株も買えない銘柄が %d 件ある。**" % len(zero))
        print("  1銘柄あたりの金額が、その株価より小さい。")

    print()
    for n in notes:
        print("  注: %s" % n)

    # **証券種別と発行体の重複をここでも点検する。**
    # ゲートを通っているはずだが、**通っている「はず」で発注しない。**
    # 2026-05-31 の記録は、この点検を持たずに作られている。
    kinds = ST.load(TYPES)
    t2c = {}
    if TICKERS.exists():
        t2c = {v["ticker"]: int(v["cik_str"]) for v in
               json.loads(TICKERS.read_text(encoding="utf-8")).values()}
    bad = [(tk, kinds[tk].value) for tk, *_ in
           [(x[0],) for x in lines] if kinds.get(tk)
           and ST.is_excluded(kinds[tk])]
    dup: dict[int, list[str]] = {}
    for tk, *_ in [(x[0],) for x in lines]:
        c = t2c.get(tk)
        if c is not None:
            dup.setdefault(c, []).append(tk)
    dup = {c: v for c, v in dup.items() if len(v) > 1}
    print()
    if bad:
        print("  **普通株でないものが %d 件ある:**" % len(bad))
        for tk, why in bad:
            print("    %-8s %s" % (tk, why))
    if dup:
        print("  **同一発行体が %d 組ある:**" % len(dup))
        for c, v in dup.items():
            print("    cik %-9d %s" % (c, ", ".join(sorted(v))))
    if not bad and not dup:
        print("  点検: **普通株のみ / 発行体の重複なし**")

    print()
    print("  " + "!" * 60)
    print("  **この表は発注ではない。** 判断のための材料である。")
    print("  過去13.5年の測定では年率 +21.8%（SPY +13.7%）だが、")
    print("  **これを「指数に勝つ証拠」と受け取ってはいけない。**")
    print("  生存者バイアス（価格データに上場廃止銘柄が1本も無い）と")
    print("  カタログ自体のルックアヘッドは、**何も解決していない。**")
    print("  **交絡のない証拠は前向き記録だけで、初回評価は 2027-02-05 である。**")
    lo = [x for x in lines if x[2] < 2.0]
    if lo:
        w = sum(x[3] for x in lo)
        print()
        print("  参考: **株価 $2 未満が %d 銘柄（%.0f%%）ある。**"
              % (len(lo), 100 * w))
        print("  ゲートは $1 だが、実測では **$1-2 帯は5年で中央値 0.38倍、"
              "54% が半減する**（docs/02 の株価帯の表）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
