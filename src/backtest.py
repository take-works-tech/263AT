#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ウォークフォワード検証 — **生成時点 PIT を構造で強制する。**

docs/05_backtest_protocol.md §1-2。

私が守っていなかったこと
------------------------
PIT には2つの水準がある。

| 水準 | 内容 | 状態 |
|---|---|---|
| **データ時点 PIT** | 時点 t のスコアは `filed <= t` のデータで作る | `facts.AsOf` で実装済み |
| **生成時点 PIT** | 時点 T に**システムを生成**するとき、生成に使う情報も T 以前 | **これが無かった** |

**データが PIT でも、システムの設計が未来を知っていれば検証にならない。**

この層の構造
------------
    for T in 生成日程:
        訓練窓 = [T - L, T)              ← **T を含まない**
        system = generate(訓練窓)         ← 重み・閾値を訓練窓だけで決める
        for t in [T, 次の生成日):
            断面 = score(t, system)       ← filed <= t のデータのみ
            発注 = decide(断面, 保有)
            約定 = t+1 の始値

**`generate()` に渡すのは `TrainingWindow` だけ**で、
それは `[lo, hi)` の外を返さないようにできている。
**未来を渡せない構造にする**のが、この層の唯一の要件である。

**まだ残る漏れ（正直に書く）**
------------------------------
**カタログ自体が 2024年までの OSAP を見て書かれている**（§1.3 of docs/05）。
このエンジンで 2024年以前を回して良い成績が出ても、**それは検証ではない。**

→ **過去検証は「成績を測る」ためではなく
「仕組みが壊れていないかを測る」ために回す**（docs/05 §1.4）。
バグ検出・回転率・コスト・売りルールの挙動は測れる。期待リターンは測れない。

自己テスト
    python src/backtest.py
"""
from __future__ import annotations

import dataclasses
import datetime as dt

import sell as SL      # type: ignore
import sizing as SZ    # type: ignore


class LookaheadError(RuntimeError):
    """**訓練窓の外を見ようとしたときに投げる。**

    黙って未来を使うくらいなら落ちた方が良い。
    """


@dataclasses.dataclass(frozen=True)
class TrainingWindow:
    """`generate()` に渡す唯一の入り口。**窓の外は取れない。**

    `series` は日付をキーにした任意の観測。
    **窓の外を要求したら例外**を投げる（黙って空を返さない）。
    """

    lo: str                       # 含む
    hi: str                       # **含まない**（= 生成日 T）
    _data: dict                   # {date: {key: value}}

    def dates(self) -> list[str]:
        return sorted(d for d in self._data if self.lo <= d < self.hi)

    def get(self, date: str) -> dict:
        """**窓の外なら例外。**"""
        if not (self.lo <= date < self.hi):
            raise LookaheadError(
                "訓練窓 [%s, %s) の外 %s を参照した。**これはルックアヘッドである**"
                % (self.lo, self.hi, date))
        return self._data.get(date, {})

    def series(self, key: str) -> list[tuple[str, float]]:
        """窓の中の系列だけを返す。"""
        return [(d, self._data[d][key]) for d in self.dates()
                if key in self._data[d]]

    def split_inner(self, valid_days: int) -> tuple["TrainingWindow", "TrainingWindow"]:
        """訓練窓をさらに**内側訓練**と**内側検証**に割る（docs/05 §2.2）。

        **内側検証まで使って閾値を選ぶと、訓練窓に過剰適合する。**
        §1.9 で「選択は多重検定そのもの」と書いたのと同じ理屈。
        """
        cut = (dt.date.fromisoformat(self.hi[:10])
               - dt.timedelta(days=valid_days)).isoformat()
        if cut <= self.lo:
            raise ValueError("内側検証が訓練窓より長い")
        return (TrainingWindow(self.lo, cut, self._data),
                TrainingWindow(cut, self.hi, self._data))


@dataclasses.dataclass
class System:
    """時点 T に生成された売買システム。**生成の由来を持ち歩く。**"""

    generated_at: str
    train_lo: str
    train_hi: str
    sell_rules: SL.SellRules
    limits: SZ.RiskLimits
    kelly_fraction: float
    notes: list[str] = dataclasses.field(default_factory=list)

    def provenance(self) -> str:
        return ("生成 %s / 訓練 [%s, %s) / 損切り %s / トレーリング %s / 保有上限 %s"
                % (self.generated_at, self.train_lo, self.train_hi,
                   self.sell_rules.stop_loss, self.sell_rules.trailing_stop,
                   self.sell_rules.max_hold_years))


def optimize_sell_rules(win: TrainingWindow,
                        evaluate,
                        valid_days: int = 365) -> tuple[SL.SellRules, list[str]]:
    """売りの閾値を**訓練窓の中だけ**で探索する。

    `evaluate(rules, window) -> float` は成績（大きいほど良い）を返す関数。
    **内側訓練で探索し、内側検証で選ぶ**（docs/05 §2.2）。

    **格子は粗い**（§2.3）。損切り −8% と −8.5% を区別できるデータは無い。
    """
    inner_train, inner_valid = win.split_inner(valid_days)
    grid = SL.SellRules.grid()

    # 内側訓練で上位を絞り、内側検証で最終決定する
    scored = [(evaluate(r, inner_train), r) for r in grid]
    scored.sort(key=lambda x: -x[0])
    top = [r for _, r in scored[:max(3, len(scored) // 10)]]

    best = max(top, key=lambda r: evaluate(r, inner_valid))
    notes = ["格子 %d 通りを内側訓練で評価し、上位 %d を内側検証で選んだ"
             % (len(grid), len(top))]
    notes += ["**格子の端**: " + e for e in best.at_grid_edge()]
    return best, notes


def generate(T: str, win: TrainingWindow, evaluate=None,
             limits: SZ.RiskLimits | None = None,
             kelly_fraction: float = 0.25) -> System:
    """時点 T のシステムを生成する。**訓練窓の外は一切見ない。**

    `evaluate` を渡さなければ閾値の最適化を行わず、**空のルール**を使う
    — 「最適化していない」ことを明示するため、
    デフォルトで適当な損切りを入れたりしない。
    """
    if win.hi != T:
        raise LookaheadError(
            "訓練窓の上端 %s と生成日 %s が一致しない。**T を含む窓で生成してはいけない**"
            % (win.hi, T))
    notes = []
    rules = SL.SellRules()
    if evaluate is not None:
        rules, notes = optimize_sell_rules(win, evaluate)
    else:
        notes.append("**閾値を最適化していない**（evaluate が渡されていない）")
    return System(generated_at=T, train_lo=win.lo, train_hi=win.hi,
                  sell_rules=rules, limits=limits or SZ.RiskLimits(),
                  kelly_fraction=kelly_fraction, notes=notes)


def schedule(start: str, end: str, months: int = 3) -> list[str]:
    """生成日程。**数ヶ月ごとにシステムを作り直す**（263AT のメタシステム設計）。"""
    out, cur = [], dt.date.fromisoformat(start[:10])
    last = dt.date.fromisoformat(end[:10])
    while cur <= last:
        out.append(cur.isoformat())
        y, m = divmod(cur.month - 1 + months, 12)
        cur = dt.date(cur.year + y, m + 1, min(cur.day, 28))
    return out


def walk_forward(start: str, end: str, data: dict,
                 evaluate=None, months: int = 3, train_years: float = 5.0,
                 limits: SZ.RiskLimits | None = None) -> list[System]:
    """生成日程に沿ってシステムを作り続ける。

    **各システムは自分の訓練窓しか見ていない。**
    返り値を見れば「いつ・何を根拠に・どの閾値を選んだか」が全部追える。
    """
    out = []
    for T in schedule(start, end, months):
        lo = (dt.date.fromisoformat(T)
              - dt.timedelta(days=int(365.25 * train_years))).isoformat()
        win = TrainingWindow(lo, T, data)
        if not win.dates():
            continue          # 訓練データが無い時点では生成しない
        out.append(generate(T, win, evaluate=evaluate, limits=limits))
    return out


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/backtest.py 自己テスト")
    print("-" * 80)

    # 2020-01-01 から 2026-01-01 まで、月次の観測
    data = {}
    d = dt.date(2020, 1, 1)
    i = 0
    while d < dt.date(2026, 1, 1):
        data[d.isoformat()] = {"x": float(i)}
        i += 1
        y, m = divmod(d.month, 12)
        d = dt.date(d.year + y, m + 1, 1)

    win = TrainingWindow("2022-01-01", "2024-01-01", data)
    check("窓の中の日付だけを返す",
          all("2022-01-01" <= x < "2024-01-01" for x in win.dates()))
    check("**上端は含まない**", "2024-01-01" not in win.dates())
    check("窓の中は取れる", win.get("2023-01-01") == {"x": 36.0})

    for bad in ("2024-01-01", "2025-01-01", "2021-01-01"):
        try:
            win.get(bad)
            check("**窓の外 %s を取ろうとしたら例外** " % bad, False)
        except LookaheadError:
            check("**窓の外 %s を取ろうとしたら例外**" % bad, True)

    check("系列も窓の中だけ", len(win.series("x")) == len(win.dates()))

    # 内側の分割
    it, iv = win.split_inner(365)
    check("**内側訓練と内側検証に割れる**", it.hi == iv.lo)
    check("内側検証は訓練窓の末尾", iv.hi == win.hi)
    check("内側訓練は訓練窓の先頭から", it.lo == win.lo)
    check("**内側訓練が内側検証を見られない**",
          all(x < it.hi for x in it.dates()))
    try:
        win.split_inner(3650)
        check("内側検証が長すぎたら拒否", False)
    except ValueError:
        check("内側検証が長すぎたら拒否", True)

    # 生成
    sysm = generate("2024-01-01", win)
    check("生成日と訓練窓の上端が一致する", sysm.train_hi == "2024-01-01")
    check("**最適化していないことを記録する**",
          any("最適化していない" in n for n in sysm.notes))
    check("**既定で損切りを勝手に入れない**", sysm.sell_rules.stop_loss is None)
    check("由来が読める", "訓練 [2022-01-01, 2024-01-01)" in sysm.provenance())

    try:
        generate("2024-06-01", win)
        check("**生成日と窓の上端がずれたら例外**", False)
    except LookaheadError:
        check("**生成日と窓の上端がずれたら例外**", True)

    # 閾値の最適化。**evaluate が窓の外を見ようとしたら例外になる**ことを確認
    seen = []

    def ev(rules, w):
        seen.append((w.lo, w.hi))
        # 損切りが深いほど良い、という人工の目的関数
        return -(rules.stop_loss or -1.0)

    best, notes = optimize_sell_rules(win, ev, valid_days=365)
    check("最適化が閾値を選ぶ", isinstance(best, SL.SellRules))
    check("**評価は内側訓練と内側検証でしか呼ばれない**",
          set(w[1] for w in seen) <= {"2023-01-01", "2024-01-01"})
    check("**内側訓練の上端は訓練窓の上端より前**",
          any(w[1] == "2023-01-01" for w in seen))
    check("探索の記録が残る", any("格子" in n for n in notes))

    def ev_peek(rules, w):
        w.get("2025-06-01")        # **窓の外を見る**
        return 0.0

    try:
        optimize_sell_rules(win, ev_peek, valid_days=365)
        check("**評価関数が窓の外を見たら例外で落ちる**", False)
    except LookaheadError:
        check("**評価関数が窓の外を見たら例外で落ちる**", True)

    # 日程
    sch = schedule("2022-01-01", "2023-01-01", months=3)
    check("3ヶ月ごとの日程", sch == ["2022-01-01", "2022-04-01", "2022-07-01",
                                     "2022-10-01", "2023-01-01"])

    systems = walk_forward("2022-01-01", "2024-01-01", data, months=6,
                           train_years=1.0)
    check("**ウォークフォワードが複数のシステムを生む**", len(systems) >= 4)
    check("**各システムの訓練窓が生成日より前で閉じている**",
          all(s.train_hi == s.generated_at for s in systems))
    check("訓練窓が時間とともに進む",
          systems[0].train_lo < systems[-1].train_lo)
    # **訓練窓はローリングなので、古い期間は落ちていく。**
    # 「過去は全部使える」ではなく「直近 train_years だけ使う」という設計。
    # 全期間を使うと、**古いレジームの重みが新しいレジームに残り続ける**
    # （§1.9.5 で「1926-2024 の相関は構造変化を平均している」と書いたのと同じ問題）
    check("**訓練窓はローリング（古い期間は落ちる）**",
          systems[-1].train_lo > systems[0].train_lo)
    check("訓練窓の長さが一定",
          all(abs((dt.date.fromisoformat(s.train_hi)
                   - dt.date.fromisoformat(s.train_lo)).days - 365) <= 1
              for s in systems))

    print("-" * 80)
    total = 28
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
