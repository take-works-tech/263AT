#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
通しの検証。**買いスコアだけでなく、売り・サイズ・コストまで含めた最終利益。**

これまで測っていたのは**買いスコアの分位差だけ**だった。
しかし最終的な利益は、**売りルール・ポジションサイズ・執行コスト**で決まる。
分位差が +5% でも、**損切りが早すぎれば全部消え、遅すぎれば裾で殺される。**

**売りは日次で判定する。**
月末だけで判定すると、損切りもトレーリングも**ほとんど発動しない。**
月中に -30% まで落ちて -15% で月末を迎えたら、
月次評価では「-15%」としか見えず、**-20% の損切りは素通りする。**
Position は最高値を持ち回るので（sell.Position.update）、
**日次で更新しなければトレーリングは作れない。**

    買い     … 月末のみ（断面が月次だから）
    売り     … **毎営業日**（損切りは指値として置いてあるものと同じ）
    約定     … 翌営業日の始値（spec §1.5）

価格は**遅延読み込み**する。
ユニバースは 9,631銘柄あるが、**実際に触るのは保有と買い候補だけ**で、
全期間を通しても数百銘柄にしかならない。全部載せるとメモリが持たない。

使い方
    .venv/Scripts/python.exe tools/run_system.py --panel gate --horizon 90
    .venv/Scripts/python.exe tools/run_system.py --stop-loss -0.20 --trailing -0.25
"""
from __future__ import annotations

import argparse
import collections
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

import bars as BR             # noqa: E402
import portfolio as PF        # noqa: E402
import prices as PR           # noqa: E402
import prior as PRIOR         # noqa: E402
import sell as SL             # noqa: E402
import shrink as SH           # noqa: E402
import sizing as SZ           # noqa: E402


class BarStore:
    """**触った銘柄だけ**を読む。9,631銘柄を全部載せるとメモリが持たない。"""

    def __init__(self):
        self._c: dict[str, list[dict]] = {}
        self.misses = 0

    def get(self, tk: str) -> list[dict]:
        v = self._c.get(tk)
        if v is None:
            s = PR.load([tk]).get(tk)
            v = BR.adjust(s.bars) if s else []
            if not v:
                self.misses += 1
            self._c[tk] = v
        return v

    def upto(self, tk: str, date: str) -> list[dict]:
        return [x for x in self.get(tk) if x["date"] <= date]


def load_panel(branch: str, horizon: int) -> dict[str, list[dict]]:
    d = ROOT / "data" / "panel" / branch
    out: dict[str, list[dict]] = {}
    for f in sorted(d.glob("*_h%d.json" % horizon)):
        rows = json.loads(f.read_text(encoding="utf-8"))
        if rows:
            out[rows[0]["date"]] = rows
    return out


def usable_at(row: dict, T: str, horizon: int) -> bool:
    """**訓練に使ってよいか。** 日付が過去でもラベルが未来なら使えない。"""
    if row["date"] >= T or row.get("fwd") is None:
        return False
    resolved = (dt.date.fromisoformat(row["date"])
                + dt.timedelta(days=horizon)).isoformat()
    return resolved <= T


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="gate")
    ap.add_argument("--horizon", type=int, default=90)
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    ap.add_argument("--stop-loss", type=float, default=None)
    ap.add_argument("--trailing", type=float, default=None)
    ap.add_argument("--max-hold-years", type=float, default=None)
    ap.add_argument("--spread-bps", type=float, default=25.0)
    ap.add_argument("--min-train", type=int, default=2000)
    ap.add_argument("--min-trade-frac", type=float, default=0.005,
                    help="**この比率未満の調整はしない。** 回転率＝コストの主因")
    ap.add_argument("--all-params", action="store_true")
    a = ap.parse_args()

    panel = load_panel(a.panel, a.horizon)
    if not panel:
        print("パネルが無い: data/panel/%s" % a.panel)
        return 1
    dates = sorted(panel)
    flat = [r for t in dates for r in panel[t]]
    names = sorted({k for r in flat for k in r["z"]})
    if not a.all_params:
        names = [n for n in names if n in PRIOR.ADOPTED]

    print("=" * 78)
    print("通しの検証（%s / 保有 %d日 / %s 〜 %s）"
          % (a.panel, a.horizon, dates[0], dates[-1]))
    print("=" * 78)
    print("**目的は成績の自慢ではない。** カタログ自体が 2024年までの OSAP を")
    print("見て書かれているので、この期間の成績は検証にならない（docs/05 §1.3）。")
    print("測るのは: **売りが効くか / 回転率 / コスト / 集中度の影響**")
    print()
    print("パネル %d 日付 / %d 行 / パラメータ %d 本"
          % (len(dates), len(flat), len(names)))
    print("売りルール: 損切り %s / トレーリング %s / 保有上限 %s"
          % (a.stop_loss, a.trailing, a.max_hold_years))
    print()

    store = BarStore()
    rules = SL.SellRules(stop_loss=a.stop_loss, trailing_stop=a.trailing,
                         max_hold_years=a.max_hold_years)
    limits = SZ.RiskLimits()
    costs = PF.Costs(spread_bps=a.spread_bps)
    pf = PF.Portfolio(cash=a.capital)

    reasons: collections.Counter = collections.Counter()
    equity: list[tuple[str, float]] = []
    n_gen = 0


    for k, T in enumerate(dates):
        train = [r for r in flat if usable_at(r, T, a.horizon)]
        if len(train) < a.min_train:
            continue
        fit = SH.fit(train, names)
        n_gen += 1

        # --- 1) 評価（**執行より先**。t+1 の約定を t の価格で見ない）------
        px = PF.mark_to_market(pf, {t: store.get(t) for t in pf.positions}, T)
        equity.append((T, pf.value(px)))

        # --- 2) 売り判定（この時点）--------------------------------------
        forced = set()
        for tk, pos in list(pf.positions.items()):
            r, _ = SL.decide(pos, SL.MarketState(), rules, T)
            if r is not SL.SellReason.HOLD:
                forced.add(tk)
                reasons[r.name] += 1

        # --- 3) スコアと候補 ----------------------------------------------
        cands = []
        for r in panel[T]:
            tk = r["ticker"]
            if tk in forced:
                continue                  # **売ると決めた銘柄は買い直さない**
            s = SH.score(r["z"], fit)
            if s is None or s <= 0:
                continue
            b = store.upto(tk, T)
            rr = [x for x in BR.log_return(b[-61:]) if x is not None]
            vol = ((sum(x * x for x in rr) / len(rr)) ** 0.5 * (252 ** 0.5)
                   if len(rr) >= 20 else None)
            cands.append(SZ.Candidate(ticker=tk, sector=r["sector"], score=s,
                                      volatility=vol, adv_jpy=r["adv_jpy"]))
        w, _ = SZ.target_positions(cands, pf.value(px) or a.capital, limits)
        for tk in forced:
            w[tk] = 0.0

        # --- 4) 執行（翌営業日の始値）------------------------------------
        touch = set(w) | set(pf.positions)
        PF.execute(pf, w, {t: store.get(t) for t in touch}, T, costs,
                   min_trade_frac=a.min_trade_frac)

        # --- 5) 次の月末までは**毎営業日**売りだけ見る ---------------------
        nxt = dates[k + 1] if k + 1 < len(dates) else "9999-12-31"
        # **営業日は保有銘柄のバーから取る。**
        # 最初はパネルの日付から作っていたが、**パネルは月末しか持たない**ので
        # 日次ループが1日も回らず、**損切りが一度も発動しなかった。**
        held0 = {t: store.get(t) for t in pf.positions}
        days = sorted({x["date"] for b in held0.values() for x in b
                       if T < x["date"] < nxt})
        for d in days:
            if not pf.positions:
                break
            held = {t: store.get(t) for t in pf.positions}
            PF.mark_to_market(pf, held, d)
            hit = {}
            for tk, pos in list(pf.positions.items()):
                r, _ = SL.decide(pos, SL.MarketState(), rules, d)
                if r is not SL.SellReason.HOLD:
                    hit[tk] = 0.0
                    reasons[r.name] += 1
            if hit:
                # **日中に発動したら、その日のシグナルとして翌日始値で売る**
                PF.execute(pf, {**pf.weights(PF.mark_to_market(pf, held, d)),
                                **hit},
                           {t: store.get(t) for t in pf.positions}, d, costs,
                           min_trade_frac=a.min_trade_frac)

    # ------------------------------------------------------------------ 結果
    if not equity:
        print("**1回も生成できなかった。** 訓練の観測が足りない。")
        return 0
    px = PF.mark_to_market(pf, {t: store.get(t) for t in pf.positions},
                           dates[-1])
    final = pf.value(px)
    yrs = ((dt.date.fromisoformat(equity[-1][0])
            - dt.date.fromisoformat(equity[0][0])).days / 365.25) or 1.0
    cagr = (final / a.capital) ** (1.0 / yrs) - 1.0

    print("-" * 78)
    print("結果")
    print("-" * 78)
    print("  生成回数            %d" % n_gen)
    print("  期間                %.1f 年" % yrs)
    print("  **最終評価額        %s 円**（元本 %s）"
          % ("{:,.0f}".format(final), "{:,.0f}".format(a.capital)))
    print("  **年率（CAGR）      %+.2f%%**" % (100 * cagr))
    print("  約定回数            %d" % len(pf.fills))
    # **システムが黙って止まっていないか。**
    # min_trade_frac を上げすぎると、1銘柄の目標ウェイト（2%前後）を
    # 調整幅の下限が上回り、**一度も売買されないまま現金で寝る。**
    # それでもエラーは出ず、「CAGR +0.50%」と表示されるだけになる。
    # 実測: min_trade_frac=0.05 で 13.5年に約定 121回、0.10 で **2回**。
    if len(pf.fills) < n_gen:
        print("  " + "!" * 60)
        print("  **約定が生成回数(%d)より少ない。システムがほぼ動いていない。**"
              % n_gen)
        print("  min_trade_frac(%.3f) が1銘柄の目標ウェイトを上回っていないか。"
              % a.min_trade_frac)
        print("  **この成績は「取引しなかった結果」であって、戦略の成績ではない。**")
        print("  " + "!" * 60)
    print("  **払ったコスト合計  %.0f 円（元本の %.1f%%）**"
          % (PF.total_costs(pf), 100 * PF.total_costs(pf) / a.capital))

    # 最大ドローダウン。**これが 3M円の耐えられる範囲かが実務の問い**
    peak, mdd, mdd_at = 0.0, 0.0, ""
    for d, v in equity:
        peak = max(peak, v)
        if peak > 0 and (v / peak - 1.0) < mdd:
            mdd, mdd_at = v / peak - 1.0, d
    print("  **最大ドローダウン  %.1f%%**（%s）" % (100 * mdd, mdd_at))

    print()
    print("  売りの内訳:")
    if reasons:
        for r, n in reasons.most_common():
            print("    %-22s %5d" % (r, n))
    else:
        print("    **一度も発動していない**（ルールが None のため）")
    if store.misses:
        print("  価格が取れなかった銘柄 %d（**黙って無視していない**）"
              % store.misses)

    print()
    print("  " + "!" * 60)
    print("  **これは検証ではない**（docs/05 §1.3, §4.5）。")
    print("  生存者バイアス（2012年の残存率 32.6%）と")
    print("  カタログ自体のルックアヘッドが残っている。")
    print("  **売りルールが効くかどうかの相対比較にのみ使う。**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
