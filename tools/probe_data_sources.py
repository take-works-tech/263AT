#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
データソースの実現可能性を実際に叩いて確認する。docs/03_data_feasibility.md の再現用。

「公式サイトにそう書いてあった」ではなく「実際に取れた」を記録するための道具。
仕様は変わるので、判断の前に必ず再実行すること。

使い方
    python tools/probe_data_sources.py            # 登録不要のものだけ
    python tools/probe_data_sources.py --oap      # Open Source Asset Pricing も（.venv が要る）
    python tools/probe_data_sources.py --keyed    # .env の API キーを使うものも
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
UA = {"User-Agent": "263AT research (contact: tzero30208@gmail.com)"}
PAUSE = 0.2  # SEC は 10 req/s 制限。余裕を持って


def req(url, method="GET", timeout=25):
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA, method=method), timeout=timeout)
    return r


def get_json(url, timeout=25):
    return json.loads(req(url, "GET", timeout).read())


def probe(label, url, method="HEAD", timeout=25):
    try:
        r = req(url, method, timeout)
        cl = r.headers.get("Content-Length")
        size = ("%.1fMB" % (int(cl) / 1e6)) if cl else "-"
        print("  OK  %-34s %s %8s" % (label, r.status, size))
        return True
    except urllib.error.HTTPError as e:
        print("  --  %-34s HTTP %s" % (label, e.code))
    except Exception as e:
        print("  NG  %-34s %s" % (label, str(e)[:50]))
    return False


# 中間表現（docs/02_definition_spec.md §2）に対応する us-gaap / dei タグ候補
ACCOUNT_TAGS = {
    "REV": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet"],
    "COGS": ["CostOfRevenue", "CostOfGoodsAndServicesSold"],
    "GP": ["GrossProfit"],
    "SGA": ["SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"],
    "OP": ["OperatingIncomeLoss"],
    "NI": ["NetIncomeLoss"],
    "DA": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet"],
    "CFO": ["NetCashProvidedByUsedInOperatingActivities"],
    "CAPEX": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "TA": ["Assets"],
    "EQ": ["StockholdersEquity"],
    "CASH": ["CashAndCashEquivalentsAtCarryingValue"],
    "IBD_lt": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "IBD_st": ["LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper"],
    "LEASE": ["OperatingLeaseLiabilityNoncurrent", "OperatingLeaseLiabilityCurrent", "FinanceLeaseLiabilityNoncurrent"],
    "SHARES": ["CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding",
               "WeightedAverageNumberOfSharesOutstandingBasic"],
    "TAX": ["IncomeTaxExpenseBenefit"],
    "PRETAX": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"],
    "GOODWILL": ["Goodwill"],
    "RD": ["ResearchAndDevelopmentExpense"],
    "INV": ["InventoryNet"],
    "AR": ["AccountsReceivableNetCurrent"],
    "AP": ["AccountsPayableCurrent"],
    "SBC": ["ShareBasedCompensation"],
    "DIV": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "BUYBACK": ["PaymentsForRepurchaseOfCommonStock"],
}


def sec_edgar():
    print("\n=== SEC EDGAR（登録不要）" + "=" * 40)

    print("\n[1] PIT 日付 filed の有無と、訂正後データの罠")
    cf = get_json("https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json", 40)
    us = cf["facts"]["us-gaap"]
    any_unit = next(iter(next(iter(us.values()))["units"].values()))
    print("  entity=%s  us-gaap tags=%d  keys=%s" % (cf["entityName"], len(us), sorted(any_unit[0].keys())))
    print("  同一(tag,期間)が複数回 filed されるか / 値が変わるか:")
    print("    %-44s %7s %9s %9s" % ("tag", "期間数", "複数filed", "値が違う"))
    worst = None
    for tag in ["NetIncomeLoss", "NetCashProvidedByUsedInOperatingActivities",
                "StockholdersEquity", "Assets"]:
        d = us.get(tag)
        if not d:
            continue
        arr = d["units"].get("USD")
        if not arr:
            continue
        fl, vl = {}, {}
        for f in arr:
            k = (f["end"], f.get("start"))
            fl.setdefault(k, set()).add(f["filed"])
            vl.setdefault(k, set()).add(f["val"])
        multi = {k: v for k, v in fl.items() if len(v) > 1}
        diff = {k: sorted(v) for k, v in vl.items() if len(v) > 1}
        print("    %-44s %7d %9d %9d" % (tag, len(fl), len(multi), len(diff)))
        for k, vals in diff.items():
            spread = (max(vals) - min(vals)) / max(abs(min(vals)), 1)
            if worst is None or spread > worst[2]:
                worst = (tag, k[0], spread, vals)
    if worst:
        print("    → 値の食い違いが最大: %s end=%s %s (%.1f%% の差)"
              % (worst[0], worst[1], worst[3], worst[2] * 100))
    print("    → 正: value_as_of(t) = filed <= t のうち filed が最大の val")
    print("    → 誤: groupby([tag,end]).last()  … 訂正後データを使うことになる")
    time.sleep(PAUSE)


    print("\n[2] 遡及可能年数")
    filed = sorted({f["filed"] for d in us.values() for a in d["units"].values() for f in a})
    print("  最古 filed=%s  最新 filed=%s  → 自前構築の PIT 履歴はここが下限" % (filed[0], filed[-1]))

    print("\n[3] 勘定コードの網羅性（大型株ベストケース）")
    dei = cf["facts"].get("dei", {})
    hit = sum(1 for tags in ACCOUNT_TAGS.values() if any(t in us or t in dei for t in tags))
    miss = [c for c, tags in ACCOUNT_TAGS.items() if not any(t in us or t in dei for t in tags)]
    print("  %d/%d 取得可能  欠落=%s" % (hit, len(ACCOUNT_TAGS), miss or "なし"))

    print("\n[4] 上場廃止企業のデータ残存（生存者バイアス対策の要）")
    for name, cik in [("Bed Bath & Beyond", 886158), ("Sears Holdings", 1310067),
                      ("SVB Financial", 719739), ("Lehman Brothers", 806085)]:
        c = "CIK%010d" % cik
        try:
            d = get_json("https://data.sec.gov/api/xbrl/companyfacts/%s.json" % c, 30)
            u = d["facts"].get("us-gaap", {})
            ends = sorted({x["end"] for v in u.values() for a in v["units"].values() for x in a if "end" in x})
            print("  OK  %-20s tags=%-4d %s..%s" % (name, len(u), ends[0], ends[-1]))
        except urllib.error.HTTPError as e:
            print("  --  %-20s HTTP %s（XBRL 前の廃止は存在しない）" % (name, e.code))
        except Exception as e:
            print("  NG  %-20s %s" % (name, str(e)[:40]))
        time.sleep(PAUSE)

    print("\n[5] 過去時点の銘柄を列挙する手段")
    tk = get_json("https://www.sec.gov/files/company_tickers.json", 30)
    ciks = {v["cik_str"] for v in tk.values()}
    print("  company_tickers.json: %d社  廃止企業の収録: BBBY=%s Sears=%s SVB=%s"
          % (len(tk), 886158 in ciks, 1310067 in ciks, 719739 in ciks))
    print("  → 現行ファイルだけでは生存者バイアス。以下を使う:")
    time.sleep(PAUSE)
    for lab, u in [("full-index 2010Q1", "https://www.sec.gov/Archives/edgar/full-index/2010/QTR1/company.idx"),
                   ("DERA 2010q1", "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2010q1.zip"),
                   ("DERA 2025q1", "https://www.sec.gov/files/dera/data/financial-statement-data-sets/2025q1.zip")]:
        probe(lab, u)
        time.sleep(PAUSE)


def free_research_data():
    print("\n=== 研究用データ（登録不要）" + "=" * 34)
    for lab, u in [("Ken French 5factors daily",
                    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/F-F_Research_Data_5_Factors_2x3_daily_CSV.zip"),
                   ("Ken French 25 Portfolios",
                    "https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/25_Portfolios_5x5_CSV.zip"),
                   ("openassetpricing (PyPI)", "https://pypi.org/simple/openassetpricing/")]:
        probe(lab, u, "GET" if "pypi" in u else "HEAD")
        time.sleep(PAUSE)


def keyed_sources():
    print("\n=== 要登録（キーがあれば実測）" + "=" * 33)
    print("\n[J-Quants]")
    if not probe("/v1/listed/info（無認証）", "https://api.jquants.com/v1/listed/info", "GET"):
        print("      → 403 が正常。登録して .env に JQUANTS_MAILADDRESS / JQUANTS_PASSWORD を置く")
    time.sleep(PAUSE)

    print("\n[EDINET v2]")
    key = os.environ.get("EDINET_API_KEY")
    u = "https://api.edinet-fsa.go.jp/api/v2/documents.json?date=2026-08-01&type=2"
    if key:
        u += "&Subscription-Key=" + key
    try:
        d = get_json(u, 25)
        n = d.get("results")
        print("  keys=%s  results=%s" % (list(d)[:4], len(n) if n is not None else "None"))
        if n is None:
            print("      → API キーなしでは results が返らない。無料キーの取得が必要")
    except Exception as e:
        print("  NG", str(e)[:70])


def oap():
    print("\n=== Open Source Asset Pricing" + "=" * 33)
    try:
        import openassetpricing as _oap
    except ImportError:
        print("  未導入。 .venv/Scripts/python.exe -m pip install openassetpricing")
        return
    op = _oap.OpenAP()
    doc = op.dl_signal_doc("pandas")
    pred = doc[doc["Cat.Signal"] == "Predictor"]
    print("  シグナル %d件（Predictor %d / Placebo %d）"
          % (len(doc), len(pred), (doc["Cat.Signal"] == "Placebo").sum()))
    print("  再現品質:", doc["Signal Rep Quality"].value_counts().to_dict())
    print("  Predictor |t| 中央値 %.2f  月次リターン中央値 %.2f%%"
          % (pred["T-Stat"].abs().median(), pred["Return"].median()))
    print("  提供ポートフォリオ（OQ-17 の検証に使う）:")
    op.list_port()
    out = ROOT / "research" / "oap_signal_doc.csv"
    doc.to_csv(out, index=False, encoding="utf-8")
    print("  saved:", out.relative_to(ROOT))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--oap", action="store_true", help="Open Source Asset Pricing も調べる")
    ap.add_argument("--keyed", action="store_true", help="要登録のソースも叩く")
    args = ap.parse_args()

    print("263AT データソース実測  %s" % datetime.date.today().isoformat())
    sec_edgar()
    free_research_data()
    if args.keyed:
        keyed_sources()
    if args.oap:
        oap()
    print("\n所見は docs/03_data_feasibility.md に記録すること。**仕様は変わるので、判断の前に再実行する。**")


if __name__ == "__main__":
    main()
