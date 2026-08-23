#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ファクター残差から作るパラメータ（Phase 0、断面が要る層）。

`src/factors.py` が作る MKT / SMB / HML の日次系列と、
銘柄の日次リターンを**日付で揃えて**渡すと、次を返す。

| ID | 名前 | 定義 | 窓 | 符号 |
|---|---|---|---|---|
| **I26** | Frazzini-Pedersen ベータ | 相関とボラを別々に推定し 1 へ縮小 | 相関5年 / ボラ1年 | − |
| **I29** | 3ファクター残差歪度 | FF3 残差の歪度 | 252日 | − |
| **I04** | 特異ボラティリティ (IVOL) | FF3 残差の sd（年率） | 60日 | − |
| **G10** | 残差モメンタム | FF3 残差の累積（12-1ヶ月） | 推定3年 | + |
| **I08** | 特異歪度 | **市場モデル**残差の歪度 | 252日 | − |
| **I27** | 共歪度 | 市場リターンの2乗への感応度 | 252日 | − |

**I26 は I05（素のベータ）より遥かに強い**（OSAP t=7.1 対 —）。
カタログの備考に「BAB の正式な推定法」と書いた通り、
**素のベータは推定誤差が大きいので、こちらを主に使う。**

I08 と I29 の切り分け — **カタログが曖昧なので、ここで決める**
------------------------------------------------------------
カタログの定義は
    I08 特異歪度      「残差リターンの歪度（252日）」  ← **モデルを書いていない**
    I29 3ファクター残差歪度「FF3 回帰残差の歪度」        ← FF3 と明記

**同じモデルを使うと2本が同一になる。** そこで
**I08 = 市場モデル（1ファクター）残差、I29 = FF3 残差**と決めた。
根拠は I29 だけが FF3 と明記されていること。
**これは私の判断であって、カタログに書かれていたことではない。** OQ に残す。

I05（素のベータ）をここで作らない理由
------------------------------------
カタログの定義が「**Vasicek 収縮**」を含んでいる。
Vasicek 収縮は**断面の分散が要る**（各銘柄のベータを、断面平均に向けて
推定精度に応じて縮める）ので、**1銘柄だけでは作れない。**
`vasicek()` を用意したので、**断面が揃う層（build_panel）で掛ける。**
ここで素のベータを I05 として返すと、**定義と違うものに I05 という名前が付く。**

自己テスト
    python src/params_fx.py
"""
from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import factors as FC          # type: ignore  # noqa: E402
from params_us import Value   # type: ignore  # noqa: E402

W_SHORT = 60        # I04
W_LONG = 252        # I08 / I27 / I29 / G10
W_FP_VOL = 252      # I26 のボラ推定（1年）
W_FP_COR = 252 * 5  # I26 の相関推定（5年）
W_G10_EST = 252 * 3  # G10 の推定窓。**累積窓（252日）より長くする**
FP_OVERLAP = 3      # I26 の相関は3日リターンで測る（非同期取引の補正）
FP_SHRINK = 0.6     # β_final = 0.6 β + 0.4 × 1（Frazzini-Pedersen の値）
M1 = 21


def align(dates: list[str], rets: list[float | None],
          fac: list[FC.Day]) -> tuple[list[float], list[list[float]], list[str]]:
    """銘柄のリターンとファクターを**日付で突き合わせる。**

    **位置で揃えてはいけない。** 上場日・休場日・停止日がずれるので、
    位置で合わせると**別の日のファクターに回帰することになる。**
    銘柄側が欠損（停止・出来高0）の日は落とす。
    """
    by = {d.date: d for d in fac}
    y: list[float] = []
    xs: list[list[float]] = [[], [], []]
    ds: list[str] = []
    for d, r in zip(dates, rets):
        f = by.get(d)
        if f is None or r is None:
            continue
        y.append(r)
        xs[0].append(f.mkt)
        xs[1].append(f.smb)
        xs[2].append(f.hml)
        ds.append(d)
    return y, xs, ds


def _tail(y, xs, n):
    return y[-n:], [c[-n:] for c in xs]


def _resid(y: list[float], xs: list[list[float]],
           n_factors: int) -> list[float] | None:
    got = FC.regress(y, xs[:n_factors])
    return None if got is None else got[1]


def i04(y, xs) -> float | None:
    """特異ボラティリティ。**FF3 残差の sd を年率化する。**

    I01（生のボラ）と同じく年率にする — 単位を揃えないと
    正規化の前に比較したときに読み違える。
    """
    yy, xx = _tail(y, xs, W_SHORT)
    if len(yy) < W_SHORT // 2:
        return None
    r = _resid(yy, xx, 3)
    if r is None:
        return None
    m = FC.moments(r)
    return None if m is None else m[0] * math.sqrt(252.0)


def i29(y, xs) -> float | None:
    """3ファクター残差歪度。**§1.8 の宝くじ回避の主力。**"""
    yy, xx = _tail(y, xs, W_LONG)
    if len(yy) < W_LONG // 2:
        return None
    r = _resid(yy, xx, 3)
    if r is None:
        return None
    m = FC.moments(r)
    return None if m is None else m[1]


def i08(y, xs) -> float | None:
    """特異歪度。**市場モデル（1ファクター）残差**の歪度（冒頭の注記）。"""
    yy, xx = _tail(y, xs, W_LONG)
    if len(yy) < W_LONG // 2:
        return None
    r = _resid(yy, xx, 1)
    if r is None:
        return None
    m = FC.moments(r)
    return None if m is None else m[1]


def i27(y, xs) -> float | None:
    """共歪度。**市場リターンの2乗に対する感応度。**

    `r_i = a + b·r_m + c·r_m² + ε` の `c`。
    市場が大きく動くときに一緒に壊れるか、を測る。
    """
    yy, xx = _tail(y, xs, W_LONG)
    if len(yy) < W_LONG // 2:
        return None
    mkt = xx[0]
    got = FC.regress(yy, [mkt, [m * m for m in mkt]])
    return None if got is None else got[0][2]


def g10(y, xs) -> float | None:
    """残差モメンタム。**推定窓と累積窓を分ける。**

    ここは間違えやすい。**OLS 残差は総和が厳密に 0 になる。**
    推定窓と累積窓を同じにすると、

        累積（全体） = 0
        累積（直近1ヶ月を除く） = −（直近1ヶ月の残差の和）

    となり、**残差モメンタムではなく短期反転**が出来上がる。
    最初にそう書いて、自己テストが「直近に残差が積み上がれば正になる」で
    落ちて気づいた。**符号が逆のものに G10 という名前が付くところだった。**

    Blitz-Huij-Martens に従い、**推定は3年、累積は 12-1 ヶ月**とする。
    累積窓は推定窓の一部分なので、和は 0 にならない。
    **カタログは「累積」としか書いていないので、
    Blitz らの標準化（残差 sd で割る）は行っていない。** OQ に残す。
    """
    yy, xx = _tail(y, xs, W_G10_EST)
    if len(yy) < W_G10_EST:
        return None
    r = _resid(yy, xx, 3)
    if r is None:
        return None
    # 末尾が t。**t-252 〜 t-21 の区間だけを足す**
    seg = r[len(r) - W_LONG: len(r) - M1]
    if len(seg) < W_LONG - M1:
        return None
    return sum(seg)


def _overlap(xs: list[float], k: int) -> list[float]:
    """重なり合う k 日リターン（対数の和）。**非同期取引の補正。**"""
    if len(xs) < k:
        return []
    return [sum(xs[i: i + k]) for i in range(len(xs) - k + 1)]


def i26(y, xs) -> float | None:
    """Frazzini-Pedersen ベータ。**BAB の正式な推定法。**

        β = ρ × (σ_i / σ_m)   →   0.6 β + 0.4 × 1

    **相関とボラを別々の窓で測る**のが要点。
    相関は動きが遅いので長い窓（5年、3日リターン）、
    ボラは速いので短い窓（1年、日次）。
    **同じ窓で共分散を1回で推定すると、この分離ができない。**

    最後に 1 へ縮小するのは、**素のベータの推定誤差が大きい**から
    （カタログ I05 の備考と同じ理由。ただし収縮の形が違う）。
    """
    if len(y) < W_FP_VOL:
        return None
    mkt = xs[0]
    # ボラは直近1年、日次
    yi, ym = y[-W_FP_VOL:], mkt[-W_FP_VOL:]
    mi = FC.moments(yi)
    mm = FC.moments(ym)
    if mi is None or mm is None or mm[0] <= 0:
        return None
    sd_i, sd_m = mi[0], mm[0]
    if sd_i <= 0:
        return None
    # 相関は最大5年、3日の重なりリターン
    yl, ml = y[-W_FP_COR:], mkt[-W_FP_COR:]
    if len(yl) < W_FP_VOL:
        return None
    a, b = _overlap(yl, FP_OVERLAP), _overlap(ml, FP_OVERLAP)
    if len(a) < 30:
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return None
    rho = sum((x - ma) * (z - mb) for x, z in zip(a, b)) / math.sqrt(va * vb)
    beta = rho * (sd_i / sd_m)
    return FP_SHRINK * beta + (1.0 - FP_SHRINK) * 1.0


def vasicek(betas: list[float], ses: list[float]) -> list[float] | None:
    """Vasicek 収縮。**断面が要るのでここは別関数。**

        w_i = σ²_断面 / (σ²_断面 + se_i²)
        β_i^收縮 = w_i β_i + (1 − w_i) β_断面平均

    **推定精度が悪い銘柄ほど、断面平均に強く引き寄せる。**
    I05 の定義がこれを含むので、素のベータを I05 と呼んではいけない。
    """
    n = len(betas)
    if n < 2 or len(ses) != n:
        return None
    mu = sum(betas) / n
    var_x = sum((b - mu) ** 2 for b in betas) / (n - 1)
    if var_x <= 0:
        return None
    out = []
    for b, se in zip(betas, ses):
        w = var_x / (var_x + max(se, 0.0) ** 2)
        out.append(w * b + (1.0 - w) * mu)
    return out


PARAMS = {"I04": i04, "I08": i08, "I26": i26, "I27": i27,
          "I29": i29, "G10": g10}

NEEDS = {"I04": W_SHORT, "I08": W_LONG, "I27": W_LONG, "I29": W_LONG,
         "G10": W_G10_EST, "I26": W_FP_VOL}


def compute_all(dates: list[str], rets: list[float | None],
                fac: list[FC.Day]) -> dict[str, Value]:
    """全部まとめて計算する。**揃わなかった理由を持ち歩く。**"""
    y, xs, _ = align(dates, rets, fac)
    out: dict[str, Value] = {}
    for pid, f in PARAMS.items():
        if len(y) < NEEDS[pid]:
            out[pid] = Value(pid, None, "揃った日数が足りない（%d 必要、%d）"
                             % (NEEDS[pid], len(y)))
            continue
        try:
            v = f(y, xs)
        except Exception as e:                    # pragma: no cover
            out[pid] = Value(pid, None, "計算で例外: %s" % e)
            continue
        if v is None:
            out[pid] = Value(pid, None, "回帰が解けない、または入力が欠損")
        elif not math.isfinite(v):
            out[pid] = Value(pid, None, "有限でない値")
        else:
            out[pid] = Value(pid, float(v))
    return out


# ---------------------------------------------------------------- self-test
def _test() -> int:
    import random
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/params_fx.py 自己テスト")
    print("-" * 80)

    random.seed(7)
    n = 252 * 6
    import datetime as dt
    d0 = dt.date(2015, 1, 1)
    dates, fac = [], []
    mkts = []
    for k in range(n):
        d = (d0 + dt.timedelta(days=k)).isoformat()
        m = random.gauss(0.0004, 0.011)
        mkts.append(m)
        dates.append(d)
        fac.append(FC.Day(date=d, mkt=m, smb=random.gauss(0, 0.005),
                          hml=random.gauss(0, 0.005), n=100))

    # β=1.5、残差は対称
    rets = [1.5 * fac[k].mkt + random.gauss(0, 0.008) for k in range(n)]

    # --- 突き合わせ ---------------------------------------------------------
    y, xs, ds = align(dates, rets, fac)
    check("**日付で突き合わせる**", len(y) == n and len(xs) == 3)
    y2, _, ds2 = align(dates, [None] * 10 + rets[10:], fac)
    check("**銘柄側の欠損日は落とす**", len(y2) == n - 10)
    shifted = [FC.Day(date=(d0 + dt.timedelta(days=k + 500)).isoformat(),
                      mkt=0.0, smb=0.0, hml=0.0, n=9) for k in range(n)]
    y3, _, _ = align(dates, rets, shifted)
    check("**日付が合わない日は捨てる（位置で揃えない）**", len(y3) < n)

    # --- I26 ----------------------------------------------------------------
    b = i26(y, xs)
    # 0.6×1.5 + 0.4 = 1.3
    check("**FP ベータが真値の周りに出る（0.6β+0.4）**",
          b is not None and 1.15 < b < 1.45)
    check("**1 へ縮小されている（素の 1.5 より小さい）**", b < 1.5)
    flat = [0.0] * n
    check("**市場が動かなければ作らない**", i26(flat, [flat, flat, flat]) is None)

    # ベータが大きいほど FP ベータも大きい
    hi = [3.0 * fac[k].mkt + random.gauss(0, 0.008) for k in range(n)]
    yh, xh, _ = align(dates, hi, fac)
    check("**ベータが大きい銘柄は FP ベータも大きい**", i26(yh, xh) > b)

    # --- I04 / I29 / I08 ----------------------------------------------------
    v = i04(y, xs)
    check("**IVOL が年率で妥当（残差 sd 0.008 → 約 0.13）**",
          v is not None and 0.08 < v < 0.20)
    sk = i29(y, xs)
    check("**対称な残差なら歪度はほぼ 0**", sk is not None and abs(sk) < 0.5)

    # 右に裾を作ると歪度が正になる
    lot = [1.0 * fac[k].mkt + (0.25 if k % 97 == 0 else 0.0)
           + random.gauss(0, 0.004) for k in range(n)]
    yl, xl, _ = align(dates, lot, fac)
    check("**宝くじ的な銘柄は残差歪度が正**", i29(yl, xl) > 0.8)
    check("I08 も同じ向きに動く（モデルが違うだけ）", i08(yl, xl) > 0.8)
    check("**I08 と I29 は同一ではない**", abs(i08(y, xs) - i29(y, xs)) > 1e-12)

    # --- I27 ----------------------------------------------------------------
    co = [1.0 * fac[k].mkt - 8.0 * fac[k].mkt ** 2 for k in range(n)]
    yc, xc, _ = align(dates, co, fac)
    check("**共歪度が負の銘柄を負と判定する**", i27(yc, xc) < -4.0)
    co2 = [1.0 * fac[k].mkt + 8.0 * fac[k].mkt ** 2 for k in range(n)]
    yc2, xc2, _ = align(dates, co2, fac)
    check("正の共歪度も拾う", i27(yc2, xc2) > 4.0)

    # --- G10 ----------------------------------------------------------------
    up = [1.0 * fac[k].mkt + 0.002 for k in range(n)]
    yu, xu, _ = align(dates, up, fac)
    gu = g10(yu, xu)
    check("**残差が一定なら累積は切片に吸収されてほぼ 0**",
          gu is not None and abs(gu) < 0.05)
    # 後半だけ残差が正 → 累積は正（ただし直近1ヶ月は除く）
    late = [1.0 * fac[k].mkt + (0.004 if k > n - 120 else 0.0)
            for k in range(n)]
    yv, xv, _ = align(dates, late, fac)
    check("**直近に残差が積み上がれば正になる**", g10(yv, xv) > 0)

    # --- vasicek ------------------------------------------------------------
    vb = vasicek([0.5, 1.0, 1.5], [0.01, 0.01, 0.01])
    check("**推定が正確なら元の値にほぼ等しい**",
          vb is not None and abs(vb[0] - 0.5) < 0.02)
    vb2 = vasicek([0.5, 1.0, 1.5], [10.0, 10.0, 10.0])
    check("**推定が粗いと断面平均に寄る**", abs(vb2[0] - 1.0) < 0.02)
    check("**断面がばらつかなければ作らない**",
          vasicek([1.0, 1.0], [0.1, 0.1]) is None)
    check("本数が合わなければ None", vasicek([1.0, 2.0], [0.1]) is None)

    # --- compute_all --------------------------------------------------------
    allv = compute_all(dates, rets, fac)
    check("6本すべてを返す", len(allv) == 6)
    check("**すべて作れた**", all(x.value is not None for x in allv.values()))
    short = compute_all(dates[:100], rets[:100], fac[:100])
    n_none = sum(1 for x in short.values() if x.value is None)
    check("**日数が足りなければ理由を返す**", n_none >= 5)
    check("理由に必要日数が入る",
          all("必要" in x.reason for x in short.values() if x.value is None))

    print("-" * 80)
    declared = 24
    if len(ran) != declared:
        fails.append("本数が宣言と違う")
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
