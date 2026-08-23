#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
§1.9 の priority_k を実際に計算し、データパイプラインの構築順序を出す。

なぜこれが要るか
----------------
770 件すべてを検証し、150 件に再現 t を付け、49 件に塊での生存を測った。
**それでも「明日どのデータから作るか」は決まっていない。**

§1.9 の方針は「選択せず全部入れて縮める」なので、
**パラメータを選ぶのではなく、データパイプラインを作る順序を決める。**
モデルには、その時点で取得できている全部を入れる。

    priority_k = 再現の強さ × 入手容易性 × 塊での生存 / 取得コスト

**この式は「自分の成績」を一切見ていない。**
成績を見て順序を決めた瞬間に、それは選択になる（§1.9）。

出力は**パラメータ単位ではなくデータ源単位**である。
1つのデータ源を引くと複数のパラメータが同時に作れるので、
**「そのデータ源を引くと何点ぶんの価値が手に入るか」**で並べる。

使い方
------
    python tools/build_priority.py
    python tools/build_priority.py --market JP
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import sys

import yaml

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "research" / "implementation_priority.csv"
SURVIVAL = ROOT / "research" / "z12_survival.csv"

# 入手容易性。docs/03_data_feasibility.md の実測に基づく。
# **「無料か」ではなく「263AT が今日引けるか」で付ける。**
EASE = {
    "PX": 1.0,     # 価格。yfinance / stooq で取れることを確認済み
    "JQ": 0.9,     # J-Quants 無料枠。**要登録（未完了）**
    "TD": 0.8,     # 適時開示。スクレイピングが要る
    "SEC": 1.0,    # EDGAR。DERA データセットで PIT が取れることを確認済み
    "EDI": 0.8,    # EDINET。**API キーが要る（未取得）**
    "FIN": 0.9,    # 財務。SEC/EDINET から導出
    "MACRO": 1.0,  # FRED 等。公開
    "CALC": 1.0,   # 派生値。入力側の容易性が効くので別途伝播させる
    "SELF": 1.0,   # 自分の口座状態
    "EST": 0.3,    # アナリスト予想。**無料では取れない**
    "OPT": 0.4,    # オプション。米国の一部のみ
    "NEWS": 0.5,   # ニュース。無料 API は履歴が短い
    "LLM": 0.6,    # 生成できるが**過去に遡れない**（§6.5）
    "手": 0.3,     # 手動・スクレイピング
}

# 取得コスト（実装工数の相対値）。低いほど良い。
COST = {
    "PX": 1.0, "SEC": 1.5, "FIN": 2.0, "MACRO": 1.0, "CALC": 1.2, "SELF": 1.0,
    "JQ": 1.5, "EDI": 2.5, "TD": 2.5, "EST": 3.0, "OPT": 2.5,
    "NEWS": 2.0, "LLM": 2.5, "手": 3.0,
}

STAR_TO_T = {3: 4.0, 2: 2.5, 1: 1.5, None: 1.0}

# CALC（派生値）は data_sources に "CALC" としか書かれていないが、
# **実際の難易度は入力側で決まる。** 例: E29（税金費用の変化）は CALC だが
# 入力は法人税等・税引前利益なので、財務データ（FIN）が無ければ作れない。
# → **入力名から本当のデータ源を推定して伝播させる。**
#   これをやらないと Phase 0（価格だけ）が実際より遥かに多く見える。
INPUT_TO_SOURCE = {
    # 価格・出来高だけで作れるもの
    "PRICE": "PX", "PRICE_adj": "PX", "VOLUME": "PX", "index": "PX",
    "listing_date": "PX", "factor_returns": "PX", "sector_index": "PX",
    # 財務諸表が要るもの（spec §2 の正規化勘定科目）
    "REV": "FIN", "COGS": "FIN", "GP": "FIN", "SGA": "FIN", "OP": "FIN",
    "EBIT": "FIN", "DA": "FIN", "EBITDA": "FIN", "NI": "FIN", "CFO": "FIN",
    "CAPEX": "FIN", "FCF": "FIN", "TA": "FIN", "EQ": "FIN", "IBD": "FIN",
    "CASH": "FIN", "ND": "FIN", "IC": "FIN", "NOPAT": "FIN", "SHARES": "FIN",
    "RD": "FIN", "dividends": "FIN", "receivables": "FIN", "inventory": "FIN",
    "payables": "FIN", "PPE": "FIN", "employees": "FIN", "advertising": "FIN",
    "segment_revenue": "FIN", "segment_data": "FIN", "goodwill": "FIN",
    "tax_expense": "FIN", "pretax_income": "FIN", "working_capital": "FIN",
    "current_assets": "FIN", "current_liabilities": "FIN",
    "total_liabilities": "FIN", "short_term_debt": "FIN",
    "accumulated_depreciation": "FIN", "deferred_tax_assets": "FIN",
    "financial_assets": "FIN", "cf_financing": "FIN", "cf_statement": "FIN",
    "comprehensive_income": "FIN", "MCAP": "FIN",
    # マクロ
    "MACRO_10y": "MACRO", "MACRO_2y": "MACRO", "MACRO_fx": "MACRO",
    "MACRO_cpi": "MACRO", "MACRO_policy": "MACRO", "MACRO_vix": "MACRO",
    "MACRO_credit": "MACRO", "MACRO_gdp": "MACRO", "MACRO_leading": "MACRO",
    "commodity_index": "MACRO", "VIX": "MACRO", "WACC": "MACRO",
    "discount_rate": "MACRO", "statutory_rate": "MACRO",
    # 自分の口座
    "SELF_positions": "SELF", "SELF": "SELF",
    # LLM
    "LLM": "LLM",
    # 上の表に無い入力名は保守的に「手」扱いになる。
    # **その保守性が優先順位を歪めていたので、実体が明らかなものを追記する。**
    # （2026-08-23、build_priority.py のボトルネック分析を1周させて判明した）
    "market_return": "PX", "market_cap": "FIN", "liquidity": "PX",
    "EV": "FIN", "TL": "FIN", "TAXR": "FIN", "INTEREST": "FIN",
    "EPS_actual": "FIN", "segment_profit": "FIN", "segment_assets": "FIN",
    "buyback_execution": "TD", "insider_transactions": "SEC",
    "earnings_date": "TD", "earnings_datetime": "TD", "GUIDE_CO": "TD",
    "margin_long": "JQ", "margin_short": "JQ", "float_shares": "JQ",
    "option_iv": "OPT", "option_volume": "OPT", "option_oi": "OPT",
    "appraisal_value": "EDI", "customer_disclosure": "EDI",
    "index_criteria": "手", "index_valuation": "手", "replacement_cost": "手",
    # 推定量・自分の状態・設計上のパラメータは追加のデータ源を要さない
    "mu_hat": "CALC", "growth_rate": "CALC", "growth_assumptions": "CALC",
    "h_star": "CALC", "available_at": "CALC", "CAC_estimate": "LLM",
    "target_position": "SELF", "target_position_jpy": "SELF",
    "SELF_entry_date": "SELF",
    # 業種分類。**spec §4.1（2026-08-23）で確定した。**
    #   日本 = 東証33業種（JPX の銘柄一覧。J-Quants でも取れる）
    #   米国 = Fama-French 49業種（SEC filing の SIC + Ken French の対応表）
    # → 以前は SP-03/SP-04 が未解決だったので "手" にしており、
    #   **それが最大のボトルネックとして検出された**（§1.9.9）。
    "sector_classification": "JQ", "industry": "JQ",
}


def resolve_sources(e, by_id):
    """CALC パラメータの実効的なデータ源を、入力を辿って解決する。

    入力が他のパラメータ ID（A03, B01, J05 …）ならその源を再帰的に取る。
    **循環は 1 段で打ち切る**（相互参照するパラメータが少数ある）。
    """
    srcs = set(e.get("data_sources") or [])
    if "CALC" not in srcs:
        return srcs
    for inp in (e.get("inputs") or []):
        if inp in INPUT_TO_SOURCE:
            srcs.add(INPUT_TO_SOURCE[inp])
        elif inp in by_id and inp != e["id"]:
            child = by_id[inp]
            srcs |= {x for x in (child.get("data_sources") or []) if x != "CALC"}
            for ci in (child.get("inputs") or []):
                if ci in INPUT_TO_SOURCE:
                    srcs.add(INPUT_TO_SOURCE[ci])
        elif inp in ("all_parameters", "S_buy", "S_sell"):
            pass                      # ポートフォリオ文脈。追加の源は要らない
        else:
            srcs.add("手")            # 未知の入力は手作業扱い（保守的に）
    return srcs


def load_registry():
    rows = []
    for f in sorted((ROOT / "params").glob("[A-Z].yaml")):
        rows += yaml.safe_load(f.read_text(encoding="utf-8")) or []
    return rows


def load_survival():
    """§1.9.7 追試(3) の結果。塊の他メンバーを控除した後の alpha_t。"""
    if not SURVIVAL.exists():
        return {}
    out = {}
    for ln in SURVIVAL.read_text(encoding="utf-8").splitlines()[1:]:
        c = ln.split(",")
        if len(c) < 6:
            continue
        try:
            out[c[1]] = abs(float(c[4]))
        except ValueError:
            pass
    return out


def strength(e) -> tuple[float, str]:
    """再現の強さ。**再現 t があればそれを、無ければ★にフォールバックする。**

    再現 t を持たないことは弱さの証拠ではない（§1.9.8）。
    日本固有・LLM 派生・保有状態には OSAP に対応が存在しないだけで、
    **263AT の優位性はむしろそちら側にある。**
    """
    r = e.get("replication") or {}
    t = r.get("t_replicated")
    if t is not None:
        return abs(float(t)), "再現"
    return STAR_TO_T.get(e.get("evidence_stars"), 1.0), "★"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", choices=["JP", "US"], default=None)
    args = ap.parse_args()

    params = load_registry()
    surv = load_survival()

    if args.market:
        params = [p for p in params if args.market in (p.get("markets") or [])]

    by_id = {e["id"]: e for e in load_registry()}
    rows = []
    for e in params:
        if not (e.get("data_sources") or []):
            continue
        srcs = sorted(resolve_sources(e, by_id))
        ease = min([EASE.get(s, 0.3) for s in srcs])      # 最も取りにくい源に律速される
        cost = max([COST.get(s, 3.0) for s in srcs])
        st, how = strength(e)
        # 塊での生存。測っていないものは中立の 1.0 とする（罰しない）
        sv = surv.get(e["id"])
        surv_mult = min(max(sv / 2.0, 0.3), 1.5) if sv is not None else 1.0
        # ゲートは断面リターンで測るものではないので、再現 t で罰しない（§1.9.8）
        is_gate = e.get("buy_class") == "gate" or e.get("sell_class") == "gate"
        if is_gate:
            st = max(st, 3.0)
        prio = st * ease * surv_mult / cost
        rows.append({
            "id": e["id"], "name": e["name_ja"], "cat": e["category"],
            "sources": "/".join(srcs), "strength": round(st, 2), "how": how,
            "ease": ease, "surv": round(surv_mult, 2), "cost": cost,
            "gate": is_gate, "buy": e.get("buy_class"),
            "priority": round(prio, 3),
        })

    rows.sort(key=lambda r: -r["priority"])

    # --- データ源ごとに集計する ------------------------------------------------
    by_src = collections.defaultdict(lambda: {"n": 0, "sum": 0.0, "gates": 0, "top": []})
    for r in rows:
        for s in r["sources"].split("/"):
            b = by_src[s]
            b["n"] += 1
            b["sum"] += r["priority"]
            b["gates"] += 1 if r["gate"] else 0
            if len(b["top"]) < 4:
                b["top"].append(r["id"])

    print("=" * 74)
    print("データパイプラインの構築順序（§1.9 priority_k）%s"
          % ("— %s のみ" % args.market if args.market else ""))
    print("=" * 74)
    print("priority = 再現の強さ × 入手容易性 × 塊での生存 / 取得コスト")
    print("**成績は一切見ていない。見た瞬間に選択になる（§1.9）。**")
    print()
    print("%-6s %5s %8s %6s  %s" % ("データ源", "本数", "価値合計", "ゲート", "代表"))
    print("-" * 74)
    for s, b in sorted(by_src.items(), key=lambda kv: -kv[1]["sum"]):
        print("%-6s %5d %8.1f %6d  %s"
              % (s, b["n"], b["sum"], b["gates"], ",".join(b["top"])))

    print()
    print("-" * 74)
    print("個別パラメータ 上位30")
    print("-" * 74)
    print("%-5s %-22s %-10s %6s %-4s %5s %s"
          % ("ID", "名前", "データ源", "強さ", "根拠", "優先度", ""))
    for r in rows[:30]:
        print("%-5s %-22s %-10s %6.2f %-4s %5.2f %s"
              % (r["id"], r["name"][:22], r["sources"][:10], r["strength"],
                 r["how"], r["priority"], "▣ゲート" if r["gate"] else ""))

    # --- 段階分け --------------------------------------------------------------
    print()
    print("=" * 74)
    print("段階分け — 何をどの順に用意するか")
    print("=" * 74)
    phases = [
        ("Phase 0", ["PX", "CALC", "SELF"],
         "**今日から作れる。** 価格だけで G/H/I/J の大半とサイジングが動く"),
        ("Phase 1", ["SEC", "FIN", "MACRO"],
         "**米国の財務。DERA で PIT が取れることは実測済み**（docs/03 §2.1）"),
        ("Phase 2", ["JQ", "EDI", "TD"],
         "**日本。J-Quants 登録と EDINET API キーが要る（未完了）**"),
        ("Phase 3", ["NEWS", "LLM"],
         "**過去に遡れない。forward_log で今日から記録する**（§6.5）"),
        ("Phase 4", ["EST", "OPT", "手"],
         "**有料またはスクレイピング。中核ユニバースでは欠損が多い**（OQ-33）"),
    ]
    done = set()
    for label, srcs, note in phases:
        avail = _cum(phases, label)
        got = [r for r in rows if r["id"] not in done
               and set(r["sources"].split("/")) <= avail]
        done |= {r["id"] for r in got}
        val = sum(r["priority"] for r in got)
        gates = sum(1 for r in got if r["gate"])
        print()
        print("%s  %s" % (label, "/".join(srcs)))
        print("  %s" % note)
        print("  → 新たに作れるパラメータ %d 件（累計 %d）、価値 %.1f、ゲート %d 件"
              % (len(got), len(done), val, gates))
        if got:
            print("  代表: %s" % ", ".join("%s(%s)" % (r["id"], r["name"][:10]) for r in got[:6]))

    # --- ボトルネック分析 ------------------------------------------------------
    # **1つのデータ源が欠けているせいで作れないパラメータ**を、源ごとに集計する。
    # これが「次に何を1つ解決すべきか」の答えになる。
    print()
    print("=" * 74)
    print("ボトルネック — その源が1つ欠けているだけで作れないパラメータ")
    print("=" * 74)
    print("**「その源だけを解決すると、いくつのパラメータがどれだけの価値で解放されるか」**")
    print()
    base = {"PX", "CALC", "SELF", "SEC", "FIN", "MACRO"}   # Phase 0-1 で揃う分
    block = collections.defaultdict(lambda: {"n": 0, "sum": 0.0, "top": []})
    for r in rows:
        need = set(r["sources"].split("/")) - base
        if len(need) == 1:                      # ちょうど1つだけ足りない
            s1 = need.pop()
            b = block[s1]
            b["n"] += 1
            b["sum"] += r["priority"]
            if len(b["top"]) < 5:
                b["top"].append("%s(%.1f)" % (r["id"], r["priority"]))
    for s1, b in sorted(block.items(), key=lambda kv: -kv[1]["sum"]):
        print("  %-6s %3d 件  価値 %6.1f   %s" % (s1, b["n"], b["sum"], ", ".join(b["top"])))
    print()
    print("→ **業種分類（手）が最大のボトルネック**なら、それは spec SP-03/SP-04")
    print("  （GICS を日本株にどう付与するか、業種の粒度をどうするか）が未解決だからで、")
    print("  **データを買う話ではなく設計を決める話。今日決められる。**")

    rest = [r for r in rows if r["id"] not in done]
    if rest:
        print()
        print("どの段階にも入らない %d 件（複数段階のデータ源を跨ぐもの）" % len(rest))

    import csv
    with OUT.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print()
    print("→ %s" % OUT.relative_to(ROOT))
    return 0


def _cum(phases, upto):
    """その段階までに揃っているデータ源の集合。"""
    s = set()
    for label, srcs, _ in phases:
        s |= set(srcs)
        if label == upto:
            break
    return s


if __name__ == "__main__":
    raise SystemExit(main())
