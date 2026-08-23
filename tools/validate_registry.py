#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
params/*.yaml を検証し、レビューの進捗を報告する。

エラー（exit 1）になるもの
  - スキーマ違反 / 列挙値の逸脱
  - ID の重複・欠番、カタログとの件数不一致
  - review.status: verified なのに pending が残っている
  - verified なのに必須項目（formula / inputs / 経済的理由 / 一次文献 …）が空
  - correlated_with / inputs が存在しない ID を参照している
  - prior_scale が evidence_stars と整合しない
  - ゲート判定なのに gate_policy がない（verified のみエラー、draft は警告）

使い方
    python tools/validate_registry.py
    python tools/validate_registry.py --report   # 進捗レポートを詳しく出す
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys

import yaml

# Windows コンソール（cp932）でも日本語・記号が落ちないようにする
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
PDIR = ROOT / "params"

REQUIRED_KEYS = [
    "id", "name_ja", "category", "definition", "formula", "inputs", "sign",
    "buy_class", "sell_class", "gate_policy", "horizon", "data_sources",
    "evidence_stars", "prior_scale", "evidence_tier", "markets", "industries",
    "period_convention", "consolidation", "accounting_standard_note", "pit_lag_days",
    "zero_denominator_policy", "missing_policy", "missing_bias", "normalization",
    "nonlinear", "winsorize", "correlated_with", "economic_rationale", "references",
    "notes", "provenance", "review", "version",
]

# verified を名乗るために埋まっていなければならない項目
VERIFIED_REQUIRED = [
    "formula", "inputs", "economic_rationale", "evidence_tier", "references",
    "correlated_with", "missing_bias", "zero_denominator_policy",
    "accounting_standard_note", "period_convention", "consolidation", "markets",
]


def load():
    meta = yaml.safe_load((PDIR / "_meta.yaml").read_text(encoding="utf-8"))
    entries = []
    for f in sorted(PDIR.glob("[A-Z].yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or []
        for e in data:
            e["_file"] = f.name
        entries += data
    return meta, entries


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args()

    meta, entries = load()
    enums = meta["enums"]
    errors, warnings = [], []
    ids = {e["id"] for e in entries}

    def err(e, msg):
        errors.append("%s [%s] %s" % (e["id"], e["_file"], msg))

    def warn(e, msg):
        warnings.append("%s [%s] %s" % (e["id"], e["_file"], msg))

    # --- 件数・ID -----------------------------------------------------------
    if len(entries) != meta["total_parameters"]:
        errors.append("count mismatch: files=%d meta=%d" % (len(entries), meta["total_parameters"]))
    dup = [k for k, v in collections.Counter(e["id"] for e in entries).items() if v > 1]
    if dup:
        errors.append("duplicate ids: %s" % dup)
    bycat = collections.defaultdict(list)
    for e in entries:
        bycat[e["category"]].append(int(e["id"][1:]))
    for c, ns in bycat.items():
        ns.sort()
        if ns != list(range(1, len(ns) + 1)):
            missing = sorted(set(range(1, max(ns) + 1)) - set(ns))
            errors.append("category %s has gaps: %s" % (c, missing))

    # --- 各エントリ ---------------------------------------------------------
    for e in entries:
        for k in REQUIRED_KEYS:
            if k not in e:
                err(e, "missing key: %s" % k)

        if e["sign"] not in enums["sign"]:
            err(e, "bad sign: %r" % e["sign"])
        for k in ("buy_class", "sell_class"):
            if e[k] not in enums["weight_class"]:
                err(e, "bad %s: %r" % (k, e[k]))
        for s in e["data_sources"]:
            if s not in enums["data_sources"]:
                err(e, "bad data_source: %r" % s)
        if e["review"]["status"] not in enums["review_status"]:
            err(e, "bad review.status: %r" % e["review"]["status"])
        if e["normalization"] not in enums["normalization"]:
            err(e, "bad normalization: %r" % e["normalization"])
        if e["missing_policy"] not in enums["missing_policy"]:
            err(e, "bad missing_policy: %r" % e["missing_policy"])
        if e["consolidation"] not in enums["consolidation"]:
            err(e, "bad consolidation: %r" % e["consolidation"])
        if e["period_convention"] not in enums["period_convention"]:
            err(e, "bad period_convention: %r" % e["period_convention"])
        if e["evidence_tier"] not in (None,) + tuple(enums["evidence_tier"]):
            err(e, "bad evidence_tier: %r" % e["evidence_tier"])

        # prior_scale と実証度の整合
        want = meta["prior_scale_by_stars"].get(str(e["evidence_stars"])) if e["evidence_stars"] else None
        if e["prior_scale"] != want:
            err(e, "prior_scale %r != expected %r for stars=%r" % (e["prior_scale"], want, e["evidence_stars"]))

        # ホライズン
        h = e["horizon"]
        if h.get("label") not in ("all", "immediate") and h.get("min_m") is None:
            err(e, "unparsed horizon: %r" % h.get("label"))

        # ∩ / ∪ は非線形変換が要る
        if e["sign"] in ("cap", "cup") and not e["nonlinear"]:
            err(e, "sign=%s but nonlinear is empty" % e["sign"])

        # U カテゴリは業種内正規化
        if e["category"] == "U":
            if e["normalization"] != "rank_industry":
                err(e, "U category must use rank_industry")
            if not e["industries"]:
                err(e, "U category must declare industries")

        # 参照整合
        for k in ("correlated_with", "inputs"):
            v = e.get(k) or []
            if isinstance(v, list):
                for ref in v:
                    if isinstance(ref, str) and re.fullmatch(r"[A-Z]\d{2}", ref) and ref not in ids:
                        err(e, "%s references unknown id %s" % (k, ref))

        # ゲート
        is_gate = "gate" in (e["buy_class"], e["sell_class"])
        if is_gate and not e["gate_policy"]:
            (err if e["review"]["status"] == "verified" else warn)(e, "gate without gate_policy")

        # verified の要件
        if e["review"]["status"] == "verified":
            if e["review"]["pending"]:
                err(e, "verified but pending=%s" % e["review"]["pending"])
            for k in VERIFIED_REQUIRED:
                if e[k] in (None, [], ""):
                    err(e, "verified but %s is empty" % k)
            # URL 以外でも、法令・規則・原論文の明示・実測結果への参照は正当な出典とする。
            # 「構造的制約」だけは根拠として弱いので、他に出典が1つも無い場合のみ警告する。
            OK_PREFIX = ("http", "原論文", "日本:", "米国:", "SEC ", "東証", "実測:", "OSAP ")
            urls = [r for r in e["references"] if isinstance(r, str) and r.startswith(OK_PREFIX)]
            if not urls:
                warn(e, "根拠が構造的判断のみ（実証出典なし）— 欠陥ではないが、「なぜそう決めたか」が人の判断に依存していることを明示するための印")

    # --- 法則とパラメータの対応 -------------------------------------------
    # AssetGrowth の漏れ（LAW-32 を★★★としながらパラメータが1つも無かった）の再発防止。
    # 実証的アノマリー（§3.2）の各法則が、最低1つのパラメータから参照されているかを見る。
    cat_md = (ROOT / "docs" / "01_parameter_catalog.md").read_text(encoding="utf-8")
    sec = cat_md.split("### 3.2")[-1].split("### 3.3")[0] if "### 3.2" in cat_md else ""
    laws_32 = re.findall(r"^\|\s*LAW-(\d{2})\s*\|", sec, re.M)
    referenced = set()
    for e in entries:
        for fld in ("notes", "notes_extra", "economic_rationale", "gate_policy"):
            v = e.get(fld) or ""
            referenced.update(re.findall(r"LAW-(\d{2})", str(v)))
    orphan = [l for l in laws_32 if l not in referenced]
    if orphan:
        names = {}
        for l in orphan:
            m = re.search(r"^\|\s*LAW-" + l + r"\s*\|\s*([^|]+)\|", sec, re.M)
            names[l] = m.group(1).strip() if m else "?"
        warnings.append("法則→パラメータの対応リンクが無い実証アノマリー"
                        "（パラメータが存在しない可能性と、単に注記でLAW番号に触れていないだけの可能性がある。両方とも直すべき）: "
                        + ", ".join("LAW-%s(%s)" % (l, names[l][:20]) for l in orphan))

    # --- レポート -----------------------------------------------------------
    n = len(entries)
    verified = sum(1 for e in entries if e["review"]["status"] == "verified")
    reviewed = sum(1 for e in entries if e["review"]["status"] == "reviewed")
    pend = collections.Counter()
    for e in entries:
        for q in e["review"]["pending"]:
            pend[q] += 1
    prov = collections.Counter()
    for e in entries:
        for v in e["provenance"].values():
            prov[v] += 1

    print("=" * 66)
    print("263AT parameter registry")
    print("=" * 66)
    print("parameters      : %d" % n)
    print("verified        : %d (%.1f%%)" % (verified, 100.0 * verified / n))
    print("reviewed        : %d" % reviewed)
    print("draft           : %d" % (n - verified - reviewed))
    print("field provenance: " + "  ".join("%s=%d" % (k, v) for k, v in sorted(prov.items())))
    print()
    print("open questions (残っている問い / %d件中)" % n)
    for q, cnt in sorted(pend.items()):
        label = meta["checklist"].get(q, q)
        print("  %-11s %4d  %s" % (q, cnt, label))

    if args.report:
        print()
        print("per-category verified")
        cats = collections.defaultdict(lambda: [0, 0])
        for e in entries:
            cats[e["category"]][1] += 1
            if e["review"]["status"] == "verified":
                cats[e["category"]][0] += 1
        for c in sorted(cats):
            v, t = cats[c]
            print("  %s  %3d/%3d" % (c, v, t))
        print()
        print("evidence stars")
        st = collections.Counter(e["evidence_stars"] for e in entries)
        for k in (3, 2, 1, None):
            if st.get(k):
                print("  %-4s %3d" % (("★" * k) if k else "—", st[k]))

    print()
    if warnings:
        print("WARNINGS (%d)" % len(warnings))
        for w in warnings[:15]:
            print("  ! " + w)
        if len(warnings) > 15:
            print("  ... and %d more" % (len(warnings) - 15))
    if errors:
        print("ERRORS (%d)" % len(errors))
        for x in errors[:40]:
            print("  x " + x)
        if len(errors) > 40:
            print("  ... and %d more" % (len(errors) - 40))
        return 1
    print("OK — no errors")
    return 0


if __name__ == "__main__":
    sys.exit(main())
