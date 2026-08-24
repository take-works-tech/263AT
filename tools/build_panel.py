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
import params_ind as PI    # noqa: E402
import params_sue as PS    # noqa: E402
import prior as PRIOR      # noqa: E402
import portfolio as PF     # noqa: E402
import prices as PR        # noqa: E402
import universe as UV      # noqa: E402

FX = 150.0
CACHE = ROOT / "data" / "panel"

# 符号（カタログの sign）。**生の値は歪めず、ここで掛ける**
# **符号はカタログの符号列そのまま。** ここで推測しない。
# 継続企業の前提（D13）の索引。**空なら D13 は効かない。**
# 空のまま「疑義なし」として通すのは、
# **データが無いことを健全と取り違える**ことになる。
# build_panel が起動時に有無をはっきり表示する。
GC_INDEX: dict[int, list[str]] = {}

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

# **パネルは上位集合のまま作る。** 絞り込みは測定側（run_shrink_wf）で行う。
#
# ここで絞ると、**「絞る前」を後から測れなくなる。**
# 採用集合（src/prior.py）は自分の成績を見ずに決めた凍結集合だが、
# **その規則が効いているかどうかは、比べないと分からない。**
# パネルに両方入れておけば、同じデータで両方を測れる。
_NOT_ADOPTED = sorted(set(SIGN) - set(PRIOR.ADOPTED))

# **業種レベルのパラメータ。** 業種内では一定なので、
# 業種内で順位を付けると全員 z=0 になって消える（実測で確認）。
# → **市場全体で順位を付ける。** normalize.py は変更しない。
#   呼び出し側が group を揃えるだけでよい。
SIGN.update({"L01": +1, "N02": +1, "L02": +1})
# **市場全体で正規化するのは、業種内で一定になるものだけ。**
# N02（SUE）は銘柄ごとに違う値なので、業種内で正規化する。
MARKET_SCOPE = set(PI.PARAMS) | set(PS.MARKET_SCOPE)

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


def scan_one(tk: str, mes: list[str], horizons: list[int]) -> dict:
    """**1銘柄を1回だけ読み、全月末ぶんの価格系を作る。**

    なぜこうするか（2026-08-24）。
    ユニバースを SEC 全銘柄（10,403）に広げたところ、
    **価格キャッシュがディスクで 7.3GB になった。**
    これを `{ticker: adjust(bars)}` として全部メモリに持つと、
    1バーが dict（11キー）なので **30GB を超える。** 載らない。

    銘柄ごとに読んで捨てれば、**同時にメモリにあるのは1銘柄ぶんだけ**になる。
    断面（正規化）が要るのは**計算した値**であって、バーではない。
    → **価格を触る処理を先に全部終わらせ、断面はその後で組む。**

    返すのは `{月末: {"snap":..., "vals":..., "fwd":...}}`。
    """
    ser = PR.load([tk]).get(tk)
    if ser is None or len(ser.bars) < 60:
        return {}
    rows = BR.adjust(ser.bars)
    out = {}
    for t in mes:
        i = _index_at(rows, t)
        if i is None or i + 1 < 60:
            continue
        vals = {k: x.value for k, x in PX.compute_all(rows, i).items()
                if x.value is not None and k in SIGN}
        # **その時点で実際に付いていた株価。** 調整後ではない。
        # 調整後を使うと「その後に大きく分割した銘柄＝勝ち馬」が
        # 低位株に見え、ゲートと J25 の両方が未来を見ることになる。
        px_true = rows[i]["close"] * PR.unadjust_factor(ser, t)
        if "J25" in SIGN:
            vals["J25"] = px_true
        out[t] = {
            "close": rows[i]["close"],
            "px_true": px_true,
            "adv20": sum(x["turnover"] for x in rows[i - 19: i + 1]) / 20.0,
            "zero60": sum(1 for x in rows[i - 59: i + 1] if x["volume"] <= 0),
            "vals": vals,
            # **将来リターンは保有期間ごとに持つ。**
            # 第1段はゲートにも保有期間にも依存しないので、
            # **一度の走査から複数のパネルを作れる。**
            "fwd": {h: forward_return(rows, t, h) for h in horizons},
        }
    return out


def build_one(t: str, scan_t: dict, by_ticker, sic_asof, asof,
              price_gate: bool = True, horizon: int = 90) -> list[dict]:
    """時点 t の断面を組む。**バーはもう見ない。**"""
    th = dataclasses.replace(UV.Thresholds.for_rho(1.0), require_age=False)
    if not price_gate:
        # **対照条件。** 空の辞書にすると judge が最低株価を見なくなる
        th = dataclasses.replace(th, min_price_local={})

    raw = []
    for tk, d in scan_t.items():
        m = by_ticker.get(tk)
        if not m or not m.cik:
            continue
        cik = int(m.cik)
        sh = asof.latest_period(cik, "SHARES", 0, t, max_lag_days=400)
        mcap = sh.value * d["close"] * FX if sh else None
        cand = UV.Candidate(
            ticker=tk, listed=True, months_listed=None,
            adv_jpy=d["adv20"] * FX, zero_volume_days=d["zero60"],
            mcap_jpy=mcap, supervised=False,
            # **継続企業の前提（D13）。** EDGAR 全文検索で埋める。
            # 索引が空なら False（＝ゲートが効かない）になるが、
            # **それは build_panel が起動時にはっきり表示する。**
            # **データが無いことを「健全」と取り違えないため。**
            going_concern_note=(
                FT.has_doubt(GC_INDEX, cik, t) if GC_INDEX else False),
            audit_clean=True,
            # **最低株価のゲート。** 現地通貨（米国株なのでドル）で渡す。
            # 円換算すると、日本の 200円 と米国の $1.3 が同じ扱いになる。
            # **調整後ではなく、その時点の実際の株価で判定する**
            price_local=d["px_true"], market="US")
        if UV.judge(cand, th):
            continue
        v = PU.compute(asof, cik, t, mcap)
        vals = {k: x.value for k, x in v.items() if x.value is not None}
        # **決算サプライズ（N02、再現 t=9.32）。** L02 の入力にもなる
        x = PS.n02(asof, cik, t)
        if x.value is not None:
            vals["N02"] = x.value
        vals.update(d["vals"])
        if len(vals) < 3:
            continue
        raw.append({"ticker": tk, "sector": ff49.industry(sic_asof.get(cik, t)),
                    "vals": vals, "adv_jpy": cand.adv_jpy, "mcap": mcap,
                    "fwd": d["fwd"].get(horizon)})
    if not raw:
        return []

    # --- 業種レベルのパラメータ（**同じ業種の全員が同じ値**）----------------
    by_sec: dict[str, list[dict]] = {}
    for r in raw:
        if r["sector"]:
            by_sec.setdefault(r["sector"], []).append(
                {"mcap": r.get("mcap"), "ret": r["vals"].get("G04")})
    ind = PI.compute(by_sec)
    for r in raw:
        got = ind.get(r["sector"] or "")
        if got:
            for pid, x in got.items():
                if x.value is not None:
                    r["vals"][pid] = x.value

    # **同業大型株の決算サプライズ（L02、再現 t=3.21）。**
    # N02 を集計するので、N02 の後でなければ作れない。
    by_sue: dict[str, list[dict]] = {}
    for r in raw:
        if r["sector"]:
            by_sue.setdefault(r["sector"], []).append(
                {"mcap": r.get("mcap"), "sue": r["vals"].get("N02")})
    for sec, x in PS.compute_l02(by_sue).items():
        if x.value is None:
            continue
        for r in raw:
            if r["sector"] == sec:
                r["vals"]["L02"] = x.value

    # 正規化して符号を掛ける
    zs = {r["ticker"]: {} for r in raw}
    for pid in SIGN:
        idx = [i for i, r in enumerate(raw) if pid in r["vals"]]
        if len(idx) < NZ.MIN_GROUP:
            continue
        # **業種レベルのものは市場全体で順位を付ける。**
        # 業種内で付けると全員 z=0 になり、情報が完全に消える。
        grp = (["ALL"] * len(idx) if pid in MARKET_SCOPE
               else [raw[i]["sector"] for i in idx])
        res = NZ.normalize([raw[i]["vals"][pid] for i in idx], grp,
                           market=["US"] * len(idx))
        for k, i in enumerate(idx):
            if not res.missing[k]:
                zs[raw[i]["ticker"]][pid] = SIGN[pid] * res.z[k]

    out = []
    for r in raw:
        z = zs[r["ticker"]]
        if not z:
            continue
        out.append({"date": t, "ticker": r["ticker"], "sector": r["sector"],
                    "z": z, "fwd": r["fwd"], "adv_jpy": r["adv_jpy"]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2024-11-30")
    ap.add_argument("--end", default="2026-06-30")
    ap.add_argument("--horizons", type=int, nargs="*", default=[90],
                    help="将来リターンの期間。**263AT の保有は 6ヶ月-5年**だが、"
                         "重みの推定にはもっと短い窓が要る（観測数のため）")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--max-tickers", type=int, default=0,
                    help="走査する銘柄数の上限（**動作確認用**。0 で無制限）")
    ap.add_argument("--gates", default="on",
                    choices=["on", "off", "both"],
                    help="最低株価のゲート。**both は対照実験**"
                         "（同じ走査結果から有無だけを変えた2本を作る）")
    args = ap.parse_args()

    base = CACHE          # モジュール定数をそのまま使う（再代入しない）
    gc_path = ROOT / "data" / "going_concern.json"
    if gc_path.exists():
        GC_INDEX.update({int(k): v for k, v in
                         json.loads(gc_path.read_text(encoding="utf-8")).items()})
        print("継続企業の前提の索引: **%d 社**" % len(GC_INDEX))
    else:
        print("**継続企業の前提の索引が無い。D13 ゲートは効かない。**")
        print("  tools/build_gc.py を先に実行する")
    by_ticker = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    sic_asof = LS.SicAsOf.from_dera()
    mes = month_ends(args.start, args.end)
    cached = sorted(p.stem for p in (ROOT / "data" / "prices").glob("*.json"))
    if args.max_tickers:
        cached = cached[: args.max_tickers]
    conds = ([("gate", True), ("nogate", False)] if args.gates == "both"
             else [("gate", True)] if args.gates == "on"
             else [("nogate", False)])

    # --- 第1段: 価格を触る処理を全部済ませる ------------------------------
    # **1銘柄ずつ読んで捨てる。** 同時にメモリにあるのは1銘柄ぶんだけ。
    # ディスク 7.6GB を全部オブジェクトにすると 30GB を超えて載らない。
    #
    # **この段はゲートにも保有期間にも依存しない。**
    # だから一度走らせれば、条件違いのパネルを何本でも作れる。
    print("価格 %d 銘柄 / 月末 %d 点 / 保有期間 %s / 条件 %s"
          % (len(cached), len(mes), args.horizons, [c[0] for c in conds]))
    scan: dict[str, dict] = {t: {} for t in mes}
    kept = 0
    for k, tk in enumerate(cached):
        try:
            per = scan_one(tk, mes, args.horizons)
        except Exception as e:
            print("    NG %s: %s" % (tk, str(e)[:60]))
            continue
        if per:
            kept += 1
            for t, d in per.items():
                scan[t][tk] = d
        if (k + 1) % 1000 == 0:
            print("  走査 %d/%d（値が取れた %d）" % (k + 1, len(cached), kept))
    print("**第1段おわり: %d 銘柄で値が取れた**" % kept)

    need_ciks = {int(by_ticker[t].cik) for t in cached
                 if by_ticker.get(t) and by_ticker[t].cik}
    print("勘定を読む CIK %d" % len(need_ciks))

    # --- 第2段: 断面を組む（**バーはもう見ない**）--------------------------
    def quarters_for(year: int) -> list[str]:
        return ["%dq%d" % (y, q)
                for y in range(year - LOOKBACK_YEARS, year + 1)
                for q in (1, 2, 3, 4)]

    for name, _ in conds:
        (base / name).mkdir(parents=True, exist_ok=True)

    total = 0
    cur_year, asof = None, None
    for t in mes:
        y = int(t[:4])
        if y != cur_year:
            fs = FA.load(quarters_for(y), ciks=need_ciks)
            asof = FA.AsOf(fs)
            cur_year = y
            print("  --- %d年: 勘定 %d 件" % (y, len(fs)))
        line = []
        for name, gate in conds:
            for h in args.horizons:
                f = base / name / ("%s_h%d.json" % (t, h))
                if f.exists() and not args.rebuild:
                    rows = json.loads(f.read_text(encoding="utf-8"))
                else:
                    rows = build_one(t, scan[t], by_ticker, sic_asof, asof,
                                     price_gate=gate, horizon=h)
                    f.write_text(json.dumps(rows), encoding="utf-8")
                line.append("%s/h%d %4d" % (name, h, len(rows)))
                total += len(rows)
        print("  %s  %s" % (t, "  ".join(line)))
        scan[t] = {}          # **使い終わった月は捨てる**
    print("合計 %d 行 → %s" % (total, CACHE.relative_to(ROOT)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
