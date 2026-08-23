#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
実際のパラメータ計算（米国、Phase 1 で作れる分）。

`research/implementation_priority.csv`（§1.9.9）の上位から、
**現在の勘定コードで作れるもの**を実装した。
「作れない」ものは**作らない**（近似で埋めない）。

| ID | 名前 | 定義 | 再現 t |
|---|---|---|---|
| E29 | 税金費用の変化 | `Δ(TAX/PRETAX)` | **10.35** |
| B22 | 在庫回転日数の変化 | `Δ(INV/COGS×365)` | 7.56 |
| E03 | 純営業資産比率 | `NOA / 前期TA` | 6.12 |
| F24 | 金融負債の変化 | `Δ(有利子負債)/平均TA` | 9.96 |
| A04 | S/P | `REV_TTM / MCAP` | 5.45 |
| A03 | B/P | `EQ / MCAP` | 4.43 |
| A06 | EV/EBITDA の逆数 | `EBITDA / EV` | 5.13 |
| E01 | アクルーアル | `(NI − CFO) / 平均TA` | 4.59 |
| B02 | ROA | `NI_TTM / 平均TA` | — |
| B06 | 粗利/総資産 | `GP_TTM / 平均TA` | — |

**符号はカタログに従う。** ここでは「生の値」を返し、
符号の向き（+ / − / ∩）は正規化の後に適用する
— **生の値を符号で歪めると、検算ができなくなる。**

自己テスト
    python src/params_us.py
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import facts as FA        # type: ignore  # noqa: E402
import periods as PE      # type: ignore  # noqa: E402


@dataclasses.dataclass(frozen=True)
class Value:
    """1パラメータの計算結果。**作れなかった理由も持ち歩く。**"""

    pid: str
    value: float | None
    reason: str = ""
    derived_q4: bool = False


def _ttm(a, cik, code, t):
    return PE.ttm(a, cik, code, t)


def _avg(a, cik, code, t):
    return PE.avg_bs(a, cik, code, t)


def _point(a, cik, code, t, lag=400):
    return a.latest_period(cik, code, 0, t, max_lag_days=lag)


def _debt(a, cik, t):
    """有利子負債。**リースを含める版が既定**（spec §2 の IBD）。

    構成要素が1つも取れなければ None。
    **一部だけ取れたときにゼロで埋めない** — 埋めると
    「無借金企業」と「開示していない企業」が同じになる。
    """
    parts, got = 0.0, False
    for code in ("DEBT_LT", "DEBT_ST", "LEASE_OP", "LEASE_FIN"):
        f = _point(a, cik, code, t)
        if f is not None:
            parts += f.value
            got = True
    return parts if got else None


def _mcap(a, cik, t, price, fx=1.0):
    sh = _point(a, cik, "SHARES", t)
    if sh is None or price is None or sh.value <= 0:
        return None
    return sh.value * price * fx


# --------------------------------------------------------------- 各パラメータ
def e29(a, cik, t) -> Value:
    """E29 税金費用の変化（OSAP ChTax、再現 t=10.35）。

    **実効税率の前年比の変化。**
    §1.9.7 で「E は減点でなく加点のカテゴリでは」と問題提起した項目（OQ-40）。
    税務当局に嘘はつきにくいので、**納税の増加は本当に儲かっている証拠。**
    """
    cur_tax, cur_pre = _ttm(a, cik, "TAX", t), _ttm(a, cik, "PRETAX", t)
    if cur_tax is None or cur_pre is None:
        return Value("E29", None, "TTM が作れない")
    if not PE.aligned(cur_tax, cur_pre):
        return Value("E29", None, "TAX と PRETAX の期間が揃わない")
    if cur_pre.value <= 0:
        return Value("E29", None, "税引前利益が正でない")
    # 1年前の同じ量。**as-of は t のまま**（過去の値を今の情報で見る）
    import datetime as dt
    t1 = (dt.date.fromisoformat(t[:10]) - dt.timedelta(days=365)).isoformat()
    p_tax, p_pre = _ttm(a, cik, "TAX", t1), _ttm(a, cik, "PRETAX", t1)
    if p_tax is None or p_pre is None or p_pre.value <= 0:
        return Value("E29", None, "1年前の実効税率が作れない")
    return Value("E29", (cur_tax.value / cur_pre.value) - (p_tax.value / p_pre.value),
                 "", cur_tax.derived_q4)


def b22(a, cik, t) -> Value:
    """B22 在庫回転日数の変化（再現 t=7.56）。

    **急増は需要減速か押し込み販売の早期警戒。** 売り判定で価値が高い。
    """
    import datetime as dt

    def dio(tt):
        inv, cogs = _avg(a, cik, "INV", tt), _ttm(a, cik, "COGS", tt)
        if inv is None or cogs is None or cogs.value <= 0:
            return None
        return inv.value / cogs.value * 365.0

    cur = dio(t)
    prev = dio((dt.date.fromisoformat(t[:10]) - dt.timedelta(days=365)).isoformat())
    if cur is None or prev is None:
        return Value("B22", None, "在庫回転日数が2期分作れない（在庫の無い業種を含む）")
    return Value("B22", cur - prev)


def e03(a, cik, t) -> Value:
    """E03 純営業資産（NOA）比率（再現 t=6.12）。

    `NOA = (総資産 − 現金) − (総負債 − 有利子負債)`
    **フロー（アクルーアル）を積み上げるとストック（NOA）になる。**
    """
    ta, cash = _point(a, cik, "TA", t), _point(a, cik, "CASH", t)
    tl = _point(a, cik, "TL", t)
    debt = _debt(a, cik, t)
    if ta is None or tl is None or cash is None or debt is None:
        return Value("E03", None, "NOA の構成要素が揃わない")
    prev_ta = _avg(a, cik, "TA", t)
    if prev_ta is None:
        return Value("E03", None, "前期の総資産が無い")
    noa = (ta.value - cash.value) - (tl.value - debt)
    if prev_ta.value <= 0:
        return Value("E03", None, "総資産が正でない")
    return Value("E03", noa / prev_ta.value)


def f24(a, cik, t) -> Value:
    """F24 金融負債の変化（再現 t=9.96）。

    OQ-39 の「資金を集めて資産を増やした企業は負ける」塊のうち、
    **追試(3)で「残る」と判定された5本の1つ**ではないが、
    実装が最も簡単で t も高い。
    """
    import datetime as dt
    cur = _debt(a, cik, t)
    t1 = (dt.date.fromisoformat(t[:10]) - dt.timedelta(days=365)).isoformat()
    prev = _debt(a, cik, t1)
    ta = _avg(a, cik, "TA", t)
    if cur is None or prev is None or ta is None or ta.value <= 0:
        return Value("F24", None, "有利子負債が2期分揃わない")
    return Value("F24", (cur - prev) / ta.value)


def a04(a, cik, t, mcap) -> Value:
    """A04 S/P（PSR の逆数、再現 t=5.45）。**赤字企業でも作れる。**"""
    rev = _ttm(a, cik, "REV", t)
    if rev is None or mcap is None or mcap <= 0:
        return Value("A04", None, "売上 TTM か時価総額が無い")
    return Value("A04", rev.value / mcap, "", rev.derived_q4)


def a03(a, cik, t, mcap) -> Value:
    """A03 B/P（PBR の逆数、再現 t=4.43）。"""
    eq = _point(a, cik, "EQ", t)
    if eq is None or mcap is None or mcap <= 0:
        return Value("A03", None, "自己資本か時価総額が無い")
    return Value("A03", eq.value / mcap)


def a06(a, cik, t, mcap) -> Value:
    """A06 EV/EBITDA の逆数（再現 t=5.13）。

    **EBITDA は基準間で比較可能**（spec §2.1）なので、OP より頑健。
    **EV が負（ネットキャッシュ超過）でも欠損にしない** — OQ-37 の論点。
    そこが最も割安な銘柄だから。
    """
    op, da = _ttm(a, cik, "OP", t), _ttm(a, cik, "DA", t)
    cash = _point(a, cik, "CASH", t)
    debt = _debt(a, cik, t)
    if op is None or da is None or mcap is None or cash is None or debt is None:
        return Value("A06", None, "EBITDA か EV の構成要素が揃わない")
    if not PE.aligned(op, da):
        return Value("A06", None, "OP と DA の期間が揃わない")
    ebitda = op.value + da.value
    ev = mcap + debt - cash.value
    if ev == 0:
        return Value("A06", None, "EV がゼロ")
    # **EV<0 でも返す。** 欠損にすると最も割安な銘柄が消える（OQ-37）
    return Value("A06", ebitda / ev, "EV<0" if ev < 0 else "", op.derived_q4)


def e01(a, cik, t) -> Value:
    """E01 アクルーアル（再現 t=4.59）。`(NI − CFO) / 平均総資産`"""
    ni, cfo = _ttm(a, cik, "NI", t), _ttm(a, cik, "CFO", t)
    ta = _avg(a, cik, "TA", t)
    if ni is None or cfo is None or ta is None or ta.value <= 0:
        return Value("E01", None, "NI / CFO / 平均TA のいずれかが無い")
    if not PE.aligned(ni, cfo):
        return Value("E01", None, "NI と CFO の期間が揃わない")
    return Value("E01", (ni.value - cfo.value) / ta.value, "", ni.derived_q4)


def b02(a, cik, t) -> Value:
    """B02 ROA。`NI_TTM / 平均総資産`"""
    ni, ta = _ttm(a, cik, "NI", t), _avg(a, cik, "TA", t)
    if ni is None or ta is None or ta.value <= 0:
        return Value("B02", None, "NI TTM か平均総資産が無い")
    if not PE.aligned(ni, ta):
        return Value("B02", None, "分子と分母の期間が揃わない")
    return Value("B02", ni.value / ta.value, "", ni.derived_q4)


def b06(a, cik, t) -> Value:
    """B06 粗利/総資産（Novy-Marx の gross profitability）。"""
    gp, ta = _ttm(a, cik, "GP", t), _avg(a, cik, "TA", t)
    if gp is None or ta is None or ta.value <= 0:
        return Value("B06", None, "粗利 TTM か平均総資産が無い")
    return Value("B06", gp.value / ta.value, "", gp.derived_q4)


# パラメータ ID → (関数, 時価総額が要るか)
REGISTRY = {
    "E29": (e29, False), "B22": (b22, False), "E03": (e03, False),
    "F24": (f24, False), "E01": (e01, False), "B02": (b02, False),
    "B06": (b06, False),
    "A04": (a04, True), "A03": (a03, True), "A06": (a06, True),
}


def compute(a: FA.AsOf, cik: int, t: str, mcap: float | None = None,
            pids: list[str] | None = None) -> dict[str, Value]:
    """1銘柄・1時点で、指定したパラメータをまとめて計算する。"""
    out = {}
    for pid in (pids or list(REGISTRY)):
        fn, needs_mcap = REGISTRY[pid]
        out[pid] = fn(a, cik, t, mcap) if needs_mcap else fn(a, cik, t)
    return out


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-64s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/params_us.py 自己テスト")
    print("-" * 78)

    def q(code, ddate, qtrs, v, filed="2024-08-01", cik=1):
        return FA.Fact(cik, code, ddate, qtrs, v, filed, "10-Q", code)

    QS = ["2023-09-30", "2023-12-31", "2024-03-31", "2024-06-30"]
    QS_PREV = ["2022-09-30", "2022-12-31", "2023-03-31", "2023-06-30"]
    fs = []
    for d in QS:
        fs += [q("NI", d, 1, 25.0), q("CFO", d, 1, 20.0), q("REV", d, 1, 250.0),
               q("COGS", d, 1, 150.0), q("GP", d, 1, 100.0), q("OP", d, 1, 40.0),
               q("DA", d, 1, 10.0), q("TAX", d, 1, 10.0), q("PRETAX", d, 1, 35.0)]
    for d in QS_PREV:
        # **前年ぶんも一通り入れる。** B22 / E29 は2期分の TTM が要るので、
        # 片方だけ入れると「作れない」が正しい挙動として出てしまい、
        # **テストが何を確かめているのか分からなくなる**
        fs += [q("TAX", d, 1, 5.0, "2023-08-01"),
               q("PRETAX", d, 1, 35.0, "2023-08-01"),
               q("COGS", d, 1, 120.0, "2023-08-01")]
    for d, v in (("2022-06-30", 800.0), ("2023-06-30", 1000.0), ("2024-06-30", 1200.0)):
        fl = {"2022": "2022-08-01", "2023": "2023-08-01"}.get(d[:4], "2024-08-01")
        fs += [q("TA", d, 0, v, fl), q("EQ", d, 0, v * 0.6, fl),
               q("CASH", d, 0, v * 0.1, fl), q("TL", d, 0, v * 0.4, fl),
               q("INV", d, 0, v * 0.2, fl), q("DEBT_LT", d, 0, v * 0.2, fl),
               q("SHARES", d, 0, 100.0, fl)]
    a = FA.AsOf(fs)
    T = "2024-09-30"
    mcap = 100.0 * 30.0        # 100株 × $30

    v = compute(a, 1, T, mcap)
    check("**10 パラメータすべてを返す**", len(v) == 10)
    made = {k: x for k, x in v.items() if x.value is not None}
    check("**この模擬データでは全部作れる**", len(made) == 10)

    check("B02 ROA = 100 / 1100 = 0.0909",
          abs(v["B02"].value - 100.0 / 1100.0) < 1e-9)
    check("E01 アクルーアル = (100-80)/1100", abs(v["E01"].value - 20.0 / 1100.0) < 1e-9)
    check("B06 粗利/総資産 = 400/1100", abs(v["B06"].value - 400.0 / 1100.0) < 1e-9)
    check("A04 S/P = 1000 / 3000", abs(v["A04"].value - 1000.0 / 3000.0) < 1e-9)
    check("A03 B/P = 720 / 3000", abs(v["A03"].value - 720.0 / 3000.0) < 1e-9)
    # 実効税率 40/140 = 0.2857 → 前年 20/140 = 0.1429、差 +0.1429
    check("**E29 実効税率の変化 = +0.143**", abs(v["E29"].value - (40 / 140 - 20 / 140)) < 1e-9)
    # 在庫日数 = 平均在庫220 / COGS600 * 365
    check("B22 在庫回転日数の変化が作れる", v["B22"].value is not None)
    # NOA = (1200-120) - (480-240) = 840、前期 TA 平均 1100
    check("E03 NOA = 840 / 1100", abs(v["E03"].value - 840.0 / 1100.0) < 1e-9)
    # EBITDA = 160+40 = 200、EV = 3000 + 240 - 120 = 3120
    check("A06 EBITDA/EV = 200/3120", abs(v["A06"].value - 200.0 / 3120.0) < 1e-9)

    # **作れない場合は理由を返す**
    a2 = FA.AsOf([f for f in fs if f.code != "COGS"])
    v2 = compute(a2, 1, T, mcap)
    check("**COGS が無ければ B22 は None**", v2["B22"].value is None)
    check("理由を持つ", "在庫回転日数" in v2["B22"].reason)
    check("**他のパラメータは影響を受けない**", v2["B02"].value is not None)

    # 有利子負債が1つも無ければ None（ゼロで埋めない）
    a3 = FA.AsOf([f for f in fs if not f.code.startswith(("DEBT", "LEASE"))])
    check("**有利子負債が取れなければゼロで埋めない**",
          _debt(a3, 1, T) is None)
    check("その結果 E03 も作れない", compute(a3, 1, T, mcap)["E03"].value is None)

    # 時価総額が無ければバリュー系だけが落ちる
    v4 = compute(a, 1, T, None)
    check("**時価総額が無ければ A03/A04/A06 だけ落ちる**",
          v4["A03"].value is None and v4["B02"].value is not None)

    # EV が負でも欠損にしない（OQ-37）
    fs5 = [f for f in fs if f.code != "CASH"]
    fs5 += [q("CASH", "2024-06-30", 0, 9999.0)]
    a5 = FA.AsOf(fs5)
    v5 = compute(a5, 1, T, mcap)
    check("**EV<0 でも欠損にしない（OQ-37: 最も割安な銘柄を消さない）**",
          v5["A06"].value is not None and v5["A06"].reason == "EV<0")

    # **どの提出よりも前の時点では誰も作れない。**
    # 最初に書いたときに t=2024-01-01 を使ったが、
    # **2023-08-01 提出のデータは既に見えている**ので A03 などは作れてしまう。
    # 「提出前」を意味する日付を正しく選ぶ必要があった
    v6 = compute(a, 1, "2022-01-01", mcap)
    check("**どの提出よりも前なら全部 None**", all(x.value is None for x in v6.values()))
    v7 = compute(a, 1, "2024-01-01", mcap)
    check("**一部だけ提出済みなら、作れるものだけ作る**",
          v7["A03"].value is not None and v7["E29"].value is None)

    print("-" * 78)
    total = 20
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
