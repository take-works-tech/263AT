#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
OQ-24 — 弱いシグナルを束ねたら本当にプラスの期待値が残るのか。

背景
----
tools/analyze_oap.py の結果、個々のシグナルは全期間で年率シャープ 0.06〜0.12、
標本外の |t| 中央値は 0.21 しかなかった。263AT は「多数の弱いシグナルを縮小して束ねる」
（catalog §1.9）という設計だが、**それで本当にプラスになるのかは検証していない。**
ここが否なら、以降の実装は全部無意味になる。

同時に検証する3つの論点
-----------------------
  1. 合成すると個々より良くなるか（分散効果が実際に効くか）
  2. **選択 vs 縮小**（§1.9 の中心的主張の直接検定）
  3. **ロングオンリーでも成立するか** — 263AT は空売りをしない。
     学術のロングショート成績は、個人には半分しか使えない

現実性のための3つの制約
-----------------------
  - ウォークフォワード: 時点 t の重みは t-1 までの情報だけで決める
  - 標本外限定: 各シグナルの原論文の標本終了後だけを使う（公表後の減衰込み）
  - 取引コスト: 月次リバランスのコストを明示的に引く

使い方
    .venv/Scripts/python.exe tools/analyze_combination.py
    .venv/Scripts/python.exe tools/analyze_combination.py --screen ex_nyse_p20_me
"""
from __future__ import annotations

import argparse
import pathlib
import sys

import numpy as np
import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / "research" / "oap_cache"
MIN_TRAIN = 120        # 重み推定に最低10年
MIN_SIGNALS = 20       # その時点で使えるシグナルがこれ未満なら評価しない


def build_matrices(screen, doc):
    """LS とロングオンリーの月次リターン行列（月 × シグナル）を作る。"""
    df = pd.read_parquet(CACHE / ("port_%s.parquet" % screen))
    df["port"] = df["port"].astype(str)
    sign = pd.to_numeric(doc["Sign"], errors="coerce").fillna(1.0)

    # --- ロングショート -----------------------------------------------------
    ls = df[df["port"].str.upper() == "LS"]
    if len(ls):
        LS = ls.pivot_table(index="date", columns="signalname", values="ret")
    else:
        num = df[df["port"].str.fullmatch(r"\d+")].assign(q=lambda x: x["port"].astype(int))
        hi = num.groupby("signalname")["q"].transform("max")
        lo = num.groupby("signalname")["q"].transform("min")
        top = num[num["q"] == hi].pivot_table(index="date", columns="signalname", values="ret")
        bot = num[num["q"] == lo].pivot_table(index="date", columns="signalname", values="ret")
        LS = top - bot

    # --- ロングオンリー（最上位分位 - 全分位平均） ---------------------------
    num = df[df["port"].str.fullmatch(r"\d+")].assign(q=lambda x: x["port"].astype(int))
    hi = num.groupby("signalname")["q"].transform("max")
    lo = num.groupby("signalname")["q"].transform("min")
    top = num[num["q"] == hi].pivot_table(index="date", columns="signalname", values="ret")
    bot = num[num["q"] == lo].pivot_table(index="date", columns="signalname", values="ret")
    univ = num.pivot_table(index="date", columns="signalname", values="ret", aggfunc="mean")
    # Sign が -1 のシグナルは「最下位分位を買う」のが正しい向き
    s = sign.reindex(LS.columns).fillna(1.0)
    LO = pd.DataFrame(index=top.index, columns=top.columns, dtype=float)
    for c in top.columns:
        LO[c] = (top[c] if s.get(c, 1) > 0 else bot[c]) - univ[c]
    LS = LS.mul(s.reindex(LS.columns).fillna(1.0), axis=1)
    return LS.sort_index(), LO.sort_index()


def mask_out_of_sample(R, doc):
    """各シグナルの原論文の標本終了年より後だけを残す。"""
    out = R.copy()
    yrs = pd.to_datetime(out.index).year
    for c in out.columns:
        if c in doc.index and pd.notna(doc.loc[c, "SampleEndYear"]):
            out.loc[yrs <= doc.loc[c, "SampleEndYear"], c] = np.nan
    return out


# ---------------------------------------------------------------- 重み付け方式
def w_equal(train):
    c = train.columns[train.notna().sum() >= MIN_TRAIN // 2]
    w = pd.Series(0.0, index=train.columns)
    if len(c):
        w[c] = 1.0 / len(c)
    return w


def w_invvol(train):
    sd = train.std()
    ok = (train.notna().sum() >= MIN_TRAIN // 2) & sd.notna() & (sd > 0)
    w = pd.Series(0.0, index=train.columns)
    if ok.any():
        iv = 1.0 / sd[ok]
        w[ok] = iv / iv.sum()
    return w


def w_ridge(train, lam):
    """平均分散に縮小をかける。lam が大きいほど等加重に近づく（KNS 流）。"""
    c = train.columns[train.notna().sum() >= MIN_TRAIN // 2]
    if len(c) < MIN_SIGNALS:
        return pd.Series(0.0, index=train.columns)
    X = train[c].fillna(0.0)
    mu = X.mean().values
    S = np.cov(X.values, rowvar=False)
    S = S + lam * np.trace(S) / len(c) * np.eye(len(c))
    try:
        raw = np.linalg.solve(S, mu)
    except np.linalg.LinAlgError:
        return w_equal(train)
    raw = np.clip(raw, 0, None)                  # ロングオンリー制約（負の重みを許さない）
    w = pd.Series(0.0, index=train.columns)
    if raw.sum() > 0:
        w[c] = raw / raw.sum()
    return w


def w_select(train, thr):
    """t 値が閾値を超えたものだけを等加重で採用（＝選択）。§1.9 が否定する方式。"""
    n = train.notna().sum()
    t = train.mean() / train.std() * np.sqrt(n)
    ok = (n >= MIN_TRAIN // 2) & (t > thr)
    w = pd.Series(0.0, index=train.columns)
    if ok.any():
        w[ok] = 1.0 / ok.sum()
    return w


def w_select_topk(train, k):
    n = train.notna().sum()
    t = (train.mean() / train.std() * np.sqrt(n)).where(n >= MIN_TRAIN // 2)
    top = t.nlargest(k).index
    w = pd.Series(0.0, index=train.columns)
    if len(top):
        w[top] = 1.0 / len(top)
    return w


SCHEMES = [
    ("等加重（全部使う）", w_equal),
    ("逆ボラ加重", w_invvol),
    ("縮小 ridge λ=1.0", lambda tr: w_ridge(tr, 1.0)),
    ("縮小 ridge λ=0.3", lambda tr: w_ridge(tr, 0.3)),
    ("縮小 ridge λ=0.1", lambda tr: w_ridge(tr, 0.1)),
    ("選択 t>2.0", lambda tr: w_select(tr, 2.0)),
    ("選択 t>3.0", lambda tr: w_select(tr, 3.0)),
    ("選択 上位20本", lambda tr: w_select_topk(tr, 20)),
    ("選択 上位5本", lambda tr: w_select_topk(tr, 5)),
]


def walk_forward(R, scheme, step=12, min_positions=5):
    """step ヶ月ごとに重みを再推定し、次の step ヶ月をその重みで運用する。

    返す本数は「使えたシグナル数」ではなく **有効本数 1/sum(w^2)**。
    ridge に非負制約を入れると実質的に本数が絞られるので、そこを見ないと
    「縮小」と「選択」の比較が成立しない。
    """
    idx = R.index
    rets, dates, eff, turn = [], [], [], []
    prev_w = None
    for i in range(MIN_TRAIN, len(idx), step):
        train = R.iloc[:i]
        if train.notna().sum().gt(MIN_TRAIN // 2).sum() < MIN_SIGNALS:
            continue
        w = scheme(train)
        if w.sum() <= 0:
            continue
        blk = R.iloc[i:i + step]
        for d, row in blk.iterrows():
            ok = row.notna() & (w > 0)
            if ok.sum() < min_positions:
                continue
            ww = w[ok] / w[ok].sum()
            rets.append(float((row[ok] * ww).sum()))
            dates.append(d)
            eff.append(1.0 / float((ww ** 2).sum()))
        full = w.reindex(R.columns).fillna(0.0)
        full = full / full.sum() if full.sum() > 0 else full
        turn.append(0.0 if prev_w is None else float((full - prev_w).abs().sum() / 2))
        prev_w = full
    return (pd.Series(rets, index=pd.DatetimeIndex(dates)),
            (np.mean(eff) if eff else 0), (np.mean(turn) if turn else 0))


def perf(r, cost_bps_month=0.0):
    """月次%系列を年率指標にする。cost は月あたりの控除（%）。"""
    if len(r) < 24:
        return None
    net = r - cost_bps_month
    mu, sd = net.mean(), net.std()
    cum = (1 + net / 100).cumprod()
    dd = (cum / cum.cummax() - 1).min()
    return {"n": len(net), "ann_ret": mu * 12, "ann_vol": sd * np.sqrt(12),
            "sharpe": mu / sd * np.sqrt(12) if sd > 0 else np.nan,
            "maxdd": dd * 100, "t": mu / sd * np.sqrt(len(net)) if sd > 0 else np.nan}


def report(title, R, doc, costs):
    print("\n" + "=" * 100)
    print(title)
    print("=" * 100)
    ind = []
    for c in R.columns:
        s = R[c].dropna()
        if len(s) >= 60 and s.std() > 0:
            ind.append(s.mean() / s.std() * np.sqrt(12))
    if ind:
        print("  単体シグナルの年率シャープ: 中央値 %.3f / 上位10%% %.3f / 正の割合 %.0f%% / n=%d"
              % (np.median(ind), np.percentile(ind, 90), 100 * np.mean(np.array(ind) > 0), len(ind)))

    # 全方式を回してから共通の評価期間に揃える。期間が違うと比較にならない
    raw = {}
    for name, fn in SCHEMES:
        r, eff, tv = walk_forward(R, fn)
        if len(r) >= 24:
            raw[name] = (r, eff, tv)
    if not raw:
        print("  評価できる方式がない")
        return pd.DataFrame()
    t0 = max(r.index.min() for r, _, _ in raw.values())
    t1 = min(r.index.max() for r, _, _ in raw.values())
    print("  共通評価期間 %s 〜 %s に揃えて比較する" % (t0.date(), t1.date()))

    hdr = "  %-20s %8s %8s %5s %8s %8s %8s %8s" % (
        "方式", "有効本数", "重み回転", "月数", "年率%", "ボラ%", "シャープ", "最大DD%")
    for cb in costs:
        hdr += " %12s" % ("SR(月%.2f%%)" % cb)
    print("\n" + hdr)
    rows = []
    for name, (r, eff, tv) in raw.items():
        rr = r[(r.index >= t0) & (r.index <= t1)]
        p = perf(rr)
        if p is None:
            print("  %-20s （共通期間では月数不足）" % name)
            continue
        line = "  %-20s %8.1f %8.2f %5d %8.2f %8.2f %8.3f %8.1f" % (
            name, eff, tv, p["n"], p["ann_ret"], p["ann_vol"], p["sharpe"], p["maxdd"])
        rec = {"scheme": name, "eff_n": eff, "weight_turnover": tv, **p}
        for cb in costs:
            pc = perf(rr, cb)
            line += " %12.3f" % pc["sharpe"]
            rec["sharpe_cost%.2f" % cb] = pc["sharpe"]
        print(line)
        rows.append(rec)
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--screen", default="op")
    ap.add_argument("--costs", nargs="*", type=float, default=[0.03, 0.10, 0.25])
    a = ap.parse_args()

    doc = pd.read_parquet(CACHE / "signal_doc.parquet").set_index("Acronym")
    LS, LO = build_matrices(a.screen, doc)

    print("OQ-24  弱いシグナルを束ねたら本当にプラスになるのか")
    print("screen = %s   コスト仮定 = 月 %s%%" % (a.screen, " / ".join("%.2f" % c for c in a.costs)))
    print("\n※ コストは月次リバランスを仮定した控除。263AT は 6M〜5Y 保有なので実際の回転率は")
    print("   これよりずっと低い。ここで示すのは「月次で回したらどうなるか」の上限側の悲観ケース。")

    out = {}
    out["LS_full"] = report("A. ロングショート・全期間（学術の標準的な見方。楽観側）", LS, doc, a.costs)
    out["LO_full"] = report("B. ロングオンリー・全期間（263AT は空売りしない）", LO, doc, a.costs)
    out["LS_oos"] = report("C. ロングショート・標本外のみ（公表後の減衰込み）",
                           mask_out_of_sample(LS, doc), doc, a.costs)
    out["LO_oos"] = report("D. ロングオンリー・標本外のみ（**263AT に最も近い条件**）",
                           mask_out_of_sample(LO, doc), doc, a.costs)

    print("\n" + "=" * 92)
    print("判定")
    print("=" * 92)
    d = out["LO_oos"]
    if len(d):
        best = d.loc[d["sharpe"].idxmax()]
        ew = d[d["scheme"] == "等加重（全部使う）"]
        sel = d[d["scheme"].str.startswith("選択")]
        print("  D（最も現実に近い条件）での最良: %s  シャープ %.3f  年率 %.2f%%"
              % (best["scheme"], best["sharpe"], best["ann_ret"]))
        if len(ew) and len(sel):
            print("  縮小系の最良シャープ %.3f  vs  選択系の最良シャープ %.3f"
                  % (d[~d["scheme"].str.startswith("選択")]["sharpe"].max(),
                     sel["sharpe"].max()))
        for cb in a.costs:
            col = "sharpe_cost%.2f" % cb
            n_pos = (d[col] > 0).sum()
            print("  月次コスト %.2f%% を引いた後にシャープが正: %d / %d 方式" % (cb, n_pos, len(d)))
    pd.concat(out, names=["case"]).to_csv(ROOT / "research" / "oq24_combination.csv", encoding="utf-8")
    print("\n  → research/oq24_combination.csv")
    return 0


if __name__ == "__main__":
    sys.exit(main())
