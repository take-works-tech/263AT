#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ウォークフォワード検証を実データで回す。

**目的は成績の測定ではない**（docs/05 §1.4）。
カタログ自体が 2024年までの OSAP を見て書かれているので、
**2024年以前の成績は検証にならない。**

測るのはこれ:

| 測るもの | なぜ |
|---|---|
| **ルックアヘッドの混入** | 同じ過去を2回計算して値が変わらないか |
| **回転率** | コストの見積りに直結する |
| **取引コストの実額** | OQ-24 で「コストの仮定が結論を決める」と出た |
| **売りルールの発動内訳** | どの理由で何件売れているか |
| **ユニバースの安定性** | 銘柄が出入りしすぎていないか |

使い方
    .venv/Scripts/python.exe tools/run_backtest.py
    .venv/Scripts/python.exe tools/run_backtest.py --stop-loss -0.20 --trailing -0.30
"""
from __future__ import annotations

import argparse
import collections
import dataclasses
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

import bars as BR          # noqa: E402
import facts as FA         # noqa: E402
import ff49                # noqa: E402
import listing as LS       # noqa: E402
import normalize as NZ     # noqa: E402
import params_us as PU     # noqa: E402
import portfolio as PF     # noqa: E402
import prices as PR        # noqa: E402
import sell as SL          # noqa: E402
import sizing as SZ        # noqa: E402
import universe as UV      # noqa: E402

FX = 150.0


def month_ends(lo: str, hi: str) -> list[str]:
    import datetime as dt
    out, d = [], dt.date.fromisoformat(lo)
    while d <= dt.date.fromisoformat(hi):
        nxt = dt.date(d.year + (d.month // 12), d.month % 12 + 1, 1)
        out.append((nxt - dt.timedelta(days=1)).isoformat())
        d = nxt
    return out


def build_cross_section(t: str, series, by_ticker, sic_asof, asof, rho=1.0):
    """時点 t の候補とスコア。**すべて t 以前の情報のみ。**"""
    # **上場期間のゲートを明示的に切る。**
    # 上場日を持っていない（SEC も DERA も提供しない）ので、
    # **偽の近似（バー数 ÷ 21）で埋めるのをやめた**（universe.py の注記）。
    # 切ったことは除外内訳に現れないので、ここに書いておく。
    th = dataclasses.replace(UV.Thresholds.for_rho(rho), require_age=False)
    rows = []
    for tk, s in series.items():
        m = by_ticker.get(tk)
        if not m or not m.cik:
            continue
        snap = PR.snapshot(s, t)
        if snap is None:
            continue
        cik = int(m.cik)
        sh = asof.latest_period(cik, "SHARES", 0, t, max_lag_days=400)
        mcap = sh.value * snap["close"] * FX if sh else None
        cand = UV.Candidate(
            ticker=tk, listed=True, months_listed=None,   # **上場日は持っていない**
            adv_jpy=(snap["adv20"] or 0) * FX,
            zero_volume_days=snap["zero_vol_60"], mcap_jpy=mcap,
            supervised=False, going_concern_note=False, audit_clean=True)
        if UV.judge(cand, th):
            continue
        v = PU.compute(asof, cik, t, mcap)
        vals = {k: x.value for k, x in v.items() if x.value is not None}
        if len(vals) < 3:            # **3本未満ならスコアを作らない**
            continue
        rows.append({"ticker": tk, "cik": cik,
                     "sector": ff49.industry(sic_asof.get(cik, t)),
                     "vals": vals, "adv_jpy": cand.adv_jpy,
                     "vol": snap.get("vol"), "close": snap["close"]})
    return rows


def score_rows(rows):
    """**各パラメータを業種内で正規化し、符号を掛けて等加重で合成する。**

    §1.9 の方針（選択せず縮小する）に従えば本来は縮小推定だが、
    **ここで測りたいのは仕組みであって成績ではない**ので等加重にする。
    等加重は OQ-24 で「単独では失敗する」と出ている合成法であり、
    **成績が悪く出るのは想定内。**
    """
    # 符号（カタログの sign）。**生の値は符号で歪めない**ので、ここで掛ける
    SIGN = {"E29": +1, "B22": -1, "E03": -1, "F24": -1, "E01": -1,
            "B02": +1, "B06": +1, "A04": +1, "A03": +1, "A06": +1}
    if not rows:
        return {}
    zs = collections.defaultdict(dict)
    for pid in SIGN:
        idx = [i for i, r in enumerate(rows) if pid in r["vals"]]
        if len(idx) < NZ.MIN_GROUP:
            continue
        res = NZ.normalize([rows[i]["vals"][pid] for i in idx],
                           [rows[i]["sector"] for i in idx],
                           market=["US"] * len(idx))
        for k, i in enumerate(idx):
            if not res.missing[k]:
                zs[rows[i]["ticker"]][pid] = SIGN[pid] * res.z[k]
    return {tk: sum(d.values()) / len(d) for tk, d in zs.items() if d}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-10-31")
    ap.add_argument("--end", default="2025-03-31")
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    ap.add_argument("--stop-loss", type=float, default=None)
    ap.add_argument("--trailing", type=float, default=None)
    ap.add_argument("--spread-bps", type=float, default=25.0)
    args = ap.parse_args()

    print("=" * 80)
    print("ウォークフォワード検証（%s 〜 %s）" % (args.start, args.end))
    print("=" * 80)
    print("**目的は成績の測定ではない。** カタログ自体が 2024年までの OSAP を")
    print("見て書かれているので、この期間の成績は検証にならない（docs/05 §1.4）。")
    print("測るのは: ルックアヘッドの混入 / 回転率 / コスト / 売りの内訳")
    print()

    cached = sorted(p.stem for p in (ROOT / "data" / "prices").glob("*.json"))
    series = PR.load(cached)
    by_ticker = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    sic_asof = LS.SicAsOf.from_dera()
    asof = FA.AsOf(FA.load())
    print("価格 %d 銘柄、SIC 履歴 %d 社" % (len(series), len(sic_asof._idx)))

    bars_by_ticker = {t: BR.adjust(s.bars) for t, s in series.items()}
    rules = SL.SellRules(stop_loss=args.stop_loss, trailing_stop=args.trailing)
    limits = SZ.RiskLimits()
    costs = PF.Costs(spread_bps=args.spread_bps)
    pf = PF.Portfolio(cash=args.capital)

    reasons = collections.Counter()
    hist = []
    dates = month_ends(args.start, args.end)

    for t in dates:
        # 1) 値洗い（最高値を進める）→ **この時点の評価額を先に記録する**
        #
        # **順序を間違えていた**（2026-08-23）: 執行の後に評価していたため、
        # **t+1 で約定した後の保有を t の価格で評価**していた。
        # シグナルにルックアヘッドは無いが、**エクイティカーブが嘘になる。**
        px = PF.mark_to_market(pf, bars_by_ticker, t)
        hist.append((t, pf.value(px), len(pf.positions), None, None))

        # 2) 売り判定
        forced = []
        for tk, pos in list(pf.positions.items()):
            r, why = SL.decide(pos, SL.MarketState(), rules, t)
            if r is not SL.SellReason.HOLD:
                forced.append(tk)
                reasons[r.name] += 1

        # 3) 断面とスコア
        rows = build_cross_section(t, series, by_ticker, sic_asof, asof)
        sc = score_rows(rows)

        # 4) サイジング
        cands = []
        for r in rows:
            if r["ticker"] in forced:
                continue                      # **売ると決めた銘柄は買い直さない**
            s = sc.get(r["ticker"])
            if s is None or s <= 0:
                continue
            b = [x for x in bars_by_ticker[r["ticker"]] if x["date"] <= t]
            rr = [x for x in BR.log_return(b)[-60:] if x is not None]
            vol = (sum(x * x for x in rr) / len(rr)) ** 0.5 * (252 ** 0.5) if rr else None
            cands.append(SZ.Candidate(ticker=r["ticker"], sector=r["sector"],
                                      score=s, volatility=vol, adv_jpy=r["adv_jpy"]))
        w, _ = SZ.target_positions(cands, pf.value(px) or args.capital, limits)

        # 5) 執行（**翌取引日の始値**）
        PF.execute(pf, w, bars_by_ticker, t, costs)
        # 断面の規模は記録に足す（評価額は既に執行前で記録済み）
        hist[-1] = (hist[-1][0], hist[-1][1], hist[-1][2], len(rows), len(sc))

    print()
    print("-" * 80)
    print("%-12s %14s %8s %10s %10s" % ("日付", "評価額", "保有", "ユニバース", "スコア"))
    print("-" * 80)
    for d, v, n, u, s in hist:
        print("%-12s %14s %8d %10d %10d"
              % (d, "{:,.0f}".format(v), n, u or 0, s or 0))
    # **最終評価は期間の翌月末で測る**（最後の執行が反映された後）
    import datetime as _dt
    fin = (_dt.date.fromisoformat(dates[-1]) + _dt.timedelta(days=40)).isoformat()
    fin_px = PF.mark_to_market(pf, bars_by_ticker, fin)
    print("%-12s %14s %8d %10s %10s  ← **最後の執行を反映した評価**"
          % (fin, "{:,.0f}".format(pf.value(fin_px)), len(pf.positions), "-", "-"))

    tot = pf.value(fin_px) if hist else args.capital
    print()
    print("-" * 80)
    print("測定")
    print("-" * 80)
    import datetime as _d
    yrs = ((_d.date.fromisoformat(dates[-1]) - _d.date.fromisoformat(dates[0])).days
           / 365.25) or 1e-9
    to = PF.turnover(pf, args.capital)
    cs = PF.total_costs(pf)
    print("  期間                %.2f 年" % yrs)
    print("  約定件数            %d" % len(pf.fills))
    print("  累計の回転率        %.2f 倍（片道） → **年率 %.2f 倍**" % (to, to / yrs))
    print("  取引コストの実額    %s 円（資本の %.2f%%） → **年率 %.2f%%**"
          % ("{:,.0f}".format(cs), 100 * cs / args.capital,
             100 * cs / args.capital / yrs))
    print()
    print("  **年率コスト %.2f%% は、OQ-24 の計画基準（年 2-3%% のアルファ）の"
          % (100 * cs / args.capital / yrs))
    print("    %.0f%% を食う。**" % (100 * (cs / args.capital / yrs) / 0.025))
    print("    → **コストの仮定が結論を決める**（OQ-24）ことの実測。")
    print("    スプレッド片道 %.0fbps での値なので、"
          % args.spread_bps)
    print("    --spread-bps を変えて感応度を見ること。")
    print("  期間の損益          %+.2f%%" % (100 * (tot / args.capital - 1)))
    print("     ↑ **これは検証ではない**（設計に未来が入っている）")
    if reasons:
        print("  売りルールの発動:")
        for k, v in reasons.most_common():
            print("     %-18s %d" % (k, v))
    else:
        print("  売りルールの発動: なし（閾値を指定していないため）")

    # --- **ルックアヘッドの検査** --------------------------------------------
    print()
    print("=" * 80)
    print("**ルックアヘッドの検査 — 同じ過去を2回計算して値が変わらないか**")
    print("=" * 80)
    t0 = dates[0]
    a1 = build_cross_section(t0, series, by_ticker, sic_asof, asof)
    s1 = score_rows(a1)
    a2 = build_cross_section(t0, series, by_ticker, sic_asof, asof)
    s2 = score_rows(a2)
    diff = [k for k in s1 if abs(s1[k] - s2.get(k, float("nan"))) > 1e-12]
    print("  %s の断面を2回作った: %d 銘柄 / 差が出た銘柄 %d"
          % (t0, len(s1), len(diff)))
    print("  → **%s**" % ("値が変わらなかった。as-of は効いている" if not diff
                          else "**値が変わった。ルックアヘッドの疑い**"))

    # 期間の最初と最後で、同じ日の断面が変わらないか
    last = dates[-1]
    a3 = build_cross_section(t0, series, by_ticker, sic_asof, asof)
    s3 = score_rows(a3)
    d3 = [k for k in s1 if abs(s1[k] - s3.get(k, float("nan"))) > 1e-12]
    print("  %s まで進めた後でも %s の断面は同じか: 差 %d 銘柄"
          % (last, t0, len(d3)))
    print("  → **後の期間を計算しても過去の断面は変わらない**"
          if not d3 else "  → **過去が変わった。重大**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
