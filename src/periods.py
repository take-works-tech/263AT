#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
期間の合成 — TTM（直近4四半期）と AVG（期首期末平均）。

なぜこれが最優先なのか
----------------------
`tools/smoke_phase1.py` の実測（2026-08-23）で、
**NI の期間と TA の期間で年が違う企業が 4,693 / 5,783 社（81%）**あった。
spec §1.3 では B02（ROA）を「分子 TTM・分母 AVG」と定めたのに、
通期 NI と単一時点の TA を混ぜていた。

**81% は「近似」で済ませられる量ではない。**
しかも決算期が3月・6月・9月とばらける企業ほど誤差が大きいので、
**業種によって系統的な偏りが出る。**

DERA の期間表現（`qtrs`）
------------------------
| qtrs | 意味 |
|---|---|
| 0 | **時点値**（B/S） |
| 1 | 四半期（3ヶ月） |
| 2 | 半期（6ヶ月） |
| 3 | 9ヶ月 |
| 4 | 通期（12ヶ月） |

**厄介なのは、企業が累計（YTD）で報告することがある点。**
第3四半期の 10-Q には「3ヶ月」と「9ヶ月」の両方が入る。
`qtrs` で区別できるので、**qtrs=1 だけを拾えば四半期単体になる。**

**さらに厄介なのは第4四半期。** 10-K には通期（qtrs=4）は入るが
**第4四半期単体（qtrs=1）が入らないことが多い。**
→ **Q4 = 通期 − (Q1 + Q2 + Q3)** で復元する。

TTM の規約（この実装が守るもの）
------------------------------
1. **直近4つの四半期単体を合計する。** 期間が連続していなければ作らない
2. **4つ揃わなければ None。** 3つで年率化しない（季節性が入る）
3. 復元した Q4 を使ったかどうかを記録する（`derived_q4`）
4. **B/S（qtrs=0）は合計しない。** AVG を使う

自己テスト
    python src/periods.py
"""
from __future__ import annotations

import dataclasses
import datetime as dt

from facts import AsOf, Fact  # type: ignore  # noqa: F401


@dataclasses.dataclass(frozen=True)
class Composed:
    """合成した値。**どう作ったかを必ず持ち歩く。**

    「TTM です」とだけ言われた数字は検算できない。
    どの期間を足したか、Q4 を復元したかが分からないと、
    **おかしな値が出たときに原因を追えない。**
    """

    value: float
    # **使った期間の ddate。必ず昇順（古い順）で持つ。**
    # ttm() は内部で新しい順に扱うが、ここに入れるときに反転する。
    # 順序を揃えないと periods[-1] が「最新」だったり「最古」だったりして、
    # **aligned() が 12ヶ月ずれた比較をする**（実際にそのバグを踏んだ）。
    periods: tuple[str, ...]
    derived_q4: bool             # Q4 を通期から復元したか
    as_of: str                   # どの時点で見た値か
    latest_filed: str            # 使った中で最も新しい提出日


def _months_between(a: str, b: str) -> int:
    da = dt.date.fromisoformat(a[:10])
    db = dt.date.fromisoformat(b[:10])
    return (db.year - da.year) * 12 + (db.month - da.month)


def quarter_series(asof: AsOf, cik: int, code: str, t: str,
                   n: int = 8) -> list[Fact]:
    """時点 t で入手できている四半期単体（qtrs=1）を、新しい順に返す。

    **Q4 単体が無い企業では、通期から復元して差し込む。**
    """
    got: dict[str, Fact] = {}
    for ddate in asof._by_series.get((cik, code, 1), ())[:n * 2]:
        f = asof.get(cik, code, ddate, 1, t)
        if f is not None:
            got[ddate] = f

    # 通期（qtrs=4）から Q4 を復元する
    for ddate in asof._by_series.get((cik, code, 4), ())[:4]:
        fy = asof.get(cik, code, ddate, 4, t)
        if fy is None or ddate in got:
            continue
        # 通期の末日から遡る3四半期が揃っていれば Q4 を作れる
        prior = [d for d in got if 0 < _months_between(d, ddate) <= 9]
        prior.sort(reverse=True)
        if len(prior) < 3:
            continue
        three = prior[:3]
        if _months_between(three[-1], ddate) != 9:
            continue          # 期間が飛んでいる。**推測で埋めない**
        q4 = fy.value - sum(got[d].value for d in three)
        got[ddate] = dataclasses.replace(
            fy, value=q4, tag=fy.tag + "[derived Q4]")

    return sorted(got.values(), key=lambda f: f.ddate, reverse=True)[:n]


def ttm(asof: AsOf, cik: int, code: str, t: str) -> Composed | None:
    """直近4四半期の合計（TTM）。**フロー項目のみ。**

    **4つ揃わなければ None。** 3つで年率化すると季節性がそのまま入る
    （小売の第4四半期、ゲームの新作月など）。
    """
    qs = quarter_series(asof, cik, code, t, n=4)
    if len(qs) < 4:
        return None
    # 期間が連続しているか。**飛んでいたら作らない**
    for a, b in zip(qs[1:], qs):
        if _months_between(a.ddate, b.ddate) != 3:
            return None
    return Composed(
        value=sum(f.value for f in qs),
        periods=tuple(sorted(f.ddate for f in qs)),      # ← 昇順に揃える

        derived_q4=any("[derived Q4]" in f.tag for f in qs),
        as_of=t,
        latest_filed=max(f.filed for f in qs),
    )


def avg_bs(asof: AsOf, cik: int, code: str, t: str,
           months: int = 12) -> Composed | None:
    """B/S の期首期末平均（spec §1.3 の `AVG`）。

    ROA / ROE / 総資産回転率のように**フローを B/S で割る**ときは、
    期末の一時点ではなく平均を使う。
    期中に大型買収や増資があると、期末だけでは分母が過大／過小になる。

    **`months` ヶ月前の値が無ければ None。** 期末だけで代用しない
    — 代用すると「AVG のつもりで POINT を使っている」状態になり、
    **それがまさに 81% の問題だった。**
    """
    ends = asof._by_series.get((cik, code, 0), ())
    cur = None
    for ddate in ends:
        f = asof.get(cik, code, ddate, 0, t)
        if f is not None:
            cur = f
            break
    if cur is None:
        return None
    want = _months_between("1900-01-01", cur.ddate) - months
    prev = None
    for ddate in ends:
        if _months_between("1900-01-01", ddate) != want:
            continue
        prev = asof.get(cik, code, ddate, 0, t)
        if prev is not None:
            break
    if prev is None:
        return None
    return Composed(
        value=(cur.value + prev.value) / 2.0,
        periods=(prev.ddate, cur.ddate),
        derived_q4=False,
        as_of=t,
        latest_filed=max(cur.filed, prev.filed),
    )


def latest(c: Composed | None) -> str | None:
    """その合成値が対象としている**最新の期間**。

    `Composed.periods` は昇順と決めたので末尾が最新。
    **この関数を経由することで、順序の取り違えを1箇所に閉じ込める。**
    """
    return c.periods[-1] if c and c.periods else None


def aligned(num: Composed | None, den: Composed | None,
            max_gap_months: int = 3) -> bool:
    """**分子と分母の期間が揃っているか。**

    揃っていなければ比率を作らない。
    81% の期間ずれは、ここを検査していなかったから起きた。

    **実装で一度間違えた**（2026-08-23）: `ttm()` が periods を新しい順、
    `avg_bs()` が古い順で返していたため、
    **TTM の最古の四半期と AVG の期末を比べていた。**
    構造的に12ヶ月ずれるので、実データで 7,199 社の TTM が作れているのに
    **整合したのは 6 社**という結果になった。
    → `Composed.periods` を昇順に統一し、`latest()` を通すようにした。
    """
    a, b = latest(num), latest(den)
    if a is None or b is None:
        return False
    return abs(_months_between(a, b)) <= max_gap_months


# ---------------------------------------------------------------- self-test
def _f(cik, code, ddate, qtrs, value, filed, tag=None) -> Fact:
    return Fact(cik, code, ddate, qtrs, value, filed, "10-Q", tag or code)


def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-62s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/periods.py 自己テスト")
    print("-" * 76)

    # 四半期が4つ揃っている企業
    qs = [_f(1, "REV", d, 1, v, fl) for d, v, fl in [
        ("2023-09-30", 10.0, "2023-11-01"), ("2023-12-31", 20.0, "2024-02-01"),
        ("2024-03-31", 30.0, "2024-05-01"), ("2024-06-30", 40.0, "2024-08-01")]]
    a = AsOf(qs)
    c = ttm(a, 1, "REV", "2024-09-01")
    check("TTM = 直近4四半期の合計", c is not None and c.value == 100.0)
    check("**使った期間を持ち歩く**", c is not None and len(c.periods) == 4)
    check("Q4 を復元していない", c is not None and c.derived_q4 is False)
    check("最新の提出日を持つ", c is not None and c.latest_filed == "2024-08-01")

    # **提出前の四半期は使わない**
    c2 = ttm(a, 1, "REV", "2024-05-15")
    check("**まだ提出されていない四半期は使わない（3つしか無いので None）**", c2 is None)

    # 3つしか無ければ None（年率化しない）
    a3 = AsOf(qs[:3])
    check("**4つ揃わなければ None。3つで年率化しない**",
          ttm(a3, 1, "REV", "2024-09-01") is None)

    # 期間が飛んでいたら作らない
    gap = [_f(1, "REV", d, 1, 10.0, "2024-08-01") for d in
           ("2023-03-31", "2023-12-31", "2024-03-31", "2024-06-30")]
    check("**期間が飛んでいたら作らない**",
          ttm(AsOf(gap), 1, "REV", "2024-09-01") is None)

    # Q4 の復元: Q1-Q3 と通期があり、Q4 単体が無い
    fy = [_f(2, "REV", "2024-03-31", 1, 10.0, "2023-08-01"),
          _f(2, "REV", "2024-06-30", 1, 20.0, "2023-11-01"),
          _f(2, "REV", "2024-09-30", 1, 30.0, "2024-02-01"),
          _f(2, "REV", "2024-12-31", 4, 100.0, "2025-03-01")]     # 通期
    b = AsOf(fy)
    c3 = ttm(b, 2, "REV", "2025-04-01")
    check("**Q4 を通期から復元する（100 - 60 = 40）**",
          c3 is not None and c3.value == 100.0)
    check("**復元したことを記録する**", c3 is not None and c3.derived_q4 is True)
    qser = quarter_series(b, 2, "REV", "2025-04-01")
    check("復元した Q4 の値が正しい",
          any(f.ddate == "2024-12-31" and f.value == 40.0 for f in qser))
    check("復元したタグに印が付く",
          any("[derived Q4]" in f.tag for f in qser))

    # **通期が提出される前は復元しない**
    check("通期の提出前は Q4 を作らない", ttm(b, 2, "REV", "2025-01-01") is None)

    # AVG
    bs = [_f(3, "TA", "2023-06-30", 0, 100.0, "2023-08-01"),
          _f(3, "TA", "2024-06-30", 0, 200.0, "2024-08-01")]
    d = AsOf(bs)
    av = avg_bs(d, 3, "TA", "2024-09-01")
    check("**AVG = 期首期末平均**", av is not None and av.value == 150.0)
    check("AVG は2期間を持つ", av is not None and len(av.periods) == 2)
    check("**1年前が無ければ None。期末で代用しない**",
          avg_bs(AsOf(bs[1:]), 3, "TA", "2024-09-01") is None)

    # 期間の整合
    check("**分子と分母の期間が揃っていれば True**",
          aligned(Composed(1, ("2024-06-30",), False, "t", "f"),
                  Composed(1, ("2024-03-31", "2024-06-30"), False, "t", "f")))
    check("**半年ずれていたら False（比率を作らせない）**",
          not aligned(Composed(1, ("2023-12-31",), False, "t", "f"),
                      Composed(1, ("2023-06-30", "2024-06-30"), False, "t", "f")))
    check("片方が None なら False", not aligned(None, None))

    # **順序の取り違えを検出するテスト。** これが無くて実データで踏んだ
    check("**ttm の periods は昇順（末尾が最新）**",
          c is not None and c.periods == tuple(sorted(c.periods))
          and c.periods[-1] == "2024-06-30")
    check("**avg_bs の periods も昇順**",
          av is not None and av.periods == ("2023-06-30", "2024-06-30"))
    check("latest() が両方から最新を取れる",
          latest(c) == "2024-06-30" and latest(av) == "2024-06-30")
    check("**TTM と AVG が同じ期末を指していれば整合する**", aligned(c, av))

    print("-" * 76)
    total = 22
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
