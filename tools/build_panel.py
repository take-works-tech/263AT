#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
縮小推定に食わせるパネルを作る。

    {date, ticker, sector, z: {pid: 値}, fwd: 将来リターン}

**`fwd` は「その断面を作った後」のリターンでなければならない。**
ここに現在や過去のリターンを入れた瞬間、
どれだけ as-of を守っていても**ルックアヘッドになる。**

→ `fwd` は **t+1 の始値で買って、t+H の始値で売った**リターンとする。
  執行の規約（spec §1.5）と一致させる。
  **終値→終値にすると、実際には取れない値を学習することになる。**

キャッシュ
----------
断面の計算は重い（665銘柄 × 10パラメータ × 各種 TTM）。
`data/panel/<date>.json` に保存し、2回目以降は再利用する。
**data/ 配下なので git には入らない。**

使い方
    .venv/Scripts/python.exe tools/build_panel.py --start 2024-11-30 --end 2025-06-30
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
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

import bars as BR          # noqa: E402
import facts as FA         # noqa: E402
import ff49                # noqa: E402
import listing as LS       # noqa: E402
import normalize as NZ     # noqa: E402
import params_us as PU     # noqa: E402
import params_px as PX     # noqa: E402
import portfolio as PF     # noqa: E402
import prices as PR        # noqa: E402
import universe as UV      # noqa: E402

FX = 150.0
CACHE = ROOT / "data" / "panel"

# 符号（カタログの sign）。**生の値は歪めず、ここで掛ける**
# **符号はカタログの符号列そのまま。** ここで推測しない。
LOOKBACK_YEARS = 3    # latest_period が最大400日遡るので3年で足りる

SIGN = {"E29": +1, "B22": -1, "E03": -1, "F24": -1, "E01": -1,
        "B02": +1, "B06": +1, "A04": +1, "A03": +1, "A06": +1,
        # 価格系（Phase 0、src/params_px.py）
        "G01": +1, "G02": +1, "G03": +1, "G04": -1, "G16": +1, "G32": +1,
        "G38": +1, "G39": +1, "G40": +1, "G41": +1, "G42": -1, "G43": +1,
        "G44": +1, "G45": -1,
        "H05": +1,          # 定義に負号が入っているので符号は +
        "I01": -1,
        "J10": -1, "J22": -1, "J25": +1}

# **J01（平均売買代金）はここに入れない。**
# カタログの符号が `?` のまま解決していない。**推測で符号を付けると、
# 効いているのか符号を当てただけなのかが区別できなくなる。**
# 流動性ゲートの入力としては adv_jpy で既に使っている。


def _index_at(rows: list[dict], t: str) -> int | None:
    """`t` 以前で最後の行の位置。**t より後ろは存在しないものとして扱う。**

    二分探索でなく線形で十分（月末ごとに1回）。
    **`date <= t` で切る**のが PIT の境界そのもの。
    """
    lo = None
    for i, r in enumerate(rows):
        if r["date"] <= t:
            lo = i
        else:
            break
    return lo


def month_ends(lo: str, hi: str) -> list[str]:
    out, d = [], dt.date.fromisoformat(lo)
    while d <= dt.date.fromisoformat(hi):
        nxt = dt.date(d.year + (d.month // 12), d.month % 12 + 1, 1)
        out.append((nxt - dt.timedelta(days=1)).isoformat())
        d = nxt
    return out


def forward_return(bars: list[dict], t: str, horizon_days: int) -> float | None:
    """**t+1 の始値で買って、t+H の始値で売った**リターン。

    執行の規約（spec §1.5）と一致させる。
    **終値→終値にすると、実際には取れない値を学習する。**
    """
    entry = PF.next_open(bars, t)
    if entry is None:
        return None
    exit_after = (dt.date.fromisoformat(entry[0])
                  + dt.timedelta(days=horizon_days)).isoformat()
    ex = PF.next_open(bars, exit_after)
    if ex is None:
        return None
    return ex[1] / entry[1] - 1.0 if entry[1] > 0 else None


def build_one(t: str, series, by_ticker, sic_asof, asof, bars_by_ticker,
              horizon_days: int) -> list[dict]:
    th = dataclasses.replace(UV.Thresholds.for_rho(1.0), require_age=False)
    raw = []
    for tk, s in series.items():
        m = by_ticker.get(tk)
        if not m or not m.cik:
            continue
        # **PR.snapshot を使わない。**
        # あれは呼ばれるたびに `bars.adjust` で全系列を再調整し、
        # さらに `adv` / `zero_volume_days` を**全日付ぶん**計算して
        # 末尾だけ取る。1,383銘柄 × 173ヶ月 = **24万回**やると終わらない。
        # ここでは調整済みの行を使い回し、**末尾の窓だけ**を数える。
        rows_t = bars_by_ticker.get(tk)
        i_t = _index_at(rows_t, t) if rows_t else None
        if i_t is None or i_t + 1 < 60:
            continue
        snap = {
            "close": rows_t[i_t]["close"],
            "adv20": sum(x["turnover"]
                         for x in rows_t[i_t - 19: i_t + 1]) / 20.0,
            "zero_vol_60": sum(1 for x in rows_t[i_t - 59: i_t + 1]
                               if x["volume"] <= 0),
        }
        cik = int(m.cik)
        sh = asof.latest_period(cik, "SHARES", 0, t, max_lag_days=400)
        mcap = sh.value * snap["close"] * FX if sh else None
        cand = UV.Candidate(
            ticker=tk, listed=True, months_listed=None,
            adv_jpy=(snap["adv20"] or 0) * FX,
            zero_volume_days=snap["zero_vol_60"], mcap_jpy=mcap,
            supervised=False, going_concern_note=False, audit_clean=True,
            # **最低株価のゲート。** 現地通貨（米国株なのでドル）で渡す。
            # 円換算すると、日本の 200円 と米国の $1.3 が同じ扱いになる。
            price_local=snap["close"], market="US")
        if UV.judge(cand, th):
            continue
        v = PU.compute(asof, cik, t, mcap)
        vals = {k: x.value for k, x in v.items() if x.value is not None}
        # **価格系を足す。** 財務が無い銘柄でも価格系だけで行が立つ
        for k, x in PX.compute_all(rows_t, i_t).items():
            if x.value is not None and k in SIGN:
                vals[k] = x.value
        if len(vals) < 3:
            continue
        raw.append({"ticker": tk, "sector": ff49.industry(sic_asof.get(cik, t)),
                    "vals": vals, "adv_jpy": cand.adv_jpy})
    if not raw:
        return []

    # 業種内で正規化して符号を掛ける
    zs = {r["ticker"]: {} for r in raw}
    for pid in SIGN:
        idx = [i for i, r in enumerate(raw) if pid in r["vals"]]
        if len(idx) < NZ.MIN_GROUP:
            continue
        res = NZ.normalize([raw[i]["vals"][pid] for i in idx],
                           [raw[i]["sector"] for i in idx],
                           market=["US"] * len(idx))
        for k, i in enumerate(idx):
            if not res.missing[k]:
                zs[raw[i]["ticker"]][pid] = SIGN[pid] * res.z[k]

    out = []
    for r in raw:
        z = zs[r["ticker"]]
        if not z:
            continue
        fwd = forward_return(bars_by_ticker[r["ticker"]], t, horizon_days)
        out.append({"date": t, "ticker": r["ticker"], "sector": r["sector"],
                    "z": z, "fwd": fwd, "adv_jpy": r["adv_jpy"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-11-30")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--horizon-days", type=int, default=90,
                    help="将来リターンの期間。**263AT の保有は 6ヶ月-5年**だが、"
                         "重みの推定にはもっと短い窓が要る（観測数のため）")
    ap.add_argument("--rebuild", action="store_true")
    args = ap.parse_args()

    CACHE.mkdir(parents=True, exist_ok=True)
    cached = sorted(p.stem for p in (ROOT / "data" / "prices").glob("*.json"))
    series = PR.load(cached)
    by_ticker = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    sic_asof = LS.SicAsOf.from_dera()
    bars_by_ticker = {t: BR.adjust(s.bars) for t, s in series.items()}
    # **価格を持っている銘柄の CIK だけ読む。**
    # DERA は 5,000-8,000 社ぶんあるが、使うのは 1,383 銘柄。
    need_ciks = {int(by_ticker[t].cik) for t in series
                 if by_ticker.get(t) and by_ticker[t].cik}
    print("価格 %d 銘柄 / 勘定を読む CIK %d" % (len(series), len(need_ciks)))

    # **勘定データは年ごとに窓を切って読む。**
    # 69四半期を一度に持つと 2,462万ファクトになり、
    # Python オブジェクトで数GBに達する（読み込みだけで2分超）。
    # `latest_period` が最大400日遡るので、**3年ぶんあれば足りる。**
    def quarters_for(year: int) -> list[str]:
        return ["%dq%d" % (y, q)
                for y in range(year - LOOKBACK_YEARS, year + 1)
                for q in (1, 2, 3, 4)]

    total = 0
    cur_year, asof = None, None
    for t in month_ends(args.start, args.end):
        f = CACHE / ("%s_h%d.json" % (t, args.horizon_days))
        if f.exists() and not args.rebuild:
            n = len(json.loads(f.read_text(encoding="utf-8")))
            print("  %s  キャッシュ %d 行" % (t, n))
            total += n
            continue
        y = int(t[:4])
        if y != cur_year:
            qs = quarters_for(y)
            fs = FA.load(qs, ciks=need_ciks)
            asof = FA.AsOf(fs)
            cur_year = y
            print("  --- %d年: 勘定 %d 件（%s 〜 %s）"
                  % (y, len(fs), qs[0], qs[-1]))
        rows = build_one(t, series, by_ticker, sic_asof, asof,
                         bars_by_ticker, args.horizon_days)
        f.write_text(json.dumps(rows), encoding="utf-8")
        n_fwd = sum(1 for r in rows if r["fwd"] is not None)
        print("  %s  %4d 行（将来リターンあり %4d）" % (t, len(rows), n_fwd))
        total += len(rows)
    print("合計 %d 行 → %s" % (total, CACHE.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
