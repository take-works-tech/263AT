#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
docs/01_parameter_catalog.md から params/*.yaml（パラメータレジストリ）を生成する。

3層構造
-------
  1. カタログ表        … 機械的に導出できる項目（definition / sign / 買売 / horizon / 実証度 …）
  2. params/_defaults.yaml … プロジェクト全体の規約（連結/欠損方針/正規化/PITラグ …）
  3. params/_overrides.yaml … 人によるレビュー結果。**最優先。再生成しても失われない**

各項目の出所は entry.provenance に記録する（catalog / default / override）。
review.pending は「まだ人の判断が要る問い」を **provenance から自動計算**する。
手で pending を書き換えることはしない。

使い方
------
    python tools/build_registry.py            # 生成 / 更新
    python tools/build_registry.py --check    # 書き込まず差分検査（CI用）
"""
from __future__ import annotations

import argparse
import collections
import datetime as _dt
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
CATALOG = ROOT / "docs" / "01_parameter_catalog.md"
OUTDIR = ROOT / "params"
DEFAULTS_F = OUTDIR / "_defaults.yaml"
OVERRIDES_F = OUTDIR / "_overrides.yaml"
OVERRIDES_OAP_F = OUTDIR / "_overrides_oap.yaml"   # crosswalk_oap.py が生成（q11 のみ）

SCHEMA_VERSION = 2
GENERATED_ON = _dt.date(2026, 8, 23).isoformat()

SIGN = {"+": "positive", "-": "negative", "∩": "cap", "∪": "cup", "?": "context", "—": "none"}
CLASS = {"◎": "main", "○": "useful", "△": "aux", "✕": "none", "▣": "gate", "—": "na"}
MONTHS = {"1W": 0.25, "1M": 1, "3M": 3, "6M": 6, "1Y": 12, "2Y": 24, "3Y": 36, "5Y": 60, "5Y+": 60}
SOURCES = {"PX", "JQ", "EDI", "TD", "SEC", "FIN", "EST", "NEWS", "OPT", "MACRO", "CALC", "LLM", "SELF", "手"}
FINANCIAL_SOURCES = {"FIN", "EDI", "SEC"}
PRIOR_SCALE = {3: 1.00, 2: 0.45, 1: 0.15}

CHECKLIST = {
    "q01": "分子と分母はどの period_convention か",
    "q02": "available_at はいつか。disclosure から何日遅れるか",
    "q03": "連結か単体か",
    "q04": "会計基準が違う企業間で比較可能か。不可なら調整方法",
    "q05": "分母がゼロ・負の場合の扱い",
    "q06": "欠損の扱い。欠損は小型株・特定業種に偏るか",
    "q07": "正規化の母集団（全体 / セクター / サイズ分位 / 自己履歴）",
    "q08": "符号。∩ なら最適点 x* の決め方",
    "q09": "強く相関する既存パラメータと、それでも残す理由",
    "q10": "なぜ効くと考えるか（経済的な理由）",
    "q11": "実証度 T1〜T4 と一次文献 URL",
    "q12": "日米で定義が変わるか。別 ID にするか市場フラグか",
}

# 上書きで埋めることで「解決済み」になる項目
Q_FIELD = {
    "q01": "period_convention",
    "q03": "consolidation",
    "q04": "accounting_standard_note",
    "q05": "zero_denominator_policy",
    "q06": "missing_bias",
    "q07": "normalization",
    "q08": "nonlinear",
    "q09": "correlated_with",
    "q10": "economic_rationale",
    "q12": "markets",
}


def clean(s: str) -> str:
    s = s.strip()
    s = re.sub(r"~~(.+?)~~", r"\1", s)
    s = s.replace("**", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


def parse_horizon(raw):
    lab = clean(raw)
    if lab in ("全期間", "全期"):
        return {"label": "all", "min_m": 0, "max_m": None}, True
    if lab == "即時":
        return {"label": "immediate", "min_m": 0, "max_m": 0}, True
    if "-" in lab:
        a, b = [x.strip() for x in lab.split("-", 1)]
        if a in MONTHS and b in MONTHS:
            return {"label": lab, "min_m": MONTHS[a], "max_m": MONTHS[b]}, True
    if lab in MONTHS:
        return {"label": lab, "min_m": MONTHS[lab], "max_m": MONTHS[lab]}, True
    return {"label": lab, "min_m": None, "max_m": None}, False


def strip_count(heading):
    h = re.sub(r"^#+\s*", "", heading)
    h = re.sub(r"\s*\(\d+\)\s*$", "", h)
    return re.sub(r"^[A-Z](-\d)?\.\s*", "", h).strip()


def extract(md):
    lines = md.split("\n")
    rows, anomalies = [], []
    cur3 = cur4 = None
    i = 0
    while i < len(lines) - 1:
        ln = lines[i]
        if ln.startswith("### "):
            cur3, cur4 = ln, None
        elif ln.startswith("#### "):
            cur4 = ln
        if ln.startswith("|") and re.match(r"^\|[\s:|-]+\|$", lines[i + 1]):
            j = i + 2
            while j < len(lines) and lines[j].startswith("|"):
                m = re.match(r"^\|\s*([A-Z]\d{2})\s*\|", lines[j])
                if m:
                    cells = lines[j].split("|")[1:-1]
                    if len(cells) != 10:
                        anomalies.append("cols=%d %s" % (len(cells), m.group(1)))
                    else:
                        rows.append((m.group(1), [clean(c) for c in cells], cur3, cur4, j + 1))
                j += 1
            i = j
            continue
        i += 1
    return rows, anomalies


def build_entry(pid, cells, h3, h4, D):
    _, name, definition, sign_c, buy_c, sell_c, hor, src, ev, notes = cells
    cat = pid[0]
    unknown = []
    prov = {}

    def catalog(k):
        prov[k] = "catalog"

    def default(k):
        prov[k] = "default"

    sign = SIGN.get(sign_c)
    if sign is None:
        unknown.append("%s sign=%r" % (pid, sign_c)); sign = "unknown"
    buy = CLASS.get(buy_c)
    if buy is None:
        unknown.append("%s buy=%r" % (pid, buy_c)); buy = "unknown"
    sell = CLASS.get(sell_c)
    if sell is None:
        unknown.append("%s sell=%r" % (pid, sell_c)); sell = "unknown"
    for k in ("name_ja", "definition", "sign", "buy_class", "sell_class", "horizon",
              "data_sources", "evidence_stars", "notes"):
        catalog(k)

    horizon, ok = parse_horizon(hor)
    if not ok:
        unknown.append("%s horizon=%r" % (pid, hor))

    sources = [x.strip() for x in re.split(r"[,、/]", clean(src)) if x.strip()] if clean(src) not in ("—", "-", "") else []
    for s in sources:
        if s not in SOURCES:
            unknown.append("%s source=%r" % (pid, s))

    stars = len(clean(ev)) if clean(ev).startswith("★") else None
    prior = PRIOR_SCALE.get(stars) if stars else None
    prov["prior_scale"] = "default"

    sub = strip_count(h4) if h4 else None

    # --- 規約から埋まる項目 --------------------------------------------------
    is_fin = bool(set(sources) & FINANCIAL_SOURCES)
    consolidation = (D["consolidation"]["value_when_financial"] if is_fin
                     else D["consolidation"]["value_otherwise"])
    default("consolidation")

    missing_policy = D["missing_policy"]["default"]
    default("missing_policy")

    normalization = D["normalization_by_category"].get(cat)
    default("normalization")

    period_convention = D["period_convention_by_category"].get(cat)
    default("period_convention")

    lagmap = D["pit_lag_days_by_source"]
    pit_lag = max([lagmap.get(s, 0) for s in sources], default=0)
    default("pit_lag_days")

    if sign == "cap":
        nonlinear = D["nonlinear_default_for_cap"]; default("nonlinear")
    elif sign == "cup":
        nonlinear = D["nonlinear_default_for_cup"]; default("nonlinear")
    else:
        nonlinear = None

    acct_pending_cats = D["accounting_standard_pending_for"]["categories"]
    accounting_note = None if cat in acct_pending_cats else D["accounting_standard_pending_for"]["value_otherwise"]
    if accounting_note is not None:
        default("accounting_standard_note")

    is_ratio = "/" in definition or "÷" in definition
    zero_denom = None if is_ratio else D["zero_denominator"]["value_otherwise"]
    if zero_denom is not None:
        default("zero_denominator_policy")

    markets, markets_derived = ["JP", "US"], False
    if cat == "K" and sub:
        if "日本固有" in sub:
            markets, markets_derived = ["JP"], True
        elif "米国固有" in sub:
            markets, markets_derived = ["US"], True
    if markets_derived:
        prov["markets"] = "catalog"

    industries = [sub] if cat == "U" and sub else None

    e = collections.OrderedDict([
        ("id", pid), ("name_ja", name), ("category", cat),
        ("category_name", strip_count(h3) if h3 else None), ("subsection", sub),
        ("definition", definition), ("formula", None), ("inputs", None),
        ("sign", sign), ("buy_class", buy), ("sell_class", sell), ("gate_policy", None),
        ("horizon", horizon), ("data_sources", sources),
        ("evidence_stars", stars), ("prior_scale", prior), ("evidence_tier", None),
        ("markets", markets), ("industries", industries),
        ("period_convention", period_convention), ("consolidation", consolidation),
        ("accounting_standard_note", accounting_note), ("pit_lag_days", pit_lag),
        ("zero_denominator_policy", zero_denom), ("missing_policy", missing_policy),
        ("missing_bias", None), ("normalization", normalization), ("nonlinear", nonlinear),
        ("winsorize", None), ("correlated_with", None), ("economic_rationale", None),
        ("references", []), ("notes", notes), ("notes_extra", None),
        ("provenance", prov),
        ("review", collections.OrderedDict([("status", "draft"), ("pending", [])])),
        ("version", 1),
    ])
    return e, unknown


def apply_override(e, ov):
    for k, v in ov.items():
        if k == "review":
            e["review"]["status"] = v.get("status", e["review"]["status"])
            continue
        e[k] = v
        e["provenance"][k] = "override"
    return e


def compute_pending(e, D):
    cat = e["category"]
    p = e["provenance"]
    pend = []

    def open_q(q, cond=True):
        if cond and p.get(Q_FIELD[q]) != "override":
            pend.append(q)

    open_q("q01", cat in D["period_convention_still_pending_for"]["categories"])
    # q02 / q03 / q07 は規約で確定するので pending にしない
    open_q("q04", cat in D["accounting_standard_pending_for"]["categories"])
    open_q("q05", e["zero_denominator_policy"] is None)
    open_q("q06")                                  # 欠損の偏り（missing_bias）は個別判断
    open_q("q08", e["sign"] in ("cap", "cup"))
    open_q("q09")
    open_q("q10")
    if not (e["evidence_tier"] and e["references"]):
        pend.append("q11")
    open_q("q12", p.get("markets") != "catalog")
    if e["buy_class"] == "gate" or e["sell_class"] == "gate":
        if not e["gate_policy"]:
            pend.append("gate_policy")
    e["review"]["pending"] = sorted(set(pend))
    return e


def plain(o):
    if isinstance(o, dict):
        return {k: plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [plain(v) for v in o]
    return o


def dump_category(cat, entries, cat_name):
    header = (
        "# 263AT parameter registry — category %s (%s)\n"
        "#\n"
        "# GENERATED by tools/build_registry.py\n"
        "#   catalog : docs/01_parameter_catalog.md   （表から導出）\n"
        "#   default : params/_defaults.yaml          （プロジェクト規約）\n"
        "#   override: params/_overrides.yaml         （人のレビュー結果。最優先）\n"
        "# 各項目の出所は provenance を見ること。**このファイルを直接編集しない。**\n"
        "# 修正はカタログ md か _defaults.yaml か _overrides.yaml に対して行う。\n"
        % (cat, cat_name))
    return header + yaml.safe_dump([plain(e) for e in entries], allow_unicode=True,
                                   sort_keys=False, default_flow_style=False, width=10**6)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    D = yaml.safe_load(DEFAULTS_F.read_text(encoding="utf-8"))
    OV_OAP = (yaml.safe_load(OVERRIDES_OAP_F.read_text(encoding="utf-8"))
              if OVERRIDES_OAP_F.exists() else {}) or {}
    OV = yaml.safe_load(OVERRIDES_F.read_text(encoding="utf-8")) or {}
    rows, anomalies = extract(CATALOG.read_text(encoding="utf-8"))

    by_cat, cat_names, unknown_all = collections.OrderedDict(), {}, []
    seen_ids = set()
    for pid, cells, h3, h4, _ln in rows:
        e, unk = build_entry(pid, cells, h3, h4, D)
        unknown_all += unk
        if pid in OV_OAP:                    # 機械生成（q11）
            e = apply_override(e, OV_OAP[pid])
        if pid in OV:                        # 人のレビューが最優先
            e = apply_override(e, OV[pid])
        compute_pending(e, D)
        by_cat.setdefault(pid[0], []).append(e)
        cat_names[pid[0]] = e["category_name"]
        seen_ids.add(pid)

    stray = sorted((set(OV) | set(OV_OAP)) - seen_ids)
    if stray:
        print("OVERRIDES FOR UNKNOWN IDS:", stray)
    if anomalies:
        print("COLUMN ANOMALIES:", anomalies)
    if unknown_all:
        print("UNKNOWN TOKENS (%d):" % len(unknown_all), unknown_all[:20])

    OUTDIR.mkdir(exist_ok=True)
    changed = []
    for cat, entries in by_cat.items():
        entries.sort(key=lambda x: x["id"])
        text = dump_category(cat, entries, cat_names[cat])
        f = OUTDIR / ("%s.yaml" % cat)
        if (f.read_text(encoding="utf-8") if f.exists() else None) != text:
            changed.append(f.name)
            if not args.check:
                f.write_text(text, encoding="utf-8")

    verified = sum(1 for v in by_cat.values() for e in v if e["review"]["status"] == "verified")
    pend_hist = collections.Counter()
    for v in by_cat.values():
        for e in v:
            for q in e["review"]["pending"]:
                pend_hist[q] += 1

    meta = collections.OrderedDict([
        ("schema_version", SCHEMA_VERSION),
        ("generated_from", ["docs/01_parameter_catalog.md", "params/_defaults.yaml",
                            "params/_overrides_oap.yaml", "params/_overrides.yaml"]),
        ("generated_by", "tools/build_registry.py"),
        ("generated_on", GENERATED_ON),
        ("total_parameters", len(rows)),
        ("verified", verified),
        ("draft", len(rows) - verified),
        ("open_questions_by_id", dict(pend_hist)),
        ("categories", [collections.OrderedDict([("letter", c), ("name_ja", cat_names[c]), ("count", len(v))])
                        for c, v in by_cat.items()]),
        ("enums", collections.OrderedDict([
            ("sign", sorted(set(SIGN.values()))),
            ("weight_class", sorted(set(CLASS.values()))),
            ("data_sources", sorted(SOURCES)),
            ("review_status", ["draft", "reviewed", "verified"]),
            ("evidence_tier", ["T1", "T2", "T3", "T4"]),
            ("period_convention", ["TTM", "FY", "FQ", "AVG", "GUIDE_CO", "GUIDE_CONS", "POINT"]),
            ("normalization", ["rank_all", "rank_sector", "rank_market", "rank_industry", "self_history", "none"]),
            ("missing_policy", ["flag", "zero", "drop"]),
            ("consolidation", ["consolidated", "parent_only", "na"]),
            ("provenance", ["catalog", "default", "override"]),
        ])),
        ("prior_scale_by_stars", {str(k): v for k, v in PRIOR_SCALE.items()}),
        ("checklist", CHECKLIST),
        ("question_to_field", Q_FIELD),
    ])
    mtext = ("# 263AT parameter registry — metadata\n# GENERATED by tools/build_registry.py\n"
             + yaml.safe_dump(plain(meta), allow_unicode=True, sort_keys=False,
                              default_flow_style=False, width=10**6))
    f = OUTDIR / "_meta.yaml"
    if (f.read_text(encoding="utf-8") if f.exists() else None) != mtext:
        changed.append(f.name)
        if not args.check:
            f.write_text(mtext, encoding="utf-8")

    print("parameters: %d  categories: %d  verified: %d  draft: %d"
          % (len(rows), len(by_cat), verified, len(rows) - verified))
    print("open questions:", dict(sorted(pend_hist.items())))
    if args.check:
        if changed:
            print("DRIFT ->", ", ".join(sorted(changed)))
            return 1
        print("in sync")
        return 0
    print("written:", ", ".join(sorted(changed)) if changed else "(no change)")
    return 1 if (anomalies or unknown_all or stray) else 0


if __name__ == "__main__":
    sys.exit(main())
