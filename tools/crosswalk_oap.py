#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
263AT のパラメータと Open Source Asset Pricing のシグナルを対応づけ、
q11（実証度 T1〜T4 と一次文献）を機械的に埋める。

対応の付け方
------------
1. カタログの注意点欄に `OSAP \\`Acronym\\`` と書いてあるもの → 自動対応（v0.4 で追加した86件）
2. params/_oap_crosswalk.yaml の手動対応表 → 既存パラメータ
   （名前の類似度で機械的に当てると誤対応が出るので、手で書く）

出力
----
  params/_overrides_oap.yaml   … evidence_tier / references の追記案
  レポート                      … 未対応の OSAP Predictor（＝カタログの抜け漏れ候補）

使い方
    .venv/Scripts/python.exe tools/crosswalk_oap.py            # レポートのみ
    .venv/Scripts/python.exe tools/crosswalk_oap.py --write    # 上書きファイルを生成
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

import pandas as pd
import yaml

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
DOC = ROOT / "research" / "oap_cache" / "signal_doc.parquet"
XWALK = ROOT / "params" / "_oap_crosswalk.yaml"
OUT = ROOT / "params" / "_overrides_oap.yaml"
OAP_URL = "https://www.openassetpricing.com/"
CZ_PDF = "https://www.federalreserve.gov/econres/feds/files/2021-037pap.pdf"


def tier_of(row):
    """OSAP の再現品質と予測力から T1〜T3 を決める。"""
    cat = row.get("Cat.Signal")
    rep = str(row.get("Signal Rep Quality") or "")
    pred = str(row.get("Predictability in OP") or "")
    if cat != "Predictor":
        return "T3"
    if rep.startswith("1_good") and pred.startswith("1_clear"):
        return "T1"
    if rep.startswith(("1_good", "2_fair")) and pred.startswith(("1_clear", "2_likely")):
        return "T2"
    return "T3"


def load_params():
    rows = []
    for f in sorted((ROOT / "params").glob("[A-Z].yaml")):
        rows += yaml.safe_load(f.read_text(encoding="utf-8")) or []
    return {r["id"]: r for r in rows}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    doc = pd.read_parquet(DOC).set_index("Acronym")
    params = load_params()

    # --- 1. 注意点欄からの自動対応 ------------------------------------------
    auto = {}
    for pid, p in params.items():
        for note in [p.get("notes") or "", p.get("notes_extra") or ""]:
            for ac in re.findall(r"OSAP\s+`?([A-Za-z_0-9]+)`?", note):
                if ac in doc.index:
                    auto.setdefault(pid, []).append(ac)
    print("### 注意点欄からの自動対応: %d パラメータ / %d 対応"
          % (len(auto), sum(len(v) for v in auto.values())))

    # --- 2. 手動対応表 --------------------------------------------------------
    manual = {}
    if XWALK.exists():
        manual = yaml.safe_load(XWALK.read_text(encoding="utf-8")) or {}
        manual = {k: (v if isinstance(v, list) else [v]) for k, v in manual.items()}
        bad = {k: [a for a in v if a not in doc.index] for k, v in manual.items()}
        bad = {k: v for k, v in bad.items() if v}
        if bad:
            print("  ⚠ 手動対応表に存在しない Acronym:", bad)
        missing_pid = [k for k in manual if k not in params]
        if missing_pid:
            print("  ⚠ 手動対応表に存在しないパラメータID:", missing_pid)
    print("### 手動対応表: %d パラメータ" % len(manual))

    mapping = collections.defaultdict(list)
    for src in (auto, manual):
        for pid, acs in src.items():
            for ac in acs:
                if pid in params and ac in doc.index and ac not in mapping[pid]:
                    mapping[pid].append(ac)

    # --- 3. 上書き案の生成 ----------------------------------------------------
    ov, tiers = {}, collections.Counter()
    contradict = []
    for pid, acs in sorted(mapping.items()):
        best = sorted(acs, key=lambda a: ["T1", "T2", "T3"].index(tier_of(doc.loc[a])))
        row = doc.loc[best[0]]
        tier = tier_of(row)
        tiers[tier] += 1
        refs = [OAP_URL, CZ_PDF]
        for a in best[:3]:
            r = doc.loc[a]
            t = pd.to_numeric(r.get("T-Stat"), errors="coerce")
            refs.append("OSAP %s — %s %s, %s（OSAP再現 t=%s, 月次%s%%, 品質=%s, 予測力=%s）"
                        % (a, r.get("Authors"), r.get("Year"), r.get("Journal"),
                           ("%.1f" % abs(t)) if pd.notna(t) else "n/a",
                           r.get("Return"), r.get("Signal Rep Quality"),
                           r.get("Predictability in OP")))
        ov[pid] = {"evidence_tier": tier, "references": refs}
        # カタログの★と OSAP の判定が食い違うもの
        stars = params[pid].get("evidence_stars")
        if stars == 3 and tier != "T1":
            contradict.append((pid, "★★★ だが OSAP 判定は %s" % tier))
        if str(row.get("Predictability in OP")).startswith("4_not") and (stars or 0) >= 2:
            contradict.append((pid, "★%d だが OSAP は予測力なし判定" % stars))

    print("\n### 対応がついたパラメータ: %d / %d" % (len(ov), len(params)))
    print("  実証度の内訳:", dict(tiers))
    if contradict:
        print("\n### カタログの★と OSAP 判定の食い違い（要確認 %d件）" % len(contradict))
        for pid, msg in contradict[:20]:
            print("   %-5s %s  (%s)" % (pid, msg, params[pid]["name_ja"][:26]))

    # --- 4. 未対応の OSAP Predictor = カタログの抜け漏れ候補 ------------------
    used = {a for v in mapping.values() for a in v}
    pred = doc[doc["Cat.Signal"] == "Predictor"].copy()
    pred["t"] = pd.to_numeric(pred["T-Stat"], errors="coerce").abs()
    unmapped = pred[~pred.index.isin(used)].sort_values("t", ascending=False)
    print("\n### 未対応の OSAP Predictor: %d / %d （カタログの抜け漏れ候補）"
          % (len(unmapped), len(pred)))
    for ac, r in unmapped.head(25).iterrows():
        print("   %-26s t=%-5s %-16s %s"
              % (ac, ("%.1f" % r["t"]) if pd.notna(r["t"]) else "-",
                 str(r["Cat.Economic"])[:16], str(r["LongDescription"])[:44]))
    if len(unmapped) > 25:
        print("   ... 他 %d 件（research/oap_unmapped.csv）" % (len(unmapped) - 25))
    unmapped[["Cat.Economic", "LongDescription", "Authors", "Year", "t",
              "Signal Rep Quality", "Predictability in OP"]].to_csv(
        ROOT / "research" / "oap_unmapped.csv", encoding="utf-8")

    if args.write:
        hdr = ("# 263AT — OSAP との対応から機械生成した q11（実証度・一次文献）の上書き案\n"
               "# GENERATED by tools/crosswalk_oap.py\n"
               "#\n"
               "# 対応元: params/_oap_crosswalk.yaml（手動）+ カタログ注意点欄の 'OSAP `Acronym`' 記載\n"
               "# evidence_tier の規則:\n"
               "#   T1 = Predictor かつ 再現品質 1_good かつ 予測力 1_clear\n"
               "#   T2 = Predictor かつ 品質 good/fair かつ 予測力 clear/likely\n"
               "#   T3 = それ以外\n"
               "# **これは q11 だけを埋める。q06/q09/q10 は人が書く必要がある。**\n")
        OUT.write_text(hdr + yaml.safe_dump(ov, allow_unicode=True, sort_keys=True,
                                            default_flow_style=False, width=10**6),
                       encoding="utf-8")
        print("\n  → %s に %d 件を書き出し" % (OUT.relative_to(ROOT), len(ov)))
        print("     params/_overrides.yaml と併せて build_registry.py が読み込む")
    return 0


if __name__ == "__main__":
    sys.exit(main())
