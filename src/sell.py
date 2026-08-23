#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
売り判定 — **機械的な損失フィルタ。**

docs/05_backtest_protocol.md §4。

なぜ売りが特に重要か
--------------------
- **買いは見送れるが、売りは見送れない。** 保有している限り毎日判断が要る
- 263AT の設計は「9つが負けても1つで賄う」なので、
  **9つの負けを小さく抑えることが1つの勝ちと同じくらい効く**
- Bessembinder（4%の銘柄が全リターンを作る）は、
  **裏を返せば96%を早く手放す必要がある**ということ

この層の規約
------------
1. **閾値を埋め込まない。** すべて `SellRules` に外出しし、最適化対象にする
2. **ゲート違反と流動性消失は最適化しない。** 踏んではいけない地雷であって
   リスク許容度の問題ではない（§1.6 で rho を監査意見に効かせないと決めたのと同じ）
3. **上から順に評価し、最初に該当したもので売る。** 理由を1つに定める
4. **損切りは自明に良いものではない。** 「無し」を選べるようにし、
   **最適化で「無し」が選ばれたらそれを受け入れる**（§4.3）

自己テスト
    python src/sell.py
"""
from __future__ import annotations

import dataclasses
import enum


# **閾値の境界を浮動小数点の誤差で決めさせない。**
# 取得価格 100 → 現在 80 のとき `80/100 - 1 = -0.19999999999999996` になり、
# 閾値 -0.20 との比較が「わずかに上」になって損切りが発動しない。
# 実際に自己テストで踏んだ（2026-08-23）。
#
# **リターンの 1e-9 は経済的には無意味だが、判定を反転させる。**
# 「ちょうど閾値」は発動する側に倒す（損切りは早い方が安全側）。
EPS = 1e-9


class SellReason(enum.Enum):
    """売る理由。**1つに定める**（複数該当しても最初の1つ）。"""

    GATE_VIOLATION = "ゲート違反（継続企業の前提 / 監査意見 / 整理監理）"
    LIQUIDITY_LOST = "流動性の消失（J01 を下回った）"
    STOP_LOSS = "損切り（取得来の下落が閾値を超えた）"
    TRAILING_STOP = "トレーリング（最高値からの下落が閾値を超えた）"
    TIME_STOP = "時間切れ（保有期間の上限）"
    SCORE_DECAY = "スコア劣化（買いスコアが下位分位に落ちた）"
    EARLY_WARNING = "早期警戒の複合（運転資本の悪化 + 破綻確率の上昇）"
    HOLD = "保有継続"


@dataclasses.dataclass(frozen=True)
class SellRules:
    """売りルールの閾値。**すべて最適化対象**（ゲート系を除く）。

    `None` は「そのルールを使わない」。
    **「使わない」を選べることが重要** — 損切りは自明に良いものではない（§4.3）。
    """

    stop_loss: float | None = None        # 取得来リターンがこれを下回ったら売る（負の値）
    trailing_stop: float | None = None    # 最高値からの下落率（負の値）
    max_hold_years: float | None = None   # 保有期間の上限
    score_quantile: float | None = None   # 買いスコアがこの分位を下回ったら売る（0-1）
    warn_count: int | None = None         # 早期警戒がこの数以上同時に点灯したら売る

    def __post_init__(self):
        for name in ("stop_loss", "trailing_stop"):
            v = getattr(self, name)
            if v is not None and v >= 0:
                raise ValueError("%s は負の値で指定する（例: -0.20）" % name)
        if self.score_quantile is not None and not 0.0 <= self.score_quantile <= 1.0:
            raise ValueError("score_quantile は 0〜1")
        if self.max_hold_years is not None and self.max_hold_years <= 0:
            raise ValueError("max_hold_years は正")

    @staticmethod
    def grid() -> list["SellRules"]:
        """**粗い格子。** 損切り −8% と −8.5% を区別できるほどのデータは無い（§2.3）。

        **「無し」を必ず含める。**
        """
        out = []
        for sl in (None, -0.10, -0.15, -0.20, -0.25, -0.30):
            for tr in (None, -0.20, -0.30):
                for yr in (None, 1.0, 3.0, 5.0):
                    out.append(SellRules(stop_loss=sl, trailing_stop=tr,
                                         max_hold_years=yr))
        return out

    def at_grid_edge(self) -> list[str]:
        """**選ばれた値が格子の端なら警告する。** 範囲外に最適があるサイン（§2.3）。"""
        edges = []
        if self.stop_loss == -0.30:
            edges.append("stop_loss が格子の端（-0.30）。もっと広い損切りが良い可能性")
        if self.stop_loss == -0.10:
            edges.append("stop_loss が格子の端（-0.10）。もっと狭い損切りが良い可能性")
        if self.trailing_stop == -0.30:
            edges.append("trailing_stop が格子の端（-0.30）")
        if self.max_hold_years == 5.0:
            edges.append("max_hold_years が格子の端（5年）。保有期間の上限は 263AT の"
                         "設計上限でもあるので、これは想定内")
        return edges


@dataclasses.dataclass
class Position:
    """保有状態。**取得来と最高値の両方を持つ。**

    最高値を持たないとトレーリングが作れない。
    **保有中に毎日更新する必要がある** — 断面だけでは作れない情報。
    """

    ticker: str
    entry_date: str
    entry_price: float
    shares: float
    peak_price: float          # 保有開始以降の最高値（**調整後価格で**）
    last_price: float
    entry_score_rank: float | None = None    # 買った時点の断面順位（0-1）

    def total_return(self) -> float:
        return self.last_price / self.entry_price - 1.0 if self.entry_price > 0 else 0.0

    def drawdown_from_peak(self) -> float:
        return self.last_price / self.peak_price - 1.0 if self.peak_price > 0 else 0.0

    def hold_years(self, today: str) -> float:
        import datetime as dt
        a = dt.date.fromisoformat(self.entry_date[:10])
        b = dt.date.fromisoformat(today[:10])
        return (b - a).days / 365.25

    def update(self, price: float) -> "Position":
        """日次で価格を反映する。**最高値は下がらない。**"""
        return dataclasses.replace(self, last_price=price,
                                   peak_price=max(self.peak_price, price))


@dataclasses.dataclass(frozen=True)
class MarketState:
    """売り判定に要る、その銘柄の当日の状態。

    **すべて「その日までに入手できる情報」で埋める。**
    埋められないものは None にし、**None のときはそのルールを発動させない**
    （情報が無いことを理由に売らない）。
    """

    gate_violation: bool = False       # D13 / E22 / N23 のいずれか
    liquidity_ok: bool = True          # J01 を満たしているか
    score_rank: float | None = None    # 当日の買いスコアの断面順位（0-1、1が最良）
    warnings: int = 0                  # 早期警戒の点灯数（B22/B23/B35/D17 等）


def decide(pos: Position, mkt: MarketState, rules: SellRules,
           today: str) -> tuple[SellReason, str]:
    """売るかどうかを決める。**上から順に評価し、最初に該当したもので売る。**

    Returns
    -------
    (理由, 説明)。`SellReason.HOLD` なら保有継続。
    """
    # --- 1. ゲート違反。**最適化しない** -------------------------------------
    if mkt.gate_violation:
        return (SellReason.GATE_VIOLATION,
                "継続企業の前提・監査意見・整理監理のいずれかに該当")

    # --- 2. 流動性の消失。**最適化しない** -----------------------------------
    if not mkt.liquidity_ok:
        return (SellReason.LIQUIDITY_LOST,
                "売買代金が下限を下回った。**売れなくなる前に売る**")

    # --- 3. 損切り -----------------------------------------------------------
    if rules.stop_loss is not None:
        r = pos.total_return()
        if r <= rules.stop_loss + EPS:
            return (SellReason.STOP_LOSS,
                    "取得来 %+.1f%% が閾値 %+.1f%% を下回った"
                    % (100 * r, 100 * rules.stop_loss))

    # --- 4. トレーリング -----------------------------------------------------
    if rules.trailing_stop is not None:
        d = pos.drawdown_from_peak()
        if d <= rules.trailing_stop + EPS:
            return (SellReason.TRAILING_STOP,
                    "最高値から %+.1f%% が閾値 %+.1f%% を下回った"
                    % (100 * d, 100 * rules.trailing_stop))

    # --- 5. 時間切れ ---------------------------------------------------------
    if rules.max_hold_years is not None:
        y = pos.hold_years(today)
        if y >= rules.max_hold_years - EPS:
            return (SellReason.TIME_STOP,
                    "保有 %.1f 年が上限 %.1f 年に達した" % (y, rules.max_hold_years))

    # --- 6. スコア劣化 -------------------------------------------------------
    # **score_rank が None なら発動しない。** 情報が無いことを理由に売らない
    if rules.score_quantile is not None and mkt.score_rank is not None:
        if mkt.score_rank <= rules.score_quantile + EPS:
            return (SellReason.SCORE_DECAY,
                    "買いスコアの順位 %.2f が下位 %.2f 分位に落ちた"
                    % (mkt.score_rank, rules.score_quantile))

    # --- 7. 早期警戒の複合 ---------------------------------------------------
    if rules.warn_count is not None and mkt.warnings >= rules.warn_count:
        return (SellReason.EARLY_WARNING,
                "早期警戒が %d 件点灯（閾値 %d）" % (mkt.warnings, rules.warn_count))

    return (SellReason.HOLD, "")


# ---------------------------------------------------------------- self-test
def _pos(**kw) -> Position:
    base = dict(ticker="T", entry_date="2024-01-01", entry_price=100.0,
                shares=10.0, peak_price=100.0, last_price=100.0)
    base.update(kw)
    return Position(**base)


def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-64s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/sell.py 自己テスト")
    print("-" * 78)
    T = "2024-07-01"
    none_rules = SellRules()

    # ゲートと流動性は**ルールが空でも**発動する
    check("**ゲート違反は閾値と無関係に売る**",
          decide(_pos(), MarketState(gate_violation=True), none_rules, T)[0]
          is SellReason.GATE_VIOLATION)
    check("**流動性の消失も閾値と無関係に売る**",
          decide(_pos(), MarketState(liquidity_ok=False), none_rules, T)[0]
          is SellReason.LIQUIDITY_LOST)
    check("ゲートが流動性より優先される",
          decide(_pos(), MarketState(gate_violation=True, liquidity_ok=False),
                 none_rules, T)[0] is SellReason.GATE_VIOLATION)

    # 何もルールが無ければ保有継続
    check("**ルールが空なら大きく下げても保有継続**",
          decide(_pos(last_price=30.0), MarketState(), none_rules, T)[0]
          is SellReason.HOLD)

    # 損切り
    r_sl = SellRules(stop_loss=-0.20)
    check("−25% で損切りが発動", decide(_pos(last_price=75.0), MarketState(), r_sl, T)[0]
          is SellReason.STOP_LOSS)
    check("−15% では発動しない", decide(_pos(last_price=85.0), MarketState(), r_sl, T)[0]
          is SellReason.HOLD)
    check("ちょうど −20% で発動（境界を含む）",
          decide(_pos(last_price=80.0), MarketState(), r_sl, T)[0]
          is SellReason.STOP_LOSS)

    check("**境界は浮動小数点の誤差で決めない**",
          abs(_pos(last_price=80.0).total_return() - (-0.20)) < 1e-9
          and _pos(last_price=80.0).total_return() != -0.20)

    # トレーリング
    r_tr = SellRules(trailing_stop=-0.30)
    p = _pos(peak_price=200.0, last_price=130.0)      # 最高値から −35%
    check("**最高値からの下落でトレーリングが発動**",
          decide(p, MarketState(), r_tr, T)[0] is SellReason.TRAILING_STOP)
    check("**取得来は +30% でも売る**（利益が乗っていても関係ない）",
          p.total_return() > 0.29)
    p2 = _pos(peak_price=200.0, last_price=150.0)     # −25%
    check("閾値内なら保有継続", decide(p2, MarketState(), r_tr, T)[0] is SellReason.HOLD)

    # 最高値の更新
    p3 = _pos().update(150.0).update(120.0)
    check("**最高値は下がらない**", p3.peak_price == 150.0 and p3.last_price == 120.0)

    # 時間切れ
    r_t = SellRules(max_hold_years=1.0)
    check("1年経てば時間切れ",
          decide(_pos(entry_date="2023-01-01"), MarketState(), r_t, T)[0]
          is SellReason.TIME_STOP)
    check("半年なら継続",
          decide(_pos(entry_date="2024-03-01"), MarketState(), r_t, T)[0]
          is SellReason.HOLD)

    # スコア劣化
    r_s = SellRules(score_quantile=0.2)
    check("下位2割に落ちたら売る",
          decide(_pos(), MarketState(score_rank=0.1), r_s, T)[0]
          is SellReason.SCORE_DECAY)
    check("**スコアが取れなければ発動しない（情報が無いことを理由に売らない）**",
          decide(_pos(), MarketState(score_rank=None), r_s, T)[0] is SellReason.HOLD)

    # 早期警戒
    r_w = SellRules(warn_count=3)
    check("警戒3件で売る", decide(_pos(), MarketState(warnings=3), r_w, T)[0]
          is SellReason.EARLY_WARNING)
    check("2件なら継続", decide(_pos(), MarketState(warnings=2), r_w, T)[0]
          is SellReason.HOLD)

    # 優先順位
    both = SellRules(stop_loss=-0.10, trailing_stop=-0.05)
    check("**損切りがトレーリングより先に評価される**",
          decide(_pos(peak_price=200.0, last_price=80.0), MarketState(), both, T)[0]
          is SellReason.STOP_LOSS)

    # 閾値の検査
    try:
        SellRules(stop_loss=0.2)
        check("**損切りに正の値を渡したら拒否する**", False)
    except ValueError:
        check("**損切りに正の値を渡したら拒否する**", True)
    try:
        SellRules(score_quantile=1.5)
        check("分位が範囲外なら拒否する", False)
    except ValueError:
        check("分位が範囲外なら拒否する", True)

    # 格子
    g = SellRules.grid()
    check("格子が粗い（%d 通り）" % len(g), 50 <= len(g) <= 100)
    check("**格子に『損切り無し』が含まれる**",
          any(r.stop_loss is None for r in g))
    check("**格子に『全部無し』が含まれる**",
          any(r.stop_loss is None and r.trailing_stop is None
              and r.max_hold_years is None for r in g))
    check("**格子の端を選んだら警告する**",
          len(SellRules(stop_loss=-0.30).at_grid_edge()) > 0)
    check("格子の中央なら警告しない",
          SellRules(stop_loss=-0.20, trailing_stop=-0.20).at_grid_edge() == [])

    print("-" * 78)
    total = 26
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
