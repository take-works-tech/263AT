#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
価格系パラメータ（Phase 0）。**登録も課金も要らない。**

`research/implementation_priority.csv` の Phase 0（源が PX / CALC / SELF のみ）
134 本のうち、**その銘柄自身の価格系列だけで完結するもの**を実装した。
市場リターンやファクター回帰が要るもの（G10 残差モメンタム、I04 IVOL、
I29、I08、I27）は**この層では作らない** — 断面の情報が要るので層が違う。

| ID | 名前 | 定義 | 優先度 |
|---|---|---|---|
| G38 | 季節性リターン（前年同月） | 前年の同じ暦月のリターン | **9.675** |
| G04 | 直近1ヶ月リターン | `P_t / P_{t-21} - 1` | 9.29 |
| G32 | 中間モメンタム | `P_{t-147} / P_{t-252} - 1` | 9.09 |
| G41 | 季節性（11〜15年前） | 同月リターンの平均 | 7.345 |
| G44 | 季節性（16〜20年前） | 同上 | 7.312 |
| G45 | オフシーズン反転（6〜10年） | 同月を**除いた**平均 | 6.963 |
| G43 | 季節性を除いたモメンタム | `G01 - G38` | 6.062 |
| G42 | オフシーズン・モメンタム | 同月を除いた平均（1〜5年） | 5.663 |
| G39 | 季節性（2〜5年前の同月） | 同月リターンの平均 | 5.466 |
| J22 | 出来高トレンド | `ln(出来高)` の回帰の傾き | 5.225 |
| G02 | 6-1モメンタム | `P_{t-21} / P_{t-126} - 1` | 4.543 |
| G01 | 12-1モメンタム | `P_{t-21} / P_{t-252} - 1` | 4.283 |
| J01 | 平均売買代金(20日) | `mean(終値 × 出来高, 20日)` | 3.88 |
| J10 | ゼロ出来高日の比率 | 60日中のゼロ日数 / 60 | 3.0 |
| ~~J25~~ | ~~株価水準~~ | **この層では作れない（下記）** | — |
| I01 | ヒストリカルボラ(60日) | `sd(日次対数r, 60) × √252` | 2.96 |
| G03 | 3-1モメンタム | `P_{t-21} / P_{t-63} - 1` | 2.5 |
| G16 | 時系列モメンタム | `sign(P_t / P_{t-252} - 1)` | 2.5 |
| H05 | 1週間リバーサル | `-(P_t / P_{t-5} - 1)` | 2.5 |

**符号はカタログに従う。** ここでは「生の値」を返し、
符号の向き（+ / − / ∩）は正規化の後に適用する
— **生の値を符号で歪めると、検算ができなくなる。**
H05 だけは定義そのものに負号が入っている（カタログの定義に従う）。

PIT — **この層で最も間違えやすいところ**
------------------------------------------
すべての関数は `(rows, i)` を取り、**`rows[0..i]` しか見ない。**
`i` は「時点 t の行」であり、**`rows[i+1]` 以降は存在しないものとして扱う。**

    G01 は `P_{t-21} / P_{t-252}` である。
    **直近1ヶ月を除く**のは、短期反転（H05）と符号が逆で、
    混ぜると打ち消し合うため。**除外を忘れると符号が反転しうる。**

季節性の「対象月」の決め方
--------------------------
Heston-Sadka 型の季節性は「**これから入る暦月**と同じ月の、過去のリターン」。
`t` の属する暦月を対象月とする。**暦は未来を知らなくても分かる**ので、
ここにルックアヘッドは無い。

**過去の暦月リターンは、`rows[0..i]` から作った月次系列で測る。**
対象月の当年ぶんは**まだ確定していない**ので使わない
（`_monthly` は `i` までで打ち切るので構造的に入らない）。

欠損の扱い
----------
**作れないときは `None` と理由を返す。近似で埋めない。**
`normalize.py` が「欠損は z=0 + フラグ」で扱う前提なので、
**ここで 0 を返すと「平均的な銘柄」に化ける。**

自己テスト
    python src/params_px.py
"""
from __future__ import annotations

import math
import pathlib
import sys
from typing import Sequence

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import bars as B          # type: ignore  # noqa: E402
# **Value は params_us が定義したものをそのまま使う。**
# 3つ目のパラメータ層ができたら共通モジュールへ移す。
from params_us import Value  # type: ignore  # noqa: E402

# 1ヶ月 = 21営業日。カタログの定義がこの粒度で書かれている
M1, M3, M6, M12 = 21, 63, 126, 252
G32_START = 147          # 7ヶ月前（12-1 のうち直近5ヶ月を除いた区間）


def _px(rows: Sequence[dict], i: int, back: int) -> float | None:
    """`back` 営業日前の終値。**範囲外は None。**"""
    j = i - back
    if j < 0 or j >= len(rows):
        return None
    v = rows[j]["close"]
    return v if v and v > 0 else None


def _ret(rows: Sequence[dict], i: int, a: int, b: int) -> float | None:
    """`P_{t-a} / P_{t-b} - 1`。**a < b**（a が新しい側）。"""
    pa, pb = _px(rows, i, a), _px(rows, i, b)
    if pa is None or pb is None:
        return None
    return pa / pb - 1.0


# 月次系列の使い回し。**同じ銘柄で8本の季節性が同じものを作り直すのを防ぐ。**
_MONTHLY_CACHE: dict[int, list[tuple[int, int, float, int]]] = {}


def _monthly_full(rows: Sequence[dict]) -> list[tuple[int, int, float, int]]:
    """暦月ごとのリターンを**一度だけ**作る。

    返すのは `(年, 月, リターン, その月の最終行の位置)` の一覧。

    **最後の要素が「その月がいつ閉じたか」であることが重要。**
    時点 t での季節性は「t までに**確定した**月」だけを使う。
    位置を持たせておけば、`i` ごとに作り直さずに絞り込める。

    ここを作り直していたせいで、**8本の季節性がそれぞれ全系列を
    走査していた**（1銘柄 × 173月末 × 8本 × 全バー）。
    ユニバースを 9,631 銘柄に広げた時点で、計算が終わらなくなった。
    """
    key = id(rows)
    got = _MONTHLY_CACHE.get(key)
    if got is not None:
        return got
    out: list[tuple[int, int, float, int]] = []
    prev_close: float | None = None
    cur: tuple[int, int] | None = None
    first_prev: float | None = None
    last_close: float | None = None
    last_idx = -1
    for j in range(len(rows)):
        d = rows[j]["date"]
        y, m = int(d[:4]), int(d[5:7])
        c = rows[j]["close"]
        if cur is None:
            cur, first_prev, last_close, last_idx = (y, m), prev_close, c, j
        elif (y, m) != cur:
            if first_prev and last_close and first_prev > 0:
                out.append((cur[0], cur[1], last_close / first_prev - 1.0,
                            last_idx))
            cur, first_prev, last_close, last_idx = (y, m), prev_close, c, j
        else:
            last_close, last_idx = c, j
        prev_close = c
    # **最後の月は閉じない。** 途中かもしれないから
    if len(_MONTHLY_CACHE) > 8:
        _MONTHLY_CACHE.clear()      # 銘柄を跨いで溜めない
    _MONTHLY_CACHE[key] = out
    return out


def _monthly(rows: Sequence[dict], i: int) -> dict[tuple[int, int], float]:
    """`rows[0..i]` から暦月ごとのリターンを作る。

    **`i` より後に閉じた月は入れない。**
    対象月の当年ぶんは、その月がまだ閉じていないので構造的に入らない。
    """
    return {(y, m): r for y, m, r, j in _monthly_full(rows) if j < i}


def _season(rows, i, lo_years: int, hi_years: int,
            same_month: bool) -> float | None:
    """対象月と同じ（または異なる）暦月の、過去 lo〜hi 年のリターン平均。"""
    if i < 0 or i >= len(rows):
        return None
    d = rows[i]["date"]
    y0, m0 = int(d[:4]), int(d[5:7])
    hist = _monthly(rows, i)
    vals = []
    for k in range(lo_years, hi_years + 1):
        for m in range(1, 13):
            if same_month and m != m0:
                continue
            if not same_month and m == m0:
                continue
            v = hist.get((y0 - k, m))
            if v is not None:
                vals.append(v)
    if not vals:
        return None
    return sum(vals) / len(vals)


def _logrets(rows: Sequence[dict], i: int, window: int) -> list[float]:
    """直近 window 日の対数リターン。**欠損（停止・出来高0）は落とす。**

    **必要な区間だけを渡す。**
    以前は `B.log_return(rows[:i+1])` と全系列を計算してから末尾を取っていた。
    月末ごとに呼ばれるので、**1銘柄あたり 173 回 × 全バー**を走査していた。
    16,268 本の銘柄では、60日ぶんが欲しいだけで毎回16,268本を計算していた。

    先頭に1本余分に渡すのは、**最初の日のリターンに前日の終値が要る**から。
    """
    lo = max(0, i + 1 - window - 1)
    seg = rows[lo: i + 1]
    lr = B.log_return(seg)
    w = lr[-window:] if len(lr) > window else lr
    return [x for x in w if x is not None]


# ---------------------------------------------------------------- 各パラメータ
def g01(rows, i):
    """12-1モメンタム。**直近1ヶ月を除く**（短期反転と符号が逆のため）。"""
    return _ret(rows, i, M1, M12)


def g02(rows, i):
    return _ret(rows, i, M1, M6)


def g03(rows, i):
    return _ret(rows, i, M1, M3)


def g04(rows, i):
    """直近1ヶ月リターン。**符号は −**（短期反転）。生の値を返す。"""
    return _ret(rows, i, 0, M1)


def g16(rows, i):
    """時系列モメンタム。**符号だけ**を取る。"""
    r = _ret(rows, i, 0, M12)
    return None if r is None else (1.0 if r > 0 else -1.0)


def g32(rows, i):
    """中間モメンタム（7〜12ヶ月前の区間だけ）。"""
    return _ret(rows, i, G32_START, M12)


def g38(rows, i):
    return _season(rows, i, 1, 1, True)


def g39(rows, i):
    return _season(rows, i, 2, 5, True)


def g40(rows, i):
    return _season(rows, i, 6, 10, True)


def g41(rows, i):
    return _season(rows, i, 11, 15, True)


def g44(rows, i):
    return _season(rows, i, 16, 20, True)


def g42(rows, i):
    """オフシーズン・モメンタム（1〜5年、対象月を**除く**）。"""
    return _season(rows, i, 1, 5, False)


def g45(rows, i):
    """オフシーズン反転（6〜10年、対象月を**除く**）。"""
    return _season(rows, i, 6, 10, False)


def g43(rows, i):
    """季節性を除いたモメンタム = `G01 − G38`。

    **片方でも欠ければ作らない。** 0 で埋めると G01 そのものに化ける。
    """
    a, b = g01(rows, i), g38(rows, i)
    if a is None or b is None:
        return None
    return a - b


def h05(rows, i):
    """1週間リバーサル。**定義に負号が入っている**（カタログに従う）。"""
    r = _ret(rows, i, 0, 5)
    return None if r is None else -r


def i01(rows, i, window: int = 60):
    """ヒストリカルボラ（60日、年率）。

    **欠損日を 0 で埋めない。** 埋めるとボラが過小になり、
    Kelly の分母が小さくなってポジションが過大になる（§1.8）。
    """
    w = _logrets(rows, i, window)
    if len(w) < window // 2:
        return None
    if len(w) < 2:
        return None
    mu = sum(w) / len(w)
    var = sum((x - mu) ** 2 for x in w) / (len(w) - 1)
    # **分散 0 は「計算できない」ではなく「ボラが 0」である。**
    # `if not var` と書くと 0.0 が偽になり、**実測値を欠損に化けさせる。**
    # 自己テストが検出した（定数価格でボラが None になった）。
    if var < 0:
        return None
    return math.sqrt(var) * math.sqrt(252.0)


def j01(rows, i, window: int = 20):
    """平均売買代金（20日）。**流動性ゲートの入力。**"""
    if i + 1 < window:
        return None
    w = rows[i + 1 - window: i + 1]
    return sum(x["turnover"] for x in w) / window


def j10(rows, i, window: int = 60):
    """ゼロ出来高日の比率（60日）。"""
    if i + 1 < window:
        return None
    w = rows[i + 1 - window: i + 1]
    return sum(1 for x in w if x["volume"] <= 0) / float(window)


def j22(rows, i, window: int = 60):
    """出来高トレンド。`ln(出来高)` を時間に回帰した傾き。

    **ゼロ出来高日は落とす**（`ln(0)` が定義できない）。
    落とした結果として本数が足りなければ作らない。
    """
    if i + 1 < window:
        return None
    w = rows[i + 1 - window: i + 1]
    pts = [(k, math.log(x["volume"])) for k, x in enumerate(w)
           if x["volume"] > 0]
    if len(pts) < window // 2:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    den = sum((p[0] - mx) ** 2 for p in pts)
    if den <= 0:
        return None
    return sum((p[0] - mx) * (p[1] - my) for p in pts) / den


# **株価水準は、この層では作れない。**
#
# 遡及調整後の終値を「株価水準」として返していた（2026-08-24 まで）。
# それは**深刻なルックアヘッド**だった:
#
#   調整後の株価が低い ⟺ その後に大きく分割した ⟺ **その後に大きく上がった**
#
# NVDA の 2015年末は実際 $32.94 だが、データ上は $0.82（40分割ぶん）。
# **分割の記録は Series が持っていて、rows（調整後の行）には無い。**
# だから正しい株価は `prices.price_at()` でしか作れず、
# **この層の (rows, i) という引数では原理的に作れない。**
#
# 「作れないものは作らない」（このモジュールの規約）に従って、外す。
# 呼び出し側（build_panel）が prices.price_at() で作って渡す。


def j25(rows, i):
    """**使わない。** 上の注記の通り、この層では作れない。"""
    return None


# pid -> 関数
PARAMS = {
    "G01": g01, "G02": g02, "G03": g03, "G04": g04, "G16": g16, "G32": g32,
    "G38": g38, "G39": g39, "G40": g40, "G41": g41, "G42": g42, "G43": g43,
    "G44": g44, "G45": g45,
    "H05": h05,
    "I01": i01,
    "J01": j01, "J10": j10, "J22": j22, "J25": j25,
}

# 必要な履歴の長さ（営業日）。**足りなければ理由を返すため**
NEEDS_DAYS = {
    "G01": M12, "G02": M6, "G03": M3, "G04": M1, "G16": M12, "G32": M12,
    "H05": 5, "I01": 60, "J01": 20, "J10": 60, "J22": 60, "J25": 1,
    "G38": 252 * 2, "G39": 252 * 6, "G40": 252 * 11,
    "G41": 252 * 16, "G44": 252 * 21,
    "G42": 252 * 6, "G45": 252 * 11, "G43": 252 * 2,
}


def compute(rows: Sequence[dict], i: int, pid: str) -> Value:
    """1本を計算する。**作れない理由を持ち歩く。**"""
    f = PARAMS.get(pid)
    if f is None:
        return Value(pid, None, "未実装")
    need = NEEDS_DAYS.get(pid, 1)
    if i + 1 < need:
        return Value(pid, None, "履歴が足りない（%d 日必要、%d 日）"
                     % (need, i + 1))
    try:
        v = f(rows, i)
    except Exception as e:                     # pragma: no cover
        return Value(pid, None, "計算で例外: %s" % e)
    if v is None:
        return Value(pid, None, "入力が欠損")
    if not math.isfinite(v):
        # **inf / nan を通さない。** 通すと正規化の順位が壊れる
        return Value(pid, None, "有限でない値")
    return Value(pid, float(v))


def compute_all(rows: Sequence[dict], i: int) -> dict[str, Value]:
    return {p: compute(rows, i, p) for p in PARAMS}


# ---------------------------------------------------------------- self-test
def _rows(n: int, start: str = "2020-01-01",
          price=lambda k: 100.0, vol=lambda k: 1000.0) -> list[dict]:
    """営業日っぽい連続日付の行を作る（土日を飛ばす）。"""
    import datetime as dt
    d = dt.date.fromisoformat(start)
    out = []
    k = 0
    while len(out) < n:
        if d.weekday() < 5:
            c = price(k)
            v = vol(k)
            out.append({"date": d.isoformat(), "open": c, "high": c, "low": c,
                        "close": c, "volume": v, "turnover": c * v,
                        "dividend": 0.0, "halted": False,
                        "limit_up": False, "limit_down": False})
            k += 1
        d += dt.timedelta(days=1)
    return out


def _test() -> int:
    fails = []
    ran = []

    def check(nm, cond):

        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-68s %s" % (nm, "OK" if cond else "**FAIL**"))

    def near(a, b, tol=1e-9):
        return a is not None and abs(a - b) < tol

    print("src/params_px.py 自己テスト")
    print("-" * 84)

    # --- モメンタムの定義 ---------------------------------------------------
    r = _rows(300, price=lambda k: 100.0 * (1.001 ** k))
    n = len(r) - 1
    check("**12-1 は直近1ヶ月を除く**",
          near(g01(r, n), (1.001 ** (n - M1)) / (1.001 ** (n - M12)) - 1))
    check("**直近1ヶ月リターンは除かない**",
          near(g04(r, n), (1.001 ** n) / (1.001 ** (n - M1)) - 1))
    check("6-1 と 3-1 は窓だけが違う",
          near(g02(r, n), 1.001 ** (M6 - M1) - 1)
          and near(g03(r, n), 1.001 ** (M3 - M1) - 1))
    check("**中間モメンタムは 7〜12ヶ月前の区間**",
          near(g32(r, n), 1.001 ** (M12 - G32_START) - 1))
    check("時系列モメンタムは符号だけ", g16(r, n) == 1.0)

    dn = _rows(300, price=lambda k: 100.0 * (0.999 ** k))
    check("**下落なら時系列モメンタムは -1**", g16(dn, len(dn) - 1) == -1.0)

    # H05 の負号
    check("**1週間リバーサルは定義に負号が入る（上昇なら負）**",
          h05(r, n) is not None and h05(r, n) < 0)

    # --- PIT: 未来を見ない ---------------------------------------------------
    r2 = list(r)
    r2[n]["close"] = 1e6          # **末尾だけ壊す**
    check("**i を1つ手前にすれば壊れた末尾は影響しない**",
          near(g01(r2, n - 1), g01(r, n - 1)))
    fut = r + _rows(50, start="2030-01-01")
    check("**i より後ろの行を見ない**", near(g01(fut, n), g01(r, n)))

    # --- 履歴不足は理由を返す ------------------------------------------------
    short = _rows(30)
    v = compute(short, len(short) - 1, "G01")
    check("**履歴が足りなければ None**", v.value is None)
    check("理由に必要日数を書く", "252" in v.reason)
    check("短い窓のものは作れる", compute(short, len(short) - 1, "H05")
          .value is not None)

    # --- 月次リターンと季節性 ------------------------------------------------
    long = _rows(252 * 3, start="2019-01-01",
                 price=lambda k: 100.0 * (1.0005 ** k))
    m = _monthly(long, len(long) - 1)
    check("**月次リターンが作れる**", len(m) > 30)
    check("**最後の月は閉じない（未確定なので）**",
          (int(long[-1]["date"][:4]), int(long[-1]["date"][5:7])) not in m)
    check("単調上昇なら月次はすべて正", all(v > 0 for v in m.values()))
    check("**前年同月の季節性が作れる**", g38(long, len(long) - 1) is not None)
    check("**履歴が無い年の季節性は None**", g44(long, len(long) - 1) is None)
    check("オフシーズンは対象月を除く",
          g42(long, len(long) - 1) is not None)
    check("**季節性を除いたモメンタムは差**",
          near(g43(long, len(long) - 1),
               g01(long, len(long) - 1) - g38(long, len(long) - 1)))

    # 季節性が「対象月と同じ月」を見ていること
    import datetime as dt
    d = dt.date.fromisoformat(long[-1]["date"])
    hist = _monthly(long, len(long) - 1)
    check("**対象月は t の暦月**",
          near(g38(long, len(long) - 1), hist[(d.year - 1, d.month)]))

    # --- ボラティリティ ------------------------------------------------------
    import random
    random.seed(0)
    noisy = _rows(200, price=lambda k, s=[100.0]:
                  s.append(s[-1] * math.exp(random.gauss(0, 0.02))) or s[-1])
    vol = i01(noisy, len(noisy) - 1)
    check("**ボラが年率で妥当な範囲**", vol is not None and 0.1 < vol < 1.0)
    check("**一定値ならボラは 0**", near(i01(_rows(200), 199), 0.0, 1e-12))

    # 欠損を 0 で埋めていないこと
    halted = _rows(200)
    for k in range(100, 140):
        halted[k]["volume"] = 0.0
    # **窓の中に 0 が入る位置を選ぶ。** 最初は i=199 と書いていて、
    # 窓（140..199）に 0 が1日も入っておらず、検査が意味を成していなかった
    check("**出来高0日を 0リターンとして数えない**",
          len(_logrets(halted, 159, 60)) < 60)

    # --- 流動性 --------------------------------------------------------------
    check("平均売買代金は turnover の平均",
          near(j01(_rows(50), 49), 100.0 * 1000.0))
    check("**ゼロ出来高比率**", near(j10(halted, 159), 40 / 60.0))
    # **J25 はこの層では作らない**（分割の記録が rows に無いため）
    check("**J25 はこの層では作らない（None を返す）**", j25(_rows(10), 9) is None)
    check("**作らないものは compute でも理由付きで None**",
          compute(_rows(10), 9, "J25").value is None)

    up = _rows(100, vol=lambda k: 1000.0 * (1.01 ** k))
    check("**出来高が増えていれば傾きは正**",
          j22(up, 99) is not None and j22(up, 99) > 0)
    check("一定なら傾きは 0", near(j22(_rows(100), 99), 0.0, 1e-9))

    # --- compute の契約 ------------------------------------------------------
    v = compute(r, n, "ZZ99")
    check("**知らない ID は未実装と言う**", v.value is None and "未実装" in v.reason)
    allv = compute_all(long, len(long) - 1)
    check("全 %d 本を返す" % len(PARAMS), len(allv) == len(PARAMS))
    made = sum(1 for x in allv.values() if x.value is not None)
    print("     （3年の履歴で作れたのは %d / %d 本）" % (made, len(PARAMS)))
    check("**作れなかったものは理由を持つ**",
          all(x.reason for x in allv.values() if x.value is None))

    # 非有限を通さない
    bad = _rows(300)
    for x in bad:
        x["close"] = 0.0
    check("**価格0なら None（inf を返さない）**",
          compute(bad, 299, "G01").value is None)

    print("-" * 84)
    declared = 33
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_test())
