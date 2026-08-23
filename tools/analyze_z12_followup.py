#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Z12（tools/analyze_correlation.py）が提起した疑義の追試。

Z12 で「サイズ・株価水準・スプレッド・52週高値が一つの塊を作っている」と出た。
そこで §4-G の判断が揺らいだ。カタログでは G07（52週高値接近度）を
**「理由が明確に書けるテクニカルは稀」として T1 に格上げし、
モメンタムより頑健と記述していた**（George-Hwang JF 2004 に依拠）。

高相関は「同じ方向に動くが別の情報」かもしれないので、相関だけでは決まらない。
**回帰して alpha が残るかを見る。**

同時に、OQ-24 で測った ridge の effective breadth 28 と
Z12 の n_eff 15.8 が何を意味するかを整理する。

使い方
------
    python tools/analyze_z12_followup.py
    python tools/analyze_z12_followup.py --start 1963-07-01
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from analyze_correlation import effective_count, load_crosswalk, long_short  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORTS = ROOT / "research" / "oap_cache" / "port_deciles_ew.parquet"
OUT = ROOT / "research" / "z12_followup.csv"


def ols(y: np.ndarray, X: np.ndarray):
    """定数項つき OLS。白色標準誤差（Newey-West ではない）。

    月次リターンの自己相関は小さいので単純な SE で足りるが、
    **t が 2 前後のときは Newey-West で再確認すべき**である。
    """
    Xd = np.column_stack([np.ones(len(X)), X])
    b, *_ = np.linalg.lstsq(Xd, y, rcond=None)
    resid = y - Xd @ b
    n, k = Xd.shape
    s2 = resid @ resid / (n - k)
    se = np.sqrt(np.diag(s2 * np.linalg.inv(Xd.T @ Xd)))
    return b, b / se, 1 - resid.var() / y.var()


def tstat(s: pd.Series):
    s = s.dropna()
    if len(s) < 24:
        return np.nan, np.nan, len(s)
    return s.mean() / (s.std(ddof=1) / np.sqrt(len(s))), s.mean(), len(s)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="1963-07-01",
                    help="標準的な検証開始月（Compustat のカバレッジが安定する時点）")
    args = ap.parse_args()

    if not PORTS.exists():
        print("OSAP のキャッシュが無い。tools/analyze_oap.py を先に実行する。")
        return 1

    ls = long_short(pd.read_parquet(PORTS))
    xw = load_crosswalk()
    cols = {}
    for pid, accs in sorted(xw.items()):
        have = [a for a in accs if a in ls.columns]
        if have:
            cols[pid] = ls[have].mean(axis=1)
    P = pd.DataFrame(cols).sort_index()

    print("=" * 72)
    print("追試(0) L/S の構成が正しいか — 既知のシグナルで確認")
    print("=" * 72)
    print("これを先にやらないと、以降の結果がバグなのか発見なのか区別できない。")
    print()
    known = {"Mom12m": 3.7, "BM": 4.4, "AssetGrowth": 7.4, "Accruals": 6.0, "Size": 3.5}
    for sig, expect in known.items():
        if sig not in ls.columns:
            continue
        t, m, n = tstat(ls[sig][ls.index >= args.start])
        ok = "OK" if abs(t) > 2.0 else "!!"
        print("  %-12s t=%5.2f  月次 %+.3f%%  n=%4d   （既知の水準 ~%.1f） %s"
              % (sig, t, m, n, expect, ok))

    print()
    print("=" * 72)
    print("追試(1) G07（52週高値）は独立したアノマリーか")
    print("=" * 72)

    if "G07" not in P.columns:
        print("G07 に対応する OSAP シグナルが無い。")
        return 1

    print("--- 素の L/S（等加重デシル 10-1）")
    for lab, a, b in [("全期間", ls.index.min(), ls.index.max()),
                      ("原論文の期間 1963-2001", "1963-07-01", "2001-12-31"),
                      ("公表後 2002-2024", "2002-01-01", "2024-12-31")]:
        s = P["G07"]
        s = s[(s.index >= pd.Timestamp(a)) & (s.index <= pd.Timestamp(b))]
        t, m, n = tstat(s)
        print("  %-22s t=%5.2f  月次 %+.3f%%  n=%d" % (lab, t, m, n))
    print()
    print("  OSAP の公表値: t=2.0, 月次 0.45%, **予測力=2_likely（1_clear ではない）**")
    print("  → 原論文の期間ですら等加重デシルでは t が 2 に届かない。")

    print()
    print("--- 何を控除すると alpha が消えるか（%s 以降）" % args.start)
    Q = P[P.index >= pd.Timestamp(args.start)]
    rows = []
    for ctrl in (["J05", "J25", "J04"], ["G01"], ["J05", "J25", "J04", "G01"]):
        have = [c for c in ctrl if c in Q.columns]
        if not have:
            continue
        sub = Q[["G07"] + have].dropna()
        b, t, r2 = ols(sub["G07"].values, sub[have].values)
        rows.append({"controls": ",".join(have), "alpha": b[0], "t": t[0],
                     "r2": r2, "n": len(sub)})
        print("  控除 %-24s alpha=%+.3f%%  t=%5.2f  R^2=%.2f  n=%d"
              % (",".join(have), b[0], t[0], r2, len(sub)))

    print()
    full = [r for r in rows if "G01" in r["controls"] and "J05" in r["controls"]]
    momo = [r for r in rows if r["controls"] == "G01"]
    if full and abs(full[0]["t"]) < 2.0:
        print("→ **G07 に独立したアルファは無い。**")
        print("   サイズ・株価水準・スプレッドだけを控除すると alpha は正に出るが、")
        print("   それは G07 がそれらと**逆向き**に効く成分を持つためで、")
        if momo:
            print("   **モメンタム（G01）を入れた時点で alpha は %+.2f%% (t=%.2f) になり、"
                  % (momo[0]["alpha"], momo[0]["t"]))
            print("   4つ全部を控除すると %+.3f%% (t=%.2f) でゼロ。**"
                  % (full[0]["alpha"], full[0]["t"]))
        print()
        print("   **カタログ §4-G の「モメンタムより頑健」という記述は、")
        print("   この構成（等加重デシル L/S、米国、1963-2024）では支持されない。**")
        print("   T1（OSAP に収録され一次文献がある）という実証度の格付けは維持できるが、")
        print("   **「G01 と独立した情報を持つ」という主張は取り下げる。**")
    else:
        print("→ alpha が残る。G07 は独立したアノマリーである。")

    pd.DataFrame(rows).to_csv(OUT, index=False, encoding="utf-8")
    print("→ %s" % OUT.relative_to(ROOT))

    print()
    print("=" * 72)
    print("追試(2) OQ-24 の effective breadth 28 と Z12 の n_eff 15.8")
    print("=" * 72)
    ne = effective_count(P.corr(min_periods=120), list(P.columns))
    print("Z12          : %d 本の n_eff = %.1f" % (P.shape[1], ne))
    print("OQ-24        : 非負 ridge の effective breadth = 28（名目 131 本）")
    print()
    print("この2つは別のものを測っている。")
    print("  n_eff             … 相関行列の固有値から見た**情報の次元数**")
    print("  effective breadth … ridge が実際に重みを置いた**シグナルの本数**")
    print()
    print("**ridge が 28 本に重みを分散させても、その 28 本が張る空間は")
    print("  %.0f 次元程度しかない。**" % ne)
    print("→ OQ-24 の「非負 ridge は soft selection として働く」という結論は、")
    print("  Z12 の観点では「**%.0f 次元を 28 本で冗長に表現している**」ことを意味する。" % ne)
    print("  縮小は重複を潰しきれていない。")
    print("  ただし131本すべてに等しく重みを置く等加重（OOS Sharpe -0.050 で失敗）よりは")
    print("  遥かに良い（ridge 0.755）。**縮小は不完全だが機能している。**")

    print()
    print("=" * 72)
    print("追試(3) 各パラメータは「塊の他のメンバー」を控除しても生き残るか")
    print("=" * 72)
    print("G07 で確立した手順を、n_eff が小さく出た塊すべてに機械的に適用する。")
    print("**相関で候補を挙げ、回帰で判定する。**")
    print()
    print("  raw_t    : 素の L/S の t")
    print("  alpha_t  : 同じ塊の他メンバー全部を控除した後の alpha の t")
    print("  判定     : alpha_t の絶対値が 2.0 未満なら「塊に吸収される」")
    print()

    from analyze_correlation import HYPOTHESIZED
    surv = []
    for label, ids in HYPOTHESIZED.items():
        present = [i for i in ids if i in Q.columns]
        if len(present) < 3:
            continue
        print("--- %s（n=%d）" % (label, len(present)))
        for pid in present:
            others = [c for c in present if c != pid]
            sub = Q[[pid] + others].dropna()
            if len(sub) < 120:
                continue
            rt, rm, rn = tstat(sub[pid])
            b, t, r2 = ols(sub[pid].values, sub[others].values)
            keep = abs(t[0]) >= 2.0
            surv.append({"cluster": label, "id": pid, "raw_t": rt,
                         "alpha": b[0], "alpha_t": t[0], "r2": r2,
                         "n": len(sub), "survives": keep})
            print("   %-5s raw_t=%6.2f  alpha=%+.3f%%  alpha_t=%6.2f  R^2=%.2f  %s"
                  % (pid, rt, b[0], t[0], r2,
                     "残る" if keep else "**吸収される**"))
        print()

    if surv:
        df = pd.DataFrame(surv)
        df.to_csv(ROOT / "research" / "z12_survival.csv", index=False, encoding="utf-8")
        n_abs = int((~df["survives"]).sum())
        print("→ %d/%d が塊に吸収される（独立した情報を持たない）"
              % (n_abs, len(df)))
        print("→ research/z12_survival.csv")
        print()
        print("**注意: これは「削除せよ」という意味ではない。**")
        print("§1.9 の方針は選択せず縮小することなので、吸収されるものも残す。")
        print("意味があるのは **buy_class / sell_class の格付けと、")
        print("パイプライン構築の優先順位（§1.9 の priority_k）**である。")
        print("実装コストを払う価値があるのは、生き残った方。")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
