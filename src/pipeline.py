#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全層を束ねる — **ある時点 t のスコア断面を1本の関数で作る。**

    listing（銘柄マスタ・業種）
      + prices（価格 → J01/J10 の入力）
      + facts（PIT ファクト）+ periods（TTM / AVG）
      → universe（§6 のゲート）
      → normalize（§4 の断面正規化）
      → スコア断面

**なぜ束ねる必要があるか。**
各層は自己テストで正しいことが確認できているが、
**層と層の境目で規約が破られる**のが実際の事故の大半である。
実際、ここまでで踏んだ3つのバグはすべて境目にあった:

  - yfinance の分割調整済み → bars の想定と違った（DF-02）
  - facts の load が resolve_tags を通していなかった（訂正率 7.21% → 1.71%）
  - ttm と avg_bs で periods の順序が逆（整合 6社 → 6,847社）

→ **境目を1箇所に集め、そこにテストを置く。**

この層が守る規約
----------------
1. **すべての入力は `available_at <= t`。** 価格は t 以前のバー、
   ファクトは `filed <= t`、業種は（本来は）as-of
2. **ユニバースゲートを通ってからスコアを作る。**
   逆順にすると、シェル企業が断面の分布を決めてしまう（Phase 1 の実測）
3. **分子と分母の期間が揃っていなければ比率を作らない**（periods.aligned）
4. **欠損は z=0 + フラグ。中央値で埋めない**

自己テスト
    python src/pipeline.py
"""
from __future__ import annotations

import dataclasses
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import facts as FA        # type: ignore  # noqa: E402
import ff49               # type: ignore  # noqa: E402
import normalize as NZ    # type: ignore  # noqa: E402
import periods as PE      # type: ignore  # noqa: E402
import universe as UV     # type: ignore  # noqa: E402


@dataclasses.dataclass
class Row:
    """1銘柄・1時点の結果。**途中経過を捨てない。**"""

    ticker: str
    cik: int | None
    market: str
    sector: str | None
    sector_coarse: str | None
    in_universe: bool
    exclusions: tuple[str, ...]
    raw: float | None                 # 生の値
    z: float                          # 断面スコア（欠損なら 0.0）
    missing: bool
    fallback: str | None              # どの母集団でランクしたか
    note: str = ""


def ratio(asof: FA.AsOf, cik: int, num_code: str, den_code: str, t: str,
          den_is_bs: bool = True) -> tuple[float | None, str]:
    """`num_TTM / den_AVG` を、期間を揃えて作る。

    **揃わなければ None と理由を返す。** 近似で埋めない。
    """
    n = PE.ttm(asof, cik, num_code, t)
    if n is None:
        return None, "TTM が作れない（4四半期揃わない / 期間が飛んでいる）"
    d = (PE.avg_bs(asof, cik, den_code, t) if den_is_bs
         else PE.ttm(asof, cik, den_code, t))
    if d is None:
        return None, "分母が作れない（1年前の B/S が無い）"
    if not PE.aligned(n, d):
        return None, "分子と分母の期間が揃わない（%s vs %s）" % (
            PE.latest(n), PE.latest(d))
    if d.value == 0:
        return None, "分母がゼロ"
    return n.value / d.value, ("Q4復元" if n.derived_q4 else "")


def build(t: str, candidates: list[dict], asof: FA.AsOf,
          num_code: str, den_code: str, rho: float = 1.0,
          compute_excluded: bool = False) -> list[Row]:
    """時点 t のスコア断面を作る。

    `candidates` は1銘柄あたり次を持つ dict:
        ticker / cik / market / sic（US）/ sector・sector_coarse（JP）
        adv_jpy / zero_volume_days / mcap_jpy / months_listed
        supervised / going_concern_note / audit_clean

    **ユニバース判定 → 値の計算 → 正規化の順で行う。**

    `compute_excluded=True` にすると**ユニバース外の銘柄でも値を計算する。**
    通常は無駄なので False だが、**「ゲートを通すと分布がどう変わるか」を
    測るには両方の値が要る。**
    実際、これが無いままゲートの効果を測ろうとして
    「ゲート前後で分布が変わらない」という**測定側のバグ**を踏んだ（2026-08-23）。
    ユニバース外の raw が最初から None なので、比較が成立していなかった。
    """
    rows: list[Row] = []
    th = UV.Thresholds.for_rho(rho)

    for c in candidates:
        cand = UV.Candidate(
            ticker=c["ticker"], listed=c.get("listed", True),
            months_listed=c.get("months_listed"),
            adv_jpy=c.get("adv_jpy"), zero_volume_days=c.get("zero_volume_days"),
            mcap_jpy=c.get("mcap_jpy"), supervised=c.get("supervised", False),
            going_concern_note=c.get("going_concern_note", False),
            audit_clean=c.get("audit_clean"),
        )
        ex = UV.judge(cand, th)

        # **業種は市場で決め方が違う**（spec §4.1）
        if c["market"] == "US":
            sec = ff49.industry(c.get("sic"))
            coarse = ff49.coarse(sec)
        else:
            sec, coarse = c.get("sector"), c.get("sector_coarse")

        raw, note = (None, "ユニバース外なので計算しない")
        if (not ex or compute_excluded) and c.get("cik"):
            raw, note = ratio(asof, int(c["cik"]), num_code, den_code, t)

        rows.append(Row(
            ticker=c["ticker"], cik=c.get("cik"), market=c["market"],
            sector=sec, sector_coarse=coarse,
            in_universe=not ex, exclusions=tuple(e.name for e in ex),
            raw=raw, z=0.0, missing=True, fallback=None, note=note))

    # **正規化はユニバース内だけで行う。**
    # ユニバース外を混ぜると、シェル企業が分布を決めてしまう（Phase 1 の実測）
    idx = [i for i, r in enumerate(rows) if r.in_universe]
    res = NZ.normalize(
        [rows[i].raw for i in idx],
        [rows[i].sector for i in idx],
        coarse=[rows[i].sector_coarse for i in idx],
        market=[rows[i].market for i in idx],
    )
    for k, i in enumerate(idx):
        rows[i] = dataclasses.replace(
            rows[i], z=res.z[k], missing=res.missing[k], fallback=res.fallback[k])
    return rows


def summary(rows: list[Row]) -> str:
    import collections
    n = len(rows)
    inu = [r for r in rows if r.in_universe]
    scored = [r for r in inu if not r.missing]
    ex = collections.Counter(e for r in rows for e in r.exclusions)
    fb = collections.Counter(r.fallback for r in scored)
    out = [
        "候補 %d 銘柄" % n,
        "  ユニバース内            %d" % len(inu),
        "  スコアが作れた          %d" % len(scored),
        "  ユニバース内だが欠損     %d" % (len(inu) - len(scored)),
    ]
    if ex:
        out.append("  除外理由:")
        for k, v in ex.most_common():
            out.append("     %-16s %d" % (k, v))
    if fb:
        out.append("  母集団: 主分類 %d / 粗い分類 %d / 市場全体 %d"
                   % (fb.get(None, 0), fb.get("coarse", 0), fb.get("market", 0)))
    return "\n".join(out)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-64s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/pipeline.py 自己テスト")
    print("-" * 78)

    def q(cik, code, ddate, qtrs, v, filed):
        return FA.Fact(cik, code, ddate, qtrs, v, filed, "10-Q", code)

    # 4四半期の NI と、1年前後の TA を持つ企業を40社作る
    fs = []
    for i in range(40):
        cik = 1000 + i
        for j, d in enumerate(["2023-09-30", "2023-12-31", "2024-03-31", "2024-06-30"]):
            fs.append(q(cik, "NI", d, 1, 10.0 + i + j, "2024-08-01"))
        fs.append(q(cik, "TA", "2023-06-30", 0, 1000.0, "2023-08-01"))
        fs.append(q(cik, "TA", "2024-06-30", 0, 1000.0 + 10 * i, "2024-08-01"))
    asof = FA.AsOf(fs)

    def cand(i, **kw):
        base = dict(ticker="T%02d" % i, cik=1000 + i, market="US", sic=3674,
                    months_listed=120.0, adv_jpy=5e7, zero_volume_days=0,
                    mcap_jpy=5e10, audit_clean=True)
        base.update(kw)
        return base

    cs = [cand(i) for i in range(40)]
    rows = build("2024-09-30", cs, asof, "NI", "TA")
    check("全銘柄ぶんの行を返す", len(rows) == 40)
    check("ユニバースを全員通過", all(r.in_universe for r in rows))
    scored = [r for r in rows if not r.missing]
    check("**40社なので断面ランクが作れる（N>=30）**", len(scored) == 40)
    check("z が単調に並ぶ（NI が増えるほど大きい）",
          scored[0].z < scored[-1].z)
    check("業種が FF49 で付く", rows[0].sector == "Chips")
    check("粗い分類も付く", rows[0].sector_coarse == "BusEq")

    # ユニバースから落ちる銘柄は計算しない
    cs2 = [cand(i) for i in range(40)]
    cs2[0]["adv_jpy"] = 1.0                       # J01 で落ちる
    cs2[1]["audit_clean"] = None                  # E22 が未取得で落ちる
    rows2 = build("2024-09-30", cs2, asof, "NI", "TA")
    check("**流動性で落ちた銘柄は計算しない**",
          not rows2[0].in_universe and rows2[0].raw is None)
    check("落ちた理由を持つ", "ILLIQUID" in rows2[0].exclusions)
    check("**監査意見が未取得なら落ちる（None を適正に丸めない）**",
          not rows2[1].in_universe)
    check("**ユニバース外はスコアの母集団に入らない**",
          sum(1 for r in rows2 if not r.missing) == 38)

    # 期間が揃わない企業
    fs3 = list(fs)
    fs3 = [f for f in fs3 if not (f.cik == 1005 and f.code == "TA"
                                  and f.ddate == "2023-06-30")]
    rows3 = build("2024-09-30", cs, FA.AsOf(fs3), "NI", "TA")
    r5 = [r for r in rows3 if r.cik == 1005][0]
    check("**1年前の B/S が無ければ計算しない（近似で埋めない）**", r5.raw is None)
    check("理由を記録する", "分母が作れない" in r5.note)
    check("**それでもユニバースには残る**（別の問題なので）", r5.in_universe)
    check("欠損として扱われる", r5.missing)

    # 提出前は見えない
    rows4 = build("2024-05-01", cs, asof, "NI", "TA")
    check("**提出前の時点では誰も計算できない**",
          all(r.raw is None for r in rows4))

    s = summary(rows2)
    check("要約が出る", "ユニバース内" in s and "除外理由" in s)

    print("-" * 78)
    total = 16
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
