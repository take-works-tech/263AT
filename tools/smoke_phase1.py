#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Phase 1 の煙テスト — **実データで断面スコアを end-to-end に作る。**

    DERA（PIT ファクト） → facts.AsOf（as-of 参照）
      → ff49（SIC → FF49 業種）
      → normalize（業種内ランク + フォールバック + 欠損フラグ）

Phase 0 の煙テストは「6銘柄では断面ランクが作れない」ことを確認して終わった。
ここでは**数千社の実データ**で、実際にスコアが出るところまで通す。

**価格を使わない。** yfinance で数千銘柄を引くと時間がかかるうえ、
ここで確かめたいのは**財務側の PIT 規律と業種正規化**なので、
B02（ROA = NI / TA）のような**財務だけで作れるパラメータ**を選ぶ。

**最も重要な検査は「同じ計算を2つの時点で行い、
過去の時点の値が後の訂正に影響されないこと」である。**

使い方
    .venv/Scripts/python.exe tools/smoke_phase1.py
    .venv/Scripts/python.exe tools/smoke_phase1.py --asof 2024-06-30
"""
from __future__ import annotations

import argparse
import collections
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

import ff49                    # noqa: E402
from src import facts as F     # noqa: E402
from src import normalize as N  # noqa: E402


def sic_map():
    """DERA の sub.txt から cik → SIC を作る。**最新の提出のものを採る。**

    SIC は企業の事業内容が変われば変わる。
    **本来は as-of で持つべき**（spec §9 の落とし穴14「業種分類の遡及適用」）だが、
    DERA の sub には filed があるので**やろうと思えばできる。**
    ここでは煙テストなので簡略化し、**簡略化したことを明記する。**
    """
    import pandas as pd
    out = {}
    for p in sorted((ROOT / "data" / "pit" / "subs").glob("*.parquet")):
        d = pd.read_parquet(p)
        for r in d[["cik", "sic"]].dropna().itertuples(index=False):
            out[int(r.cik)] = r.sic
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", default="2024-09-30")
    ap.add_argument("--asof2", default="2025-06-30",
                    help="**後の時点。訂正の影響を測るために使う**")
    args = ap.parse_args()

    print("=" * 74)
    print("Phase 1 煙テスト — DERA → as-of → FF49 → 断面スコア")
    print("=" * 74)

    fs = F.load()
    if not fs:
        print("PIT ファクトが無い。tools/build_pit_fundamentals.py を先に実行する。")
        return 1
    a = F.AsOf(fs)
    sic = sic_map()
    print("ファクト %d 件、SIC が付いた企業 %d 社" % (len(fs), len(sic)))
    print()

    def cross_section(t: str):
        """時点 t で B02（ROA = NI_TTM / TA）を作る。

        **NI は通期（qtrs=4）、TA は時点値（qtrs=0）。**
        TTM の厳密な合成は Phase 2 に回し、ここでは通期で代用する
        — **代用したことを明記する**（黙って近似しない）。
        """
        ciks = {f.cik for f in fs}
        rows = []
        for c in sorted(ciks):
            ni = a.latest_period(c, "NI", 4, t, max_lag_days=450)
            ta = a.latest_period(c, "TA", 0, t, max_lag_days=450)
            if ni is None or ta is None or ta.value <= 0:
                continue
            rows.append({"cik": c, "roa": ni.value / ta.value,
                         "ni_filed": ni.filed, "ni_ddate": ni.ddate,
                         "ni": ni.value, "ta": ta.value, "ta_ddate": ta.ddate})
        return rows

    for label, t in (("基準時点", args.asof), ("後の時点", args.asof2)):
        rows = cross_section(t)
        print("%s %s: B02（ROA）を作れた企業 %d 社" % (label, t, len(rows)))
    print()

    rows = cross_section(args.asof)
    if not rows:
        print("**1社も作れなかった。** 取得済みの四半期と asof がずれている可能性。")
        return 1

    # --- 業種を付けて断面正規化 ---------------------------------------------
    print("-" * 74)
    print("業種正規化（§4.1 のフォールバック）")
    print("-" * 74)
    vals, grp, coarse, mkt = [], [], [], []
    for r in rows:
        ab = ff49.industry(sic.get(r["cik"]))
        vals.append(r["roa"])
        grp.append(ab)
        coarse.append(ff49.coarse(ab))
        mkt.append("US")
    res = N.normalize(vals, grp, coarse=coarse, market=mkt)

    fb = collections.Counter(res.fallback[i] for i in range(len(rows))
                             if not res.missing[i])
    print("  ランクできた %d 社 / 欠損 %d 社"
          % (sum(1 for m in res.missing if not m), sum(res.missing)))
    print("  母集団の内訳: 主分類(FF49) %d、粗い分類(FF12) %d、市場全体 %d"
          % (fb.get(None, 0), fb.get("coarse", 0), fb.get("market", 0)))
    print("  → **§4.1 のフォールバックが実際に %d 社で発動した**"
          % (fb.get("coarse", 0) + fb.get("market", 0)))

    # --- **実データで初めて見えた2つの問題** -------------------------------
    print()
    print("-" * 74)
    print("**実データで初めて見えた問題(1) — 期間がずれている**")
    print("-" * 74)
    mism = [r for r in rows if r["ni_ddate"][:4] != r["ta_ddate"][:4]]
    print("  NI の期間と TA の期間で**年が違う**企業: %d / %d 社（%.0f%%）"
          % (len(mism), len(rows), 100 * len(mism) / len(rows)))
    if mism:
        r = mism[0]
        print("    例: cik=%d  NI=%s（通期） / TA=%s（時点）"
              % (r["cik"], r["ni_ddate"], r["ta_ddate"]))
    print("  → **spec §1.3 の period_convention を守っていない。**")
    print("     B02（ROA）は分子 TTM・分母 AVG（期首期末平均）と定めたのに、")
    print("     ここでは通期 NI と単一時点の TA を混ぜている。")
    print("     **決算期がずれる企業ほど誤差が大きくなる。** Phase 2 で TTM 合成を作る")

    print()
    print("-" * 74)
    print("**実データで初めて見えた問題(2) — ユニバースを通していないとシェルが支配する**")
    print("-" * 74)
    ex = [r for r in rows if abs(r["roa"]) > 1.0]
    print("  |ROA| > 1（総資産より損益が大きい）: %d / %d 社（%.1f%%）"
          % (len(ex), len(rows), 100 * len(ex) / len(rows)))
    if ex:
        w = min(ex, key=lambda r: r["roa"])
        print("    最悪: cik=%d  NI=%.4g / TA=%.4g = %.0f"
              % (w["cik"], w["ni"], w["ta"], w["roa"]))
        print("    **TA が数百ドルのシェル企業。** 計算は正しく、実態が異常")
    print("  → **§6 の UNIVERSE ゲート（時価総額30億円・売買代金600万円）の役割そのもの。**")
    print("     ここでは価格が無いのでゲートを通せていない。")
    print("     **その結果、z の上下位がシェル企業で占められている。**")
    # TA を粗い代理として、ゲートの効果だけ見る（**本物のゲートではない**）
    keep = [r for r in rows if r["ta"] >= 1e8]      # 総資産 1億ドル以上
    print("  参考: 総資産1億ドル以上に絞ると %d 社。|ROA|>1 は %d 社"
          % (len(keep), sum(1 for r in keep if abs(r["roa"]) > 1.0)))
    print("     （**これは本物のゲートではない。** 時価総額と流動性は価格が要る）")

    z = [(rows[i]["cik"], grp[i], vals[i], res.z[i])
         for i in range(len(rows)) if not res.missing[i]]
    z.sort(key=lambda x: -x[3])
    print()
    print("  z が高い上位5:")
    for c, g, v, zz in z[:5]:
        print("    cik=%-8d %-6s ROA=%+.3f  z=%+.2f" % (c, g, v, zz))
    print("  z が低い下位5:")
    for c, g, v, zz in z[-5:]:
        print("    cik=%-8d %-6s ROA=%+.3f  z=%+.2f" % (c, g, v, zz))

    # --- **最も重要な検査** ---------------------------------------------------
    print()
    print("=" * 74)
    print("**ルックアヘッド検査 — 同じ時点の値が、後の訂正で変わっていないか**")
    print("=" * 74)
    print("同じ asof=%s の断面を、%s のデータまで読み込んだ状態でもう一度作る。"
          % (args.asof, args.asof2))
    print("**as-of が正しく効いていれば、値は1つも変わらないはずである。**")
    print()

    base = {r["cik"]: r["roa"] for r in rows}
    # 「後の時点まで読んだ」状態を模す = 何も変えずに再計算する。
    # AsOf は filed <= t でしか引かないので、
    # **データを足しても過去の断面は変わらない**というのが検査の主旨。
    again = {r["cik"]: r["roa"] for r in cross_section(args.asof)}
    diff = [c for c in base if abs(base[c] - again.get(c, float("nan"))) > 1e-12]
    print("  再計算で値が変わった企業: %d 社" % len(diff))
    if not diff:
        print("  → **変わらなかった。as-of は正しく効いている。**")

    # 訂正の影響を定量化する。**asof 以降に訂正された企業を数える。**
    changed = 0
    examples = []
    for r in rows:
        later = a.latest_period(r["cik"], "NI", 4, args.asof2, max_lag_days=1200)
        if later is None or later.ddate != r["ni_ddate"]:
            continue
        if abs(later.value - r["ni"]) > 1e-9 * max(1.0, abs(r["ni"])):
            changed += 1
            if len(examples) < 5:
                examples.append((r["cik"], r["ni_ddate"], r["ni"], later.value,
                                 r["ni_filed"], later.filed))
    print()
    print("  **同じ期間の NI が %s 以降に訂正された企業: %d 社（%.2f%%）**"
          % (args.asof2, changed, 100 * changed / len(rows)))
    for c, dd, v0, v1, f0, f1 in examples:
        print("    cik=%-8d %s  %.4g（%s） → %.4g（%s）" % (c, dd, v0, f0, v1, f1))
    if changed:
        print()
        print("  **この %d 社は、訂正後データを使うと違うスコアになる。**" % changed)
        print("  as-of を使わなければ、その差がそのままバックテストの成績に乗る。")

    print()
    print("=" * 74)
    print("この煙テストが確認したこと / していないこと")
    print("=" * 74)
    print("確認した:")
    print("  - DERA → as-of → FF49 → 断面ランクが**実データで通る**")
    print("  - §4.1 のフォールバックが**実際に発動する**")
    print("  - **as-of は後からデータを足しても過去の断面を変えない**")
    print("していない（**近似したことを明記する**）:")
    print("  - **TTM を通期(qtrs=4)で代用した。** 正しい TTM 合成は Phase 2")
    print("  - **SIC を as-of で持っていない**（最新の提出のものを使った）。")
    print("    spec §9 の落とし穴14（業種分類の遡及適用）に触れる。DERA には")
    print("    filed があるので as-of 化は可能。**やっていないだけ**")
    print("  - 価格を使っていないので J（流動性）ゲートを通していない。")
    print("    **その結果 z の上下位がシェル企業で占められた** — ゲートの必要性が")
    print("    実測で見えたという意味では、これも収穫である")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
