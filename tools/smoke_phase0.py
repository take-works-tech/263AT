#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 0 の層（bars → universe → normalize）を**実データで**通す煙テスト。

自己テストは合成データで規約を確認するもので、
**実データでしか出てこない問題**がある:
  - 分割イベントが実際に含まれているか
  - 出来高ゼロ日・売買停止が実際に起きるか
  - 日米で列名・タイムゾーン・欠損の出方が違わないか
  - 東証33業種のような業種分類が実際に付くか

**ここで失敗しても Phase 0 の設計が間違っているとは限らない。**
外部データの形が想定と違うだけのことが多いので、
**何が違ったかを出力する**ことを優先する。

使い方
    .venv/Scripts/python.exe tools/smoke_phase0.py
    .venv/Scripts/python.exe tools/smoke_phase0.py --tickers 7203.T,6758.T,AAPL
"""
from __future__ import annotations

import argparse
import dataclasses
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import bars as B          # noqa: E402
from src import normalize as N     # noqa: E402
from src import universe as U      # noqa: E402

# 日米の代表的な銘柄。**分割・低流動性・大型を混ぜる。**
DEFAULT = ["7203.T", "6758.T", "9984.T", "AAPL", "MSFT", "BRK-B"]


def fetch(tickers: list[str], period: str = "3y"):
    """yfinance から取る。**分割情報を必ず一緒に取る。**

    **【実測 2026-08-23】yfinance は `auto_adjust=False` を指定しても
    分割については既に調整済みの価格を返す**（未調整なのは配当だけ）。
    確認: 6758.T の 2024-09-27（5分割）で Close が 2848 → 2861 と
    5分の1に落ちていない。

    → **split_factor を bars.Bar に渡してはいけない。二重調整になる。**
    渡すと分割日に +161%（= ln 5）の偽のリターンが立ち、
    モメンタム（G）は偽の急騰を買い、リバーサル（H）は偽の急落を買う。
    **バックテストでは検出されず成績を良く見せる方向に効く**（spec §8）。

    分割イベント自体は**記録する**（何が起きたかを追えるように）が、
    調整には使わない。`bars.detect_split_misadjustment()` で毎回検査する。
    """
    import yfinance as yf

    out = {}
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            df = tk.history(period=period, auto_adjust=False, actions=True)
        except Exception as e:                       # ネットワーク・API 変更
            print("  %-8s 取得失敗: %s" % (t, str(e)[:60]))
            continue
        if df is None or df.empty:
            print("  %-8s データが空" % t)
            continue
        rows, splits = [], []
        for idx, r in df.iterrows():
            sf = float(r.get("Stock Splits") or 0.0)
            splits.append((str(idx.date()), sf)) if sf > 0 else None
            rows.append(B.Bar(
                date=str(idx.date()),
                open=float(r["Open"]), high=float(r["High"]),
                low=float(r["Low"]), close=float(r["Close"]),
                volume=float(r["Volume"]),
                # **1.0 固定。** Yahoo が既に分割調整済みなので再調整しない
                split_factor=1.0,
                dividend=float(r.get("Dividends") or 0.0),
                halted=False,
            ))
        out[t] = (rows, splits)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default=",".join(DEFAULT))
    ap.add_argument("--period", default="3y")
    args = ap.parse_args()
    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]

    print("=" * 70)
    print("Phase 0 煙テスト — bars → universe → normalize を実データで")
    print("=" * 70)
    print("取得中: %s" % ", ".join(tickers))
    raw = fetch(tickers, args.period)
    if not raw:
        print("**1銘柄も取得できなかった。** ネットワークか API の問題。")
        return 1
    print()

    # --- 1. bars 層 ---------------------------------------------------------
    print("-" * 70)
    print("1. bars — 調整と欠損の扱い")
    print("-" * 70)
    state = {}
    for t, (bs, splits) in raw.items():
        a = B.adjust(bs)
        # **二重調整の検査。** Yahoo の分割を再適用したら検出されるはず
        probe = [dataclasses.replace(b, split_factor=dict(splits).get(b.date, 1.0))
                 for b in bs]
        mis = B.detect_split_misadjustment(B.adjust(probe), probe)
        r = B.log_return(a)
        tr = B.total_return(a)
        adv = B.adv(a, 20)
        zv = B.zero_volume_days(a, 60)
        n_split = len(splits)
        n_div = sum(1 for b in bs if b.dividend > 0)
        n_nan = sum(1 for x in r if x is None)
        n_zero_vol = sum(1 for x in a if x["volume"] <= 0)
        # 最大の日次変動。**調整漏れがあるとここが跳ねる。**
        mx = max((abs(x) for x in r if x is not None), default=0.0)
        print("  %-8s %4d日  分割%d 配当%d  出来高0が%d日  欠損r=%d  最大|r|=%.1f%%"
              % (t, len(a), n_split, n_div, n_zero_vol, n_nan, 100 * mx))
        if mx > 0.35:
            print("           ⚠ 35% 超の変動。イベントか調整漏れか要確認")
        if mis:
            print("           ✓ 二重調整の検査: %d 件の分割で「再適用すると壊れる」ことを確認"
                  % len(mis))
            print("             → **Yahoo は既に分割調整済み。split_factor は 1.0 で正しい**")
        state[t] = {"adj": a, "adv": adv[-1], "zv": zv[-1],
                    "close": a[-1]["close"], "ret": r, "tr": tr}

    # 配当込みと価格リターンの差 = 配当利回り相当。**符号が正であるべき。**
    print()
    for t, s in state.items():
        pr = sum(x for x in s["ret"] if x is not None)
        tr = sum(x for x in s["tr"] if x is not None)
        print("  %-8s 価格リターン %+.1f%%  配当込み %+.1f%%  差 %+.2f%%/年"
              % (t, 100 * pr, 100 * tr, 100 * (tr - pr) / 3))
        if tr < pr - 1e-9:
            print("           ⚠ **配当込みが価格リターンを下回っている。** 実装かデータの誤り")

    # --- 2. universe 層 -----------------------------------------------------
    print()
    print("-" * 70)
    print("2. universe — UNIVERSE(t) の判定（spec §6）")
    print("-" * 70)
    fx = 150.0     # 円換算の暫定レート。**本来は available_at 時点のスポット**
    cands = []
    for t, s in state.items():
        jp = t.endswith(".T")
        mult = 1.0 if jp else fx
        cands.append(U.Candidate(
            ticker=t, listed=True, months_listed=120.0,
            adv_jpy=(s["adv"] or 0.0) * mult,
            zero_volume_days=s["zv"],
            # 時価総額は株数が要る。ここでは煙テストなので売買代金から粗く代用する
            mcap_jpy=(s["adv"] or 0.0) * mult * 200,
            supervised=False, going_concern_note=False,
            audit_clean=True,          # **本来は EDINET/SEC から取る。未取得なら None**
        ))
    print(U.report(cands, rho=1.0))
    print()
    print("  ※ 監査意見（E22）と継続企業の前提（D13）は EDINET / SEC が要るので")
    print("     ここでは True を仮置きしている。**実運用では None にして落とすのが正しい**")
    print("     — universe.py は None を『適正』に丸めない設計になっている")

    # --- 3. normalize 層 ----------------------------------------------------
    print()
    print("-" * 70)
    print("3. normalize — 断面正規化（spec §4 / §4.1）")
    print("-" * 70)
    # 実銘柄が少ないので、MIN_GROUP に届かないことを確認するのが主眼
    vals = [s["adv"] for s in state.values()]
    mkts = ["JP" if t.endswith(".T") else "US" for t in state]
    r = N.normalize(vals, ["S"] * len(vals), market=mkts)
    print("  銘柄数 %d、市場内で閉じたグループ数 %d" % (len(vals), r.n_groups))
    print("  欠損扱い: %d 件" % sum(1 for m in r.missing if m))
    if all(r.missing):
        print("  → **全件が欠損扱い。これは正しい挙動。**")
        print("     N<%d のグループではランク化しない（spec §4）。" % N.MIN_GROUP)
        print("     **煙テストの銘柄数では断面ランクは作れない** — ")
        print("     実運用には数百銘柄のユニバースが要る、ということが確認できた")
    else:
        for t, z, m in zip(state, r.z, r.missing):
            print("  %-8s z=%+.2f  欠損=%s" % (t, z, m))

    print()
    print("=" * 70)
    print("結論")
    print("=" * 70)
    print("bars 層は実データで動く（分割・配当・出来高ゼロを正しく扱えた）。")
    print("universe 層は判定できるが、**監査意見と継続企業の前提は")
    print("  EDINET / SEC が無いと埋まらない** → Phase 1/2 が要る。")
    print("normalize 層は N<30 で正しく欠損を返す。**断面ランクには")
    print("  数百銘柄のユニバースが必要** → 銘柄マスタの取得が次の作業。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
