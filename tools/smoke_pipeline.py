#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全層を通した煙テスト — **ユニバースゲートまで含めた本物の断面。**

Phase 1 の煙テスト（`tools/smoke_phase1.py`）は価格が無かったため
J01（流動性）とサイズのゲートを通せず、**z の上下位がシェル企業で埋まった。**
ここではその問題を解消した状態で断面を作る。

    listing（SEC マスタ）+ prices（yfinance）+ facts（DERA）+ periods
      → universe（§6 のゲート）→ normalize（§4）

**確認したいのは「ゲートを通すと分布がどう変わるか」である。**

使い方
    .venv/Scripts/python.exe tools/smoke_pipeline.py
    .venv/Scripts/python.exe tools/smoke_pipeline.py --rho 2.0
"""
from __future__ import annotations

import argparse
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

import facts as FA          # noqa: E402
import listing as LS        # noqa: E402
import pipeline as PL       # noqa: E402
import prices as PR         # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default="2025-03-31")
    ap.add_argument("--rho", type=float, default=1.0)
    ap.add_argument("--fx", type=float, default=150.0,
                    help="**暫定の円換算レート。本来は available_at 時点のスポット**")
    args = ap.parse_args()

    print("=" * 78)
    print("全層を通した煙テスト（asof=%s, rho=%.1f）" % (args.asof, args.rho))
    print("=" * 78)

    # --- 銘柄マスタと価格 ----------------------------------------------------
    cached = sorted(p.stem for p in (ROOT / "data" / "prices").glob("*.json"))
    if not cached:
        print("価格キャッシュが無い。`src/prices.py --fetch ...` で取得する。")
        return 1
    series = PR.load(cached)
    print("価格キャッシュ: %d 銘柄" % len(series))

    by_ticker = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    import pandas as pd
    subs = pd.concat([pd.read_parquet(p)
                      for p in sorted((ROOT / "data" / "pit" / "subs").glob("*.parquet"))])
    sic_by_cik = {}
    shares_note = 0
    for r in subs[["cik", "sic"]].dropna().itertuples(index=False):
        sic_by_cik[int(r.cik)] = r.sic

    # --- ファクト -------------------------------------------------------------
    asof = FA.AsOf(FA.load())
    print("PIT ファクトを読み込んだ")

    # --- 候補を組み立てる -----------------------------------------------------
    cands = []
    no_price = no_master = 0
    for t, s in series.items():
        m = by_ticker.get(t)
        if m is None or not m.cik:
            no_master += 1
            continue
        snap = PR.snapshot(s, args.asof)
        if snap is None:
            no_price += 1
            continue
        cik = int(m.cik)
        # **時価総額は DERA の SHARES × 価格。**
        # SHARES も PIT で引く（発行済株式数は増資・自社株買いで変わる）
        sh = asof.latest_period(cik, "SHARES", 0, args.asof, max_lag_days=400)
        mcap = (sh.value * snap["close"] * args.fx) if sh else None
        if sh is None:
            shares_note += 1
        cands.append({
            "ticker": t, "cik": cik, "market": "US", "sic": sic_by_cik.get(cik),
            "months_listed": snap["n_bars"] / 21.0,     # 概算（バー数から）
            "adv_jpy": (snap["adv20"] or 0.0) * args.fx,
            "zero_volume_days": snap["zero_vol_60"],
            "mcap_jpy": mcap,
            "audit_clean": True,   # **本来は SEC から。未取得なら None が正しい**
        })
    print("候補 %d 銘柄（マスタ無し %d / 価格不足 %d / SHARES 無し %d）"
          % (len(cands), no_master, no_price, shares_note))
    print()

    # --- 断面を作る -----------------------------------------------------------
    # **診断用に、ユニバース外の値も計算した版を別に作る。**
    # これが無いと「ゲートを通すと分布がどう変わるか」を測れない
    rows_all = PL.build(args.asof, cands, asof, "NI", "TA", rho=args.rho,
                        compute_excluded=True)
    rows = PL.build(args.asof, cands, asof, "NI", "TA", rho=args.rho)
    print(PL.summary(rows))

    scored = [r for r in rows if not r.missing]
    if not scored:
        print()
        print("**スコアが1件も作れなかった。**")
        print("N<30 でランク化できないか、期間が揃っていない可能性。")
        return 0

    # --- **ゲートの効果を測る** -----------------------------------------------
    print()
    print("-" * 78)
    print("**ゲートを通すと分布がどう変わるか**")
    print("-" * 78)
    allraw = [r.raw for r in rows_all if r.raw is not None]
    inraw = [r.raw for r in scored]

    def stat(v, label):
        if not v:
            print("  %-22s なし" % label)
            return
        v2 = sorted(v)
        ex = sum(1 for x in v2 if abs(x) > 1.0)
        print("  %-22s n=%4d  中央値 %+.3f  |ROA|>1 が %d 件（%.1f%%）"
              % (label, len(v2), v2[len(v2) // 2], ex, 100 * ex / len(v2)))

    stat(allraw, "ゲート前")
    stat(inraw, "**ゲート後**")
    print()
    ex_all = sum(1 for x in allraw if abs(x) > 1.0)
    ex_in = sum(1 for x in inraw if abs(x) > 1.0)
    if allraw and inraw:
        print()
        print("  → **ゲートが |ROA|>1 の銘柄を %d 件から %d 件に減らした**"
              % (ex_all, ex_in))
        print("     （割合では %.1f%% → %.1f%%）"
              % (100 * ex_all / len(allraw), 100 * ex_in / len(inraw)))

    scored.sort(key=lambda r: -r.z)
    print()
    print("  z 上位5:")
    for r in scored[:5]:
        print("    %-8s %-6s ROA=%+.4f  z=%+.2f  %s"
              % (r.ticker, r.sector, r.raw, r.z, r.note))
    print("  z 下位5:")
    for r in scored[-5:]:
        print("    %-8s %-6s ROA=%+.4f  z=%+.2f  %s"
              % (r.ticker, r.sector, r.raw, r.z, r.note))

    # --- rho を動かす ---------------------------------------------------------
    print()
    print("-" * 78)
    print("rho（§1.6 のリスク許容度ダイヤル）を動かすとどうなるか")
    print("-" * 78)
    for rho in (0.5, 1.0, 2.0):
        rs = PL.build(args.asof, cands, asof, "NI", "TA", rho=rho)
        n_in = sum(1 for r in rs if r.in_universe)
        n_sc = sum(1 for r in rs if not r.missing)
        print("  rho=%.1f  ユニバース %4d 銘柄、スコア %4d 件" % (rho, n_in, n_sc))
    print("  → **rho を上げると小型・低流動性まで踏み込む。**")
    print("     263AT は「1/10 が全部を賄う」設計なので、")
    print("     **rho を下げすぎると狙う銘柄がユニバースに入らない**（§1.6）")

    print()
    print("=" * 78)
    print("まだ正しくないところ（**明記する**）")
    print("=" * 78)
    print("  - **監査意見（E22）と継続企業の前提（D13）を True で仮置きしている。**")
    print("    本来は SEC/EDINET から取り、未取得なら None にして落とすのが正しい。")
    print("    universe.py は None を『適正』に丸めない設計になっているので、")
    print("    **データを繋げばそのまま厳しくなる**")
    print("  - **上場後経過月数をバー数から概算している**（本来は上場日）")
    print("  - **円換算レートを固定値にしている**（本来は available_at 時点のスポット）")
    print("  - **SIC を as-of で持っていない**（spec §9 の落とし穴14）")
    print("  - 日本株を含んでいない（J-Quants / EDINET が未登録）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
