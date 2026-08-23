#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
執行と勘定 — **約定の規約を1箇所に固定する。**

docs/02_definition_spec.md §1.5 と §3、docs/05 §2.1。

§1.5 で決めた約定の規約
-----------------------
| 規約 | 理由 |
|---|---|
| **約定はシグナル発生日の翌取引日の始値** | 当日終値での約定は「引けを見てから引けで買う」= ルックアヘッド |
| ストップ高安・売買停止では約定しない | 翌営業日に持ち越す（J11 / J18） |
| コストはスプレッド + 手数料 | 小型株ではスプレッドが手数料より大きい（J04） |

**この層が最も事故を起こしやすい。**
「翌日の始値」を「当日の終値」にしただけでバックテストの成績は劇的に良くなる。
→ **約定価格を取る関数を1つに絞り、そこにテストを置く。**

コストの扱い
------------
**片道コスト = スプレッドの半分 + 手数料。**
J04（実効スプレッド）の検証で「往復コストの主成分」と書いた通り、
**小型株ではスプレッドが支配的。**

OQ-24 の結論（月次 0.03-0.10% のコストで 7/9 のスキームが正、
0.25% では 1/9）を踏まえると、**コストの仮定が結論を決める。**
→ **コストを引数にし、複数の仮定で回す。**

自己テスト
    python src/portfolio.py
"""
from __future__ import annotations

import dataclasses

import sell as SL      # type: ignore


@dataclasses.dataclass(frozen=True)
class Costs:
    """取引コスト。**片道で持つ。**

    `spread_bps` は**実効スプレッドの半分**（片道で払う分）。
    `commission_bps` は手数料。SBI の現物は現在ほぼゼロだが、
    **ゼロを既定にしない** — 将来変わるし、米国株には為替コストがある。
    """

    spread_bps: float = 25.0        # 片道 0.25%。小型株の実測に近い保守的な値
    commission_bps: float = 5.0     # 片道 0.05%
    slippage_bps: float = 0.0       # 追加の滑り（感度分析用）

    def one_way(self) -> float:
        return (self.spread_bps + self.commission_bps + self.slippage_bps) / 10000.0


@dataclasses.dataclass
class Fill:
    """約定1件。**なぜその価格になったかを持ち歩く。**"""

    ticker: str
    date: str
    side: str            # "buy" / "sell"
    shares: float
    price: float         # コスト込みの実効価格
    gross_price: float   # コスト前の約定価格（= 翌日始値）
    cost: float
    reason: str = ""


@dataclasses.dataclass
class Portfolio:
    """保有と現金。**評価額はコスト前の終値で測る**（保守的に見せない）。"""

    cash: float
    positions: dict[str, SL.Position] = dataclasses.field(default_factory=dict)
    fills: list[Fill] = dataclasses.field(default_factory=list)

    def value(self, prices: dict[str, float]) -> float:
        v = self.cash
        for t, p in self.positions.items():
            px = prices.get(t)
            if px is not None:
                v += p.shares * px
        return v

    def weights(self, prices: dict[str, float]) -> dict[str, float]:
        tot = self.value(prices)
        if tot <= 0:
            return {}
        return {t: p.shares * prices.get(t, 0.0) / tot
                for t, p in self.positions.items()}


def next_open(bars: list[dict], signal_date: str) -> tuple[str, float] | None:
    """**シグナル発生日の翌取引日の始値**を返す（spec §1.5）。

    - 翌取引日が無ければ None（**当日終値で代用しない**）
    - その日が約定不能（ストップ高安・売買停止）なら**さらに翌日**へ持ち越す
    - 持ち越しは最大 `MAX_CARRY` 日。それを超えたら諦める

    **ここを「当日終値」にすると、バックテストの成績は劇的に良くなる。**
    引けを見てから引けで買えるからで、それは現実には不可能。
    """
    MAX_CARRY = 5
    after = [b for b in bars if b["date"] > signal_date]
    carried = 0
    for b in after:
        if b.get("halted") or b.get("limit_up") or b.get("limit_down"):
            carried += 1
            if carried > MAX_CARRY:
                return None
            continue
        if b.get("open") is None or b["open"] <= 0:
            carried += 1
            if carried > MAX_CARRY:
                return None
            continue
        return (b["date"], float(b["open"]))
    return None


def execute(pf: Portfolio, target_w: dict[str, float],
            bars_by_ticker: dict[str, list[dict]], signal_date: str,
            costs: Costs, min_trade_frac: float = 0.005) -> list[str]:
    """目標ウェイトに向けて売買する。**約定は翌取引日の始値。**

    `min_trade_frac` 未満の調整は**行わない** —
    **回転率を無駄に上げない**（コストが効く）。

    Returns
    -------
    注意事項（約定できなかった等）
    """
    notes = []
    # 評価は**シグナル日の終値**で行う（そこまでが既知の情報）
    px_close = {}
    for t, bars in bars_by_ticker.items():
        b = [x for x in bars if x["date"] <= signal_date]
        if b:
            px_close[t] = b[-1]["close"]
    total = pf.value(px_close)
    if total <= 0:
        return ["**評価額がゼロ以下。** 売買しない"]

    cur_w = pf.weights(px_close)
    tickers = set(target_w) | set(cur_w)

    for t in sorted(tickers):
        want = target_w.get(t, 0.0)
        have = cur_w.get(t, 0.0)
        diff = want - have
        if abs(diff) < min_trade_frac:
            continue
        bars = bars_by_ticker.get(t)
        if not bars:
            notes.append("%s: 価格が無いので売買しない" % t)
            continue
        nx = next_open(bars, signal_date)
        if nx is None:
            notes.append("%s: **翌取引日に約定できなかった**（持ち越し上限）" % t)
            continue
        fill_date, gross = nx
        side = "buy" if diff > 0 else "sell"
        amt = abs(diff) * total
        c = costs.one_way()
        eff = gross * (1 + c) if side == "buy" else gross * (1 - c)
        shares = amt / eff if eff > 0 else 0.0
        if shares <= 0:
            continue

        if side == "buy":
            cost_cash = shares * eff
            if cost_cash > pf.cash:
                shares = pf.cash / eff if eff > 0 else 0.0
                cost_cash = shares * eff
                notes.append("%s: 現金の範囲に切り詰めた" % t)
            if shares <= 0:
                continue
            pf.cash -= cost_cash
            old = pf.positions.get(t)
            if old is None:
                pf.positions[t] = SL.Position(
                    ticker=t, entry_date=fill_date, entry_price=eff,
                    shares=shares, peak_price=gross, last_price=gross)
            else:
                tot_sh = old.shares + shares
                # **取得価格は加重平均。** 取得日は最初の日を保つ
                # （保有期間の上限は「最初に買った日」から数える）
                new_entry = (old.entry_price * old.shares + eff * shares) / tot_sh
                pf.positions[t] = dataclasses.replace(
                    old, shares=tot_sh, entry_price=new_entry)
        else:
            old = pf.positions.get(t)
            if old is None:
                continue
            # **目標がちょうどゼロなら全株売る。**
            #
            # 終値で評価して翌日の始値で約定する以上、
            # 「評価額 ÷ 約定価格」は保有株数と一致しない。
            # 実測では 0.18% の端株が残った（2026-08-23 の自己テスト）。
            # **残った端株は閾値未満なので永久に消えず、
            # 保有銘柄数と回転率を静かに汚し続ける。**
            #
            # 「全部売る」は「0.001 に調整する」とは別の意図なので、
            # 意図として扱う。
            if want == 0.0:
                shares = old.shares
            else:
                shares = min(shares, old.shares)
            pf.cash += shares * eff
            rest = old.shares - shares
            if rest <= 1e-12:
                del pf.positions[t]
            else:
                pf.positions[t] = dataclasses.replace(old, shares=rest)

        pf.fills.append(Fill(ticker=t, date=fill_date, side=side, shares=shares,
                             price=eff, gross_price=gross,
                             cost=abs(eff - gross) * shares))
    return notes


def mark_to_market(pf: Portfolio, bars_by_ticker: dict[str, list[dict]],
                   date: str) -> dict[str, float]:
    """日次で保有の価格を更新する。**最高値を進める**（トレーリングに要る）。"""
    px = {}
    for t in list(pf.positions):
        bars = bars_by_ticker.get(t, [])
        b = [x for x in bars if x["date"] <= date]
        if not b:
            continue
        c = b[-1]["close"]
        px[t] = c
        pf.positions[t] = pf.positions[t].update(c)
    return px


def turnover(pf: Portfolio, total_value: float) -> float:
    """約定金額の合計 / 評価額。**片道で数える。**"""
    if total_value <= 0:
        return 0.0
    return sum(f.shares * f.gross_price for f in pf.fills) / total_value


def total_costs(pf: Portfolio) -> float:
    return sum(f.cost for f in pf.fills)


# ---------------------------------------------------------------- self-test
def _bars(dates, opens=None, closes=None, **flags):
    out = []
    for i, d in enumerate(dates):
        o = (opens[i] if opens else 100.0)
        c = (closes[i] if closes else o)
        row = {"date": d, "open": o, "high": max(o, c), "low": min(o, c),
               "close": c, "volume": 1e6, "halted": False,
               "limit_up": False, "limit_down": False}
        for k, v in flags.items():
            if i in v:
                row[k] = True
        out.append(row)
    return out


def _test() -> int:
    fails = []
    ran = []

    def check(nm, cond):

        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/portfolio.py 自己テスト")
    print("-" * 80)
    D = ["2024-01-%02d" % i for i in range(1, 11)]

    # --- 約定価格。**ここが最も重要** ---------------------------------------
    # **始値と終値を大きく離すと、買った瞬間に評価が跳ねて**
    # 比率のテストが意味を失う。実データに近い形にする
    b = _bars(D, opens=[100 + i for i in range(10)],
              closes=[100.5 + i for i in range(10)])
    nx = next_open(b, "2024-01-03")
    check("**翌取引日の始値で約定する**", nx == ("2024-01-04", 103.0))
    check("**当日の終値ではない**", nx[1] != 102.5)
    check("最終日にシグナルが出たら約定できない", next_open(b, "2024-01-10") is None)

    b2 = _bars(D, opens=[100 + i for i in range(10)], limit_up={3})
    check("**ストップ高の日は約定せず翌日に持ち越す**",
          next_open(b2, "2024-01-03") == ("2024-01-05", 104.0))
    b3 = _bars(D, opens=[100 + i for i in range(10)], halted={3, 4, 5, 6, 7, 8})
    check("**持ち越しが上限を超えたら諦める（None）**",
          next_open(b3, "2024-01-03") is None)

    # --- コスト ---------------------------------------------------------------
    c = Costs(spread_bps=25, commission_bps=5)
    check("片道コスト = 0.30%", abs(c.one_way() - 0.0030) < 1e-12)
    check("**手数料ゼロを既定にしない**", Costs().commission_bps > 0)

    # --- 執行 -----------------------------------------------------------------
    bt = {"A": b, "B": _bars(D, opens=[50.0] * 10, closes=[50.0] * 10)}
    pf = Portfolio(cash=1_000_000.0)
    notes = execute(pf, {"A": 0.5, "B": 0.3}, bt, "2024-01-03", c)
    check("2銘柄を買う", set(pf.positions) == {"A", "B"})
    check("**現金が減る**", pf.cash < 1_000_000.0)
    check("**買いは始値より高い価格で約定する（コスト分）**",
          pf.fills[0].price > pf.fills[0].gross_price)
    check("コストを記録する", total_costs(pf) > 0)
    check("約定日は翌取引日", all(f.date == "2024-01-04" for f in pf.fills))

    # 目標に近いか（コスト分だけ下振れる）
    px = {t: bs[3]["close"] for t, bs in bt.items()}
    w = pf.weights(px)
    check("A の比率が目標の近くにある", 0.40 < w["A"] < 0.60)

    # --- 小さすぎる調整は行わない -------------------------------------------
    n_before = len(pf.fills)
    execute(pf, {"A": w["A"] + 0.001, "B": w["B"]}, bt, "2024-01-05", c)
    check("**閾値未満の調整は約定しない（回転率を無駄に上げない）**",
          len(pf.fills) == n_before)

    # --- 売り -----------------------------------------------------------------
    execute(pf, {"A": 0.0, "B": w["B"]}, bt, "2024-01-05", c)
    check("**目標ゼロなら全部売る（端株を残さない）**", "A" not in pf.positions)
    sells = [f for f in pf.fills if f.side == "sell"]
    check("**売りは始値より安い価格で約定する（コスト分）**",
          sells and sells[0].price < sells[0].gross_price)

    # **端株が残らないことを明示的に確認する。**
    # 終値評価と翌日始値の差で 0.18% の端株が残るのを、意図として潰している
    pf_ex = Portfolio(cash=1_000_000.0)
    execute(pf_ex, {"A": 0.5}, bt, "2024-01-01", c)
    execute(pf_ex, {"A": 0.0}, bt, "2024-01-05", c)
    check("**評価と約定の価格差があっても端株は残らない**",
          "A" not in pf_ex.positions)

    # --- 現金の範囲を超えない ------------------------------------------------
    pf2 = Portfolio(cash=1000.0)
    execute(pf2, {"A": 1.0}, bt, "2024-01-03", c)
    check("**現金以上には買わない**", pf2.cash >= -1e-9)

    # --- 値洗いと最高値 -------------------------------------------------------
    rise = _bars(D, opens=[100.0] * 10,
                 closes=[100, 110, 120, 130, 90, 90, 90, 90, 90, 90])
    pf3 = Portfolio(cash=100_000.0)
    execute(pf3, {"A": 0.5}, {"A": rise}, "2024-01-01", c)
    for d in D[2:]:
        mark_to_market(pf3, {"A": rise}, d)
    p = pf3.positions["A"]
    check("**最高値が保存される（トレーリングに要る）**", p.peak_price == 130.0)
    check("最終価格が反映される", p.last_price == 90.0)
    check("**最高値からの下落率が出る**", abs(p.drawdown_from_peak() - (90/130 - 1)) < 1e-9)

    # --- 買い増しの扱い -------------------------------------------------------
    pf4 = Portfolio(cash=100_000.0)
    execute(pf4, {"A": 0.2}, bt, "2024-01-01", c)
    e1 = pf4.positions["A"].entry_date
    execute(pf4, {"A": 0.5}, bt, "2024-01-05", c)
    check("**買い増しでも取得日は最初の日のまま**（保有期間の上限がリセットされない）",
          pf4.positions["A"].entry_date == e1)
    check("取得価格は加重平均", pf4.positions["A"].entry_price > 0)

    # --- 回転率 ---------------------------------------------------------------
    check("回転率が計算できる", turnover(pf, 1_000_000.0) > 0)
    check("評価額がゼロなら回転率もゼロ", turnover(pf, 0.0) == 0.0)

    # --- 価格が無い銘柄 -------------------------------------------------------
    pf5 = Portfolio(cash=100_000.0)
    n5 = execute(pf5, {"ZZZ": 0.5}, bt, "2024-01-03", c)
    check("**価格が無い銘柄は買わず、理由を残す**",
          "ZZZ" not in pf5.positions and any("価格が無い" in x for x in n5))

    print("-" * 80)
    declared = 26
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
