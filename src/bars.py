#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
価格・出来高の正規化層（Phase 0 の土台）。

docs/02_definition_spec.md §3 で決めた規約を**コードで一箇所に固定する。**
ここを通さずに生の価格を触ると、必ずどこかで規約が破られる。

§3 で決めたこと（この実装が守るもの）
------------------------------------
| 規約 | 実装 |
|---|---|
| 調整価格は分割・併合で遡及調整。**現金配当では調整しない** | `adjust()` が split 係数のみを使う |
| トータルリターンは**別系列**で持つ | `total_return` を別カラムにする |
| リターンは `ln(P_t/P_{t-1})` | `log_return()` |
| **出来高も分割調整する**（忘れると J01/J09 が飛ぶ） | `adjust()` が volume を逆数倍する |
| 売買停止日のリターンは **0 ではなく欠損** | `log_return()` が volume==0 を NaN にする |
| ストップ高安は約定不能フラグを立てる | `limit_flag()` |
| 上場廃止は最終価格と**廃止事由**を記録。**一律 -100% は誤り** | `DelistReason` |
| 通貨は現地通貨で保存。円換算は集計時のみ | `to_jpy()` を明示的に呼ぶまで換算しない |

**なぜ 0 ではなく欠損か**（§3 と J18 の検証で確定した）
--------------------------------------------------
売買停止日に 0 を入れるとボラティリティが過小評価される。
過小評価された I01 は Kelly の分母（§1.8）を小さくするので、
**ポジションが過大になる。** 静かに危険側へ倒れる誤りなので、
ここで構造的に防ぐ。

自己テスト
    python src/bars.py
"""
from __future__ import annotations

import dataclasses
import enum
import math
from typing import Iterable, Sequence


class DelistReason(enum.Enum):
    """上場廃止の事由。**一律 -100% は誤り。**

    docs/03_data_feasibility.md §7.0（DF-01）の実測では、
    **買収による廃止は無料の価格データから 0/15 = 0% しか取れなかった。**
    取れないことと、価値がゼロになったことは違う。
    """

    ACQUISITION = "acquisition"      # TOB・合併。**株主は対価を受け取る（プラスのことが多い）**
    BANKRUPTCY = "bankruptcy"        # 破綻。概ね -100%
    GOING_PRIVATE = "going_private"  # MBO 等。対価あり
    MERGER_SHARE = "merger_share"    # 株式交換。**存続会社の株に変わるので継続する**
    STANDARD_FAIL = "standard_fail"  # 上場維持基準抵触
    UNKNOWN = "unknown"              # **不明を「破綻」に丸めない**

    @property
    def terminal_return(self) -> float | None:
        """廃止時のリターン。**None は「別途調べる必要がある」という意味。**

        ここで安易に数字を返すと、バックテストが静かに嘘になる。
        """
        if self is DelistReason.BANKRUPTCY:
            return -1.0
        return None      # 買収・MBO・株式交換は対価次第。UNKNOWN も同様


@dataclasses.dataclass(frozen=True)
class Bar:
    """1銘柄・1日の生データ。**調整前の値をそのまま持つ。**"""

    date: str                 # ISO 8601。取引所のローカル取引日
    open: float
    high: float
    low: float
    close: float
    volume: float             # 株数
    split_factor: float = 1.0  # その日に適用された分割比率（2分割なら 2.0）
    dividend: float = 0.0      # 1株あたり現金配当（**価格調整には使わない**）
    halted: bool = False       # 売買停止
    limit_up: bool = False     # ストップ高
    limit_down: bool = False   # ストップ安


def cumulative_split(bars: Sequence[Bar]) -> list[float]:
    """各日の「その日以降に起きる分割の累積」を返す。

    調整価格 = 生価格 / （その日より後に起きた分割の累積）
    **後ろから累積する**のがポイントで、前から掛けると最新日が動いてしまう。
    """
    out = [1.0] * len(bars)
    acc = 1.0
    for i in range(len(bars) - 1, -1, -1):
        out[i] = acc
        acc *= bars[i].split_factor
    return out


def adjust(bars: Sequence[Bar]) -> list[dict]:
    """分割・併合の遡及調整を行う。**現金配当では調整しない**（§3）。

    出来高は価格と**逆向き**に調整する。
    2分割なら価格は 1/2、株数は 2倍になるので、
    調整後の出来高は「調整後株数ベース」に揃える。
    忘れると J01（売買代金）と J09（出来高ショック）が分割日に飛ぶ。
    """
    cum = cumulative_split(bars)
    rows = []
    for b, c in zip(bars, cum):
        rows.append({
            "date": b.date,
            "open": b.open / c,
            "high": b.high / c,
            "low": b.low / c,
            "close": b.close / c,
            "volume": b.volume * c,          # ← 価格と逆向き
            "turnover": (b.close / c) * (b.volume * c),   # = 生の売買代金。調整で不変
            "dividend": b.dividend,
            "halted": b.halted,
            "limit_up": b.limit_up,
            "limit_down": b.limit_down,
        })
    return rows


def log_return(rows: Sequence[dict]) -> list[float | None]:
    """`r_t = ln(P_t / P_{t-1})`。

    **売買停止日と出来高ゼロ日は 0 ではなく None（欠損）にする。**
    0 を入れるとボラティリティが過小評価され、
    Kelly の分母が小さくなってポジションが過大になる（§1.8）。
    """
    out: list[float | None] = [None]
    for prev, cur in zip(rows, rows[1:]):
        if cur["halted"] or cur["volume"] <= 0 or prev["close"] <= 0 or cur["close"] <= 0:
            out.append(None)
        else:
            out.append(math.log(cur["close"] / prev["close"]))
    return out


def total_return(rows: Sequence[dict]) -> list[float | None]:
    """配当込みリターン。**価格系列とは別に持つ**（§3）。

    S16（税）を引く前の値。税引後は S カテゴリ側で処理する。
    """
    out: list[float | None] = [None]
    for prev, cur in zip(rows, rows[1:]):
        if cur["halted"] or cur["volume"] <= 0 or prev["close"] <= 0:
            out.append(None)
        else:
            out.append(math.log((cur["close"] + cur["dividend"]) / prev["close"]))
    return out


def tradable(rows: Sequence[dict]) -> list[bool]:
    """その日に**約定できたか**。

    ストップ高で買えず、ストップ安で売れないのを明示する。
    シミュレータはこれを見て翌営業日に持ち越す（§3、J11/J18）。
    """
    return [not (r["halted"] or r["limit_up"] or r["limit_down"]) for r in rows]


def adv(rows: Sequence[dict], window: int = 20) -> list[float | None]:
    """平均売買代金。J01 のゲート（600万円）の入力。

    **出来高ゼロ日を除外せずに平均する。** ゼロ日があること自体が
    流動性の低さなので、除外すると実態より良く見える。
    """
    out: list[float | None] = []
    for i in range(len(rows)):
        if i + 1 < window:
            out.append(None)
            continue
        w = rows[i + 1 - window: i + 1]
        out.append(sum(x["turnover"] for x in w) / window)
    return out


def zero_volume_days(rows: Sequence[dict], window: int = 60) -> list[int | None]:
    """直近 window 日の出来高ゼロ日数。J10 のゲート（0日）の入力。"""
    out: list[int | None] = []
    for i in range(len(rows)):
        if i + 1 < window:
            out.append(None)
            continue
        w = rows[i + 1 - window: i + 1]
        out.append(sum(1 for x in w if x["volume"] <= 0))
    return out


def detect_split_misadjustment(rows: Sequence[dict], bars: Sequence[Bar],
                               tol: float = 0.25) -> list[dict]:
    """**分割の二重調整・未調整を検出する。**

    調整が正しければ、分割日をまたぐリターンは通常の日と変わらない。
    誤っていれば `|r| ≈ ln(split_factor)` になる。

    なぜ要るか
    ----------
    **データ源によって「分割調整済みか」が違い、しかも明記されていない。**
    実測（2026-08-23）: yfinance は `auto_adjust=False` を指定しても
    **分割については既に調整済みの価格を返す**（配当だけ未調整）。
    それに気づかず bars.adjust() を掛けると**二重調整**になり、
    5分割なら分割日に +161%（= ln 5）のリターンが立つ。

    これは spec §8 の「バックテストでは検出されず成績を良く見せる方向に効く」
    誤りそのものである — **モメンタム（G）は偽の急騰を買い、
    リバーサル（H）は偽の急落を買う。**

    → **新しいデータ源を繋いだら必ずこれを通す。**
    """
    out = []
    for i, b in enumerate(bars):
        if b.split_factor == 1.0 or i == 0:
            continue
        prev, cur = rows[i - 1], rows[i]
        if prev["close"] <= 0 or cur["close"] <= 0:
            continue
        r = math.log(cur["close"] / prev["close"])
        expected = math.log(b.split_factor)
        if abs(abs(r) - expected) < tol:
            out.append({
                "date": b.date, "split_factor": b.split_factor,
                "log_return": r,
                "diagnosis": ("**二重調整**（データ源が既に分割調整済みなのに"
                              "さらに調整した）" if r > 0 else
                              "**未調整**（split_factor が渡されていない）"),
            })
    return out


def to_jpy(values: Iterable[float | None], fx: Iterable[float | None]) -> list[float | None]:
    """円換算。**集計・比較の時にだけ呼ぶ**（§5）。

    保存は現地通貨で行う。ここを通さずに統合ランクを作ると、
    J19（日米統合の時価総額分位）が壊れる。
    """
    out: list[float | None] = []
    for v, r in zip(values, fx):
        out.append(None if (v is None or r is None) else v * r)
    return out


# ---------------------------------------------------------------- self-test
def _bar(d, c, v=1000.0, **kw) -> Bar:
    return Bar(date=d, open=c, high=c, low=c, close=c, volume=v, **kw)


def _test() -> int:
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)
        print("  %-52s %s" % (name, "OK" if cond else "**FAIL**"))

    print("src/bars.py 自己テスト")
    print("-" * 66)

    # 1) 分割調整: 3日目に2分割。それ以前の価格は半分になる
    bars = [_bar("2026-01-05", 1000.0), _bar("2026-01-06", 1010.0),
            _bar("2026-01-07", 505.0, split_factor=2.0), _bar("2026-01-08", 510.0)]
    a = adjust(bars)
    check("分割前の価格が 1/2 に調整される", abs(a[0]["close"] - 500.0) < 1e-9)
    check("分割後の価格は変わらない", abs(a[3]["close"] - 510.0) < 1e-9)
    check("**出来高が価格と逆向きに調整される**", abs(a[0]["volume"] - 2000.0) < 1e-9)
    check("売買代金は調整で不変", abs(a[0]["turnover"] - 1000.0 * 1000.0) < 1e-6)

    r = log_return(a)
    check("分割日をまたぐリターンが飛ばない（|r| < 1%）", abs(r[2]) < 0.01)

    # 2) 現金配当では価格調整しない
    bars2 = [_bar("2026-03-30", 1000.0), _bar("2026-03-31", 980.0, dividend=20.0)]
    a2 = adjust(bars2)
    check("**現金配当では価格を調整しない**", abs(a2[1]["close"] - 980.0) < 1e-9)
    tr = total_return(a2)
    check("配当込みリターンは別系列で ~0 になる", abs(tr[1]) < 1e-6)
    pr = log_return(a2)
    check("価格リターンは配当落ち分だけ負", pr[1] < -0.01)

    # 3) 売買停止は 0 ではなく欠損
    bars3 = [_bar("2026-01-05", 1000.0), _bar("2026-01-06", 1000.0, v=0.0, halted=True),
             _bar("2026-01-07", 900.0)]
    a3 = adjust(bars3)
    r3 = log_return(a3)
    check("**売買停止日のリターンは 0 ではなく欠損**", r3[1] is None)
    check("停止明けのリターンは計算される", r3[2] is not None)
    check("停止日は約定不能", tradable(a3)[1] is False)

    # 4) ストップ高安
    bars4 = [_bar("2026-01-05", 1000.0), _bar("2026-01-06", 1300.0, limit_up=True)]
    check("ストップ高は約定不能", tradable(adjust(bars4))[1] is False)

    # 5) 上場廃止事由
    check("**破綻は -100%**", DelistReason.BANKRUPTCY.terminal_return == -1.0)
    check("**買収は None（対価を別途調べる）**",
          DelistReason.ACQUISITION.terminal_return is None)
    check("不明を破綻に丸めない", DelistReason.UNKNOWN.terminal_return is None)

    # 6) 流動性ゲートの入力
    rows = adjust([_bar("d%02d" % i, 100.0, v=(0.0 if i in (3, 7) else 10000.0))
                   for i in range(60)])
    check("出来高ゼロ日を数える（J10）", zero_volume_days(rows, 60)[-1] == 2)
    check("**ゼロ日を除外せずに平均する（J01）**",
          adv(rows, 20)[-1] == sum(x["turnover"] for x in rows[-20:]) / 20)
    check("窓が足りない期間は None", adv(rows, 20)[0] is None)

    # 7) 円換算は明示的に呼ぶまで起きない
    check("円換算", to_jpy([10.0, None], [150.0, 150.0]) == [1500.0, None])

    # 8) 分割の二重調整を検出する（実データで踏んだ罠、2026-08-23）
    #    データ源が既に分割調整済みなのに split_factor を渡すと二重になる
    already = [_bar("2026-01-05", 2848.0), _bar("2026-01-06", 2861.0, split_factor=5.0)]
    d = detect_split_misadjustment(adjust(already), already)
    check("**二重調整を検出する**", len(d) == 1 and "二重調整" in d[0]["diagnosis"])
    # 正しく生価格が渡っていれば検出されない
    proper = [_bar("2026-01-05", 14240.0), _bar("2026-01-06", 2861.0, split_factor=5.0)]
    check("正しい生価格では検出しない",
          detect_split_misadjustment(adjust(proper), proper) == [])

    print("-" * 66)
    print("%d/%d 通過" % (24 - len(fails), 24))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
