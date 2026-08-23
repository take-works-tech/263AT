#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Z12 — パラメータ間の相関行列を実測する。

なぜこれが要るか
----------------
770 件の検証で q09（相関する既存パラメータ）は全件に書いたが、
**それは概念的な回答であって実測ではない。**
docs/01_parameter_catalog.md の OQ-38 / OQ-39 は、どちらも
「Z12 の相関行列で確認する」と書いたまま止まっていた。

ここで測るのは、**§1.9（選択せず全部入れて縮める）が機能するかどうか**である。

  同じ現象を測る指標が15本あると、縮小はそれらに重みを分け合わせる。
  15本の塊が1本の独立シグナルと同じ重みしか持たないなら縮小は機能している。
  15本それぞれが独立シグナルと同じ重みを持つなら、塊が重みを占有してしまう。

対象データ
----------
OSAP の等加重デシルポートフォリオ（research/oap_cache/port_deciles_ew.parquet）。
179 シグナル。**実際の firm-level データではなくポートフォリオ・リターンなので、
測っているのは「シグナル同士のリターン相関」であって
「銘柄断面でのスコアの相関」ではない。**
後者は WRDS 契約が要る（docs/03_data_feasibility.md）。
→ **リターン相関は断面相関より弱く出る**傾向があるので、
  ここで高相関が出れば断面ではさらに高いと考えてよい（保守的な下限）。

使い方
------
    python tools/analyze_correlation.py
    python tools/analyze_correlation.py --min-months 240
"""
from __future__ import annotations

import argparse
import pathlib
import re
import sys

import numpy as np
import pandas as pd
import yaml

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORTS = ROOT / "research" / "oap_cache" / "port_deciles_ew.parquet"
CROSSWALK = ROOT / "params" / "_oap_crosswalk.yaml"
CATALOG = ROOT / "docs" / "01_parameter_catalog.md"
OUT_CORR = ROOT / "research" / "z12_correlation.csv"
OUT_CLUSTER = ROOT / "research" / "z12_clusters.csv"

# カタログの検証で「同一現象を測っている」と判定した塊。
# ここに書いた仮説を、実測で検証する。
HYPOTHESIZED = {
    "OQ-39 投資・資金調達・NOA": [
        "C30", "C31", "C35", "C39", "C40",
        "F23", "F24", "F25", "F26", "F27", "F31",
        "E03", "E32", "E33", "B11",
    ],
    "OQ-38 Abarbanell-Bushee": [
        "B22", "B23", "B31", "B35", "B45", "B46",
    ],
    "アクルーアル": ["E01", "E02", "E26", "E27", "E31"],
    "OQ-32 季節性モメンタム": ["G38", "G39", "G40", "G41", "G42", "G43", "G44", "G45"],
    "バリュー": ["A01", "A03", "A04", "A05", "A06", "A44"],
    "無形リターン（H13-H16）": ["H13", "H14", "H15", "H16"],
    "低リスク": ["I04", "I05", "I26", "I31", "I29"],
    "モメンタム": ["G01", "G02", "G32", "G33", "G36"],
}


def load_crosswalk() -> dict:
    """263AT パラメータ ID -> OSAP acronym のリスト。

    _oap_crosswalk.yaml（手書き）と _overrides_oap.yaml（自動生成）の両方を読む。
    """
    m = {}
    for f in (CROSSWALK, ROOT / "params" / "_overrides_oap.yaml"):
        if not f.exists():
            continue
        d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for pid, v in d.items():
            if not re.match(r"^[A-Z]\d{2}$", str(pid)):
                continue
            if isinstance(v, list):
                accs = [str(x) for x in v]
            elif isinstance(v, dict):
                # _overrides_oap.yaml は references に URL と "OSAP Xxx t=..." を書く
                accs = []
                for ref in v.get("references", []) or []:
                    mm = re.search(r"OSAP\s+([A-Za-z_][A-Za-z0-9_]*)", str(ref))
                    if mm:
                        accs.append(mm.group(1))
            else:
                continue
            if accs:
                m.setdefault(str(pid), [])
                for a in accs:
                    if a not in m[str(pid)]:
                        m[str(pid)].append(a)
    return m


def long_short(df: pd.DataFrame) -> pd.DataFrame:
    """デシルポートフォリオから 10-1 のロングショート月次リターンを作る。

    OSAP の port は文字列 '01'..'10'。シグナルによってデシル数が違うので、
    **最小と最大の port を使う**（3分位のシグナルもあるため）。
    """
    df = df.copy()
    df["port"] = df["port"].astype(str).str.strip()
    num = pd.to_numeric(df["port"], errors="coerce")
    df = df[num.notna()].copy()
    df["pn"] = num[num.notna()].astype(int)

    out = {}
    for sig, g in df.groupby("signalname"):
        lo, hi = g["pn"].min(), g["pn"].max()
        if lo == hi:
            continue
        a = g[g["pn"] == hi].set_index("date")["ret"]
        b = g[g["pn"] == lo].set_index("date")["ret"]
        s = (a - b).dropna()
        if len(s) >= 60:
            out[sig] = s
    return pd.DataFrame(out).sort_index()


def summarize_block(corr: pd.DataFrame, ids: list[str], label: str) -> dict:
    """塊の内部相関と、塊の外との相関を比べる。"""
    present = [i for i in ids if i in corr.columns]
    if len(present) < 2:
        return {"label": label, "n": len(present), "note": "対応するOSAPシグナルが2本未満"}
    sub = corr.loc[present, present].values
    iu = np.triu_indices_from(sub, k=1)
    within = np.abs(sub[iu])

    outside = [c for c in corr.columns if c not in present]
    cross = np.abs(corr.loc[present, outside].values).ravel() if outside else np.array([np.nan])

    return {
        "label": label,
        "n": len(present),
        "within_median": float(np.nanmedian(within)),
        "within_max": float(np.nanmax(within)),
        "outside_median": float(np.nanmedian(cross)),
        "ratio": float(np.nanmedian(within) / np.nanmedian(cross)) if np.nanmedian(cross) else np.nan,
        "members": ",".join(present),
    }


def effective_count(corr: pd.DataFrame, ids: list[str]) -> float:
    """実効的な独立本数。固有値から求める participation ratio。

        n_eff = (sum lambda)^2 / sum(lambda^2)

    完全に独立なら n、完全に同一なら 1 になる。
    **「15本の塊が実際には何本分の情報を持つか」**を測るのがこれ。
    """
    present = [i for i in ids if i in corr.columns]
    if len(present) < 2:
        return float(len(present))
    C = corr.loc[present, present].values
    C = np.nan_to_num(C, nan=0.0)
    w = np.linalg.eigvalsh((C + C.T) / 2)
    w = np.clip(w, 0, None)
    if w.sum() <= 0:
        return float(len(present))
    return float(w.sum() ** 2 / (w ** 2).sum())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-months", type=int, default=120,
                    help="共通期間でこの月数未満しか重ならないペアは相関を出さない")
    args = ap.parse_args()

    if not PORTS.exists():
        print("OSAP ポートフォリオのキャッシュが無い。tools/analyze_oap.py を先に実行する。")
        return 1

    print("=" * 70)
    print("Z12 — パラメータ間相関の実測")
    print("=" * 70)

    raw = pd.read_parquet(PORTS)
    ls = long_short(raw)
    print("OSAP シグナル: %d 本、期間 %s 〜 %s"
          % (ls.shape[1], ls.index.min().date(), ls.index.max().date()))

    xw = load_crosswalk()
    print("crosswalk に載っている 263AT パラメータ: %d 件" % len(xw))

    # 263AT パラメータごとに、対応する OSAP シグナルの平均リターン系列を作る。
    # 複数対応する場合は平均する（例: A03 -> BM, BMdec）。
    cols = {}
    for pid, accs in sorted(xw.items()):
        have = [a for a in accs if a in ls.columns]
        if not have:
            continue
        cols[pid] = ls[have].mean(axis=1)
    P = pd.DataFrame(cols).sort_index()
    print("リターン系列が作れた 263AT パラメータ: %d 件" % P.shape[1])

    corr = P.corr(min_periods=args.min_months)
    corr.to_csv(OUT_CORR, encoding="utf-8")
    print("→ %s" % OUT_CORR.relative_to(ROOT))

    print()
    print("-" * 70)
    print("仮説として立てた「塊」が実際にまとまっているか")
    print("-" * 70)
    print("within_median : 塊の内部の |相関| の中央値")
    print("outside_median: 塊のメンバーと塊の外の |相関| の中央値")
    print("n_eff         : 固有値から求めた実効的な独立本数（n に近いほど独立）")
    print()

    rows = []
    for label, ids in HYPOTHESIZED.items():
        s = summarize_block(corr, ids, label)
        if "note" in s:
            print("%-28s n=%d  %s" % (label, s["n"], s["note"]))
            rows.append(s)
            continue
        s["n_eff"] = effective_count(corr, ids)
        rows.append(s)
        print("%-28s n=%2d  within=%.2f (max %.2f)  outside=%.2f  比=%.1fx  n_eff=%.1f"
              % (label, s["n"], s["within_median"], s["within_max"],
                 s["outside_median"], s["ratio"], s["n_eff"]))

    pd.DataFrame(rows).to_csv(OUT_CLUSTER, index=False, encoding="utf-8")
    print("→ %s" % OUT_CLUSTER.relative_to(ROOT))

    # 全体の実効次元。770 本のうち OSAP に対応する分だけだが、
    # 「そもそもこのユニバースに何本分の独立な情報があるか」の目安になる。
    print()
    print("-" * 70)
    print("全体")
    print("-" * 70)
    allids = list(corr.columns)
    ne = effective_count(corr, allids)
    print("対応が取れた %d 本の実効独立本数 n_eff = %.1f （%.0f%%）"
          % (len(allids), ne, 100 * ne / max(len(allids), 1)))

    # 最も強く相関するペア。
    # **crosswalk で同じ OSAP シグナルに対応させたペアは人工物**なので分けて出す。
    # 人工物であること自体は情報である（自分で「同じもの」と判定した証拠）。
    c = corr.abs().where(~np.eye(len(corr), dtype=bool))
    stacked = c.stack().sort_values(ascending=False)
    seen, artifact, genuine = set(), [], []
    for (a, b), v in stacked.items():
        key = tuple(sorted((a, b)))
        if key in seen:
            continue
        seen.add(key)
        shared = set(xw.get(a, [])) & set(xw.get(b, []))
        (artifact if shared else genuine).append((a, b, v, sorted(shared)))

    print()
    print("-" * 70)
    print("人工物: crosswalk で同じ OSAP シグナルに対応させたペア")
    print("-" * 70)
    print("測定ではない。**自分で「同じものだ」と判定した記録**として読む。")
    for a, b, v, sh in artifact[:12]:
        print("  %s - %s  %.2f   共有: %s" % (a, b, v, ",".join(sh)))

    print()
    print("-" * 70)
    print("実測: 別の OSAP シグナルなのに強く相関するペア 上位25")
    print("-" * 70)
    for a, b, v, _ in genuine[:25]:
        mark = "" if a[0] == b[0] else "   **カテゴリ跨ぎ**"
        print("  %s(%s) - %s(%s)  %.2f%s"
              % (a, ",".join(xw.get(a, []))[:22], b, ",".join(xw.get(b, []))[:22], v, mark))

    pd.DataFrame(
        [{"a": a, "b": b, "abs_corr": v, "shared_signal": ",".join(sh),
          "kind": "artifact" if sh else "measured"}
         for a, b, v, sh in artifact + genuine]
    ).sort_values("abs_corr", ascending=False).to_csv(
        ROOT / "research" / "z12_pairs.csv", index=False, encoding="utf-8")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
