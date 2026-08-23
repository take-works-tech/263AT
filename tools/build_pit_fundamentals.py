#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SEC DERA Financial Statement Data Sets から、PIT（point-in-time）な米国財務データベースを作る。

なぜ companyfacts ではなく DERA なのか
--------------------------------------
companyfacts は「今から見た全履歴」なので、同じ (tag, 期間) に複数の filed がぶら下がり、
値も変わる（AAPL 2009年6月期の純利益は 1,229 と 1,828 の2値。差 48.7%）。
素直に読むとルックアヘッドが入る。

DERA は**四半期ごとに、その四半期に提出されたものだけ**を配布する。
`sub.txt` に提出日 (filed) が入っているので、**PIT がデータ構造として保証される。**
生存者バイアスも自動的に回避される（当時提出した企業がそのまま入っている）。

出力
----
  data/raw/dera/<q>.zip                取得した生 ZIP（gitignore）
  data/pit/facts/<q>.parquet           抽出済みファクト（long 形式）
  data/pit/subs/<q>.parquet            提出メタ（cik, sic, form, filed, period …）

使い方
------
  .venv/Scripts/python.exe tools/build_pit_fundamentals.py --list
  .venv/Scripts/python.exe tools/build_pit_fundamentals.py --quarters 2024q1 2024q2
  .venv/Scripts/python.exe tools/build_pit_fundamentals.py --all        # 2009q2 〜 現在
  .venv/Scripts/python.exe tools/build_pit_fundamentals.py --demo       # PIT の正誤を実演
"""
from __future__ import annotations

import argparse
import datetime
import io
import pathlib
import sys
import time
import urllib.request
import zipfile

import pandas as pd

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw" / "dera"
OUT_FACTS = ROOT / "data" / "pit" / "facts"
OUT_SUBS = ROOT / "data" / "pit" / "subs"
BASE = "https://www.sec.gov/files/dera/data/financial-statement-data-sets/%s.zip"
UA = {"User-Agent": "263AT research (contact: tzero30208@gmail.com)"}

# docs/02_definition_spec.md §2 の中間表現 → us-gaap タグ（優先順）
TAG_MAP = {
    "REV": ["RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "Revenues", "SalesRevenueNet",
            "SalesRevenueGoodsNet", "SalesRevenueServicesNet"],
    "COGS": ["CostOfGoodsAndServicesSold", "CostOfRevenue", "CostOfGoodsSold", "CostOfServices"],
    "GP": ["GrossProfit"],
    "SGA": ["SellingGeneralAndAdministrativeExpense", "GeneralAndAdministrativeExpense"],
    "OP": ["OperatingIncomeLoss"],
    "NI": ["NetIncomeLoss", "ProfitLoss"],
    "DA": ["DepreciationDepletionAndAmortization", "DepreciationAmortizationAndAccretionNet",
           "DepreciationAndAmortization"],
    "CFO": ["NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "CAPEX": ["PaymentsToAcquirePropertyPlantAndEquipment",
              "PaymentsToAcquireProductiveAssets"],
    "TA": ["Assets"],
    "TL": ["Liabilities"],
    "EQ": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "CASH": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"],
    "DEBT_LT": ["LongTermDebtNoncurrent", "LongTermDebt"],
    "DEBT_ST": ["LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper",
                "OtherShortTermBorrowings"],
    "LEASE_OP": ["OperatingLeaseLiabilityNoncurrent", "OperatingLeaseLiabilityCurrent"],
    "LEASE_FIN": ["FinanceLeaseLiabilityNoncurrent", "FinanceLeaseLiabilityCurrent"],
    "SHARES": ["CommonStockSharesOutstanding", "CommonStockSharesIssued",
               "WeightedAverageNumberOfSharesOutstandingBasic",
               "WeightedAverageNumberOfDilutedSharesOutstanding"],
    "TAX": ["IncomeTaxExpenseBenefit"],
    "PRETAX": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "GOODWILL": ["Goodwill"],
    "INTANGIBLE": ["FiniteLivedIntangibleAssetsNet", "IntangibleAssetsNetExcludingGoodwill"],
    "RD": ["ResearchAndDevelopmentExpense"],
    "INV": ["InventoryNet"],
    "AR": ["AccountsReceivableNetCurrent"],
    "AP": ["AccountsPayableCurrent"],
    "SBC": ["ShareBasedCompensation"],
    "DIV_PAID": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    "BUYBACK": ["PaymentsForRepurchaseOfCommonStock"],
    "ISSUANCE": ["ProceedsFromIssuanceOfCommonStock"],
    "INTEREST": ["InterestExpense", "InterestExpenseDebt"],
    "EMPLOYEES": [],           # dei にしかないので DERA では取れない
}
TAG2CODE = {t: c for c, ts in TAG_MAP.items() for t in ts}
# TAG_MAP の並び順 = 優先順位。resolve_codes() で使う
TAG_RANK = {t: i for ts in TAG_MAP.values() for i, t in enumerate(ts)}


def all_quarters(start=(2009, 2)):
    today = datetime.date.today()
    y, q = start
    out = []
    while (y, q) <= (today.year, (today.month - 1) // 3 + 1):
        out.append("%dq%d" % (y, q))
        q += 1
        if q > 4:
            y, q = y + 1, 1
    return out


def download(q):
    RAW.mkdir(parents=True, exist_ok=True)
    f = RAW / ("%s.zip" % q)
    if f.exists() and f.stat().st_size > 1000:
        return f, False
    req = urllib.request.Request(BASE % q, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            f.write_bytes(r.read())
        return f, True
    except Exception as e:
        print("    NG %s: %s" % (q, str(e)[:70]))
        return None, False


def parse(q, zpath):
    """sub.txt と num.txt を読み、必要なタグだけ抽出する。"""
    with zipfile.ZipFile(zpath) as z:
        names = set(z.namelist())
        if "sub.txt" not in names or "num.txt" not in names:
            return None, None
        sub = pd.read_csv(io.BytesIO(z.read("sub.txt")), sep="\t", dtype=str,
                          usecols=lambda c: c in {"adsh", "cik", "name", "sic", "countryba", "fye",
                                                  "form", "period", "filed", "accepted", "prevrpt",
                                                  "detail", "nciks"})
        num = pd.read_csv(io.BytesIO(z.read("num.txt")), sep="\t", dtype=str,
                          usecols=lambda c: c in {"adsh", "tag", "version", "coreg", "ddate",
                                                  "qtrs", "uom", "value", "segments"})

    sub["cik"] = pd.to_numeric(sub["cik"], errors="coerce").astype("Int64")
    sub["filed"] = pd.to_datetime(sub["filed"], format="%Y%m%d", errors="coerce")
    sub["period"] = pd.to_datetime(sub["period"], format="%Y%m%d", errors="coerce")
    sub["prevrpt"] = pd.to_numeric(sub["prevrpt"], errors="coerce").fillna(0).astype("int8")

    # us-gaap の標準タグのみ。企業独自の拡張タグは version が企業名になるので落ちる
    num = num[num["tag"].isin(TAG2CODE)]
    num = num[num["version"].astype(str).str.startswith("us-gaap")]
    # セグメント別・子会社別の内訳行を除き、連結の合計行だけ残す
    if "segments" in num.columns:
        num = num[num["segments"].isna() | (num["segments"].astype(str).str.strip() == "")]
    num = num[num["coreg"].isna() | (num["coreg"].astype(str).str.strip() == "")]
    num["code"] = num["tag"].map(TAG2CODE)
    num["tag_rank"] = num["tag"].map(TAG_RANK)
    num["ddate"] = pd.to_datetime(num["ddate"], format="%Y%m%d", errors="coerce")
    num["qtrs"] = pd.to_numeric(num["qtrs"], errors="coerce").astype("Int16")
    num["value"] = pd.to_numeric(num["value"], errors="coerce")
    num = num.dropna(subset=["ddate", "value"])
    # 完全重複（同一提出・同一タグ・同一期間）を落とす。DERA には実際に存在する
    num = num.drop_duplicates(subset=["adsh", "tag", "ddate", "qtrs", "uom"])

    keep = sub[["adsh", "cik", "sic", "form", "period", "filed", "prevrpt", "name", "fye"]]
    facts = num.merge(keep, on="adsh", how="inner")
    facts = facts[["cik", "adsh", "code", "tag", "tag_rank", "ddate", "qtrs", "uom", "value",
                   "filed", "form", "prevrpt", "sic"]]
    return sub, facts


def resolve_codes(facts):
    """
    タグ別名を勘定コードに解決する。

    ここを雑にやると壊れる。実測では **(企業, コード, 期間, 提出) の 15.1% に複数タグがぶら下がる。**
    例: PaymentsOfDividends = 21.49億ドル と PaymentsOfDividendsCommonStock = 0 が同じ提出に同居する
    （普通株配当はゼロで、優先株を含む総額が 21.49億という意味）。
    単純に集約すると 0 と 21.49億のどちらかが無作為に採用され、数字が壊れる。

    規約: TAG_MAP の**並び順が優先順位**。各 (cik, code, ddate, qtrs, adsh) につき
    最も優先順位が高いタグの値だけを採る。値の大小や欠損では選ばない（恣意性を入れない）。
    """
    f = facts.sort_values("tag_rank")
    key = ["cik", "code", "ddate", "qtrs", "adsh"]
    out = f.drop_duplicates(subset=key, keep="first")
    return out[["cik", "adsh", "code", "tag", "ddate", "qtrs", "uom", "value",
                "filed", "form", "prevrpt", "sic"]]


def build(quarters):
    OUT_FACTS.mkdir(parents=True, exist_ok=True)
    OUT_SUBS.mkdir(parents=True, exist_ok=True)
    tot_f = tot_s = 0
    for q in quarters:
        fp, sp = OUT_FACTS / ("%s.parquet" % q), OUT_SUBS / ("%s.parquet" % q)
        if fp.exists() and sp.exists():
            print("  cached %s" % q)
            tot_f += len(pd.read_parquet(fp, columns=["cik"]))
            continue
        z, fresh = download(q)
        if z is None:
            continue
        if fresh:
            time.sleep(0.3)
        sub, facts = parse(q, z)
        if facts is None:
            print("  skip   %s（構造が違う）" % q)
            continue
        facts.to_parquet(fp, index=False)
        sub.to_parquet(sp, index=False)
        tot_f += len(facts)
        tot_s += len(sub)
        print("  built  %-8s 提出 %6d件  ファクト %8d件  企業 %5d社  %s〜%s"
              % (q, len(sub), len(facts), facts["cik"].nunique(),
                 sub["filed"].min().date(), sub["filed"].max().date()))
    print("\n  合計ファクト %d 件" % tot_f)


def demo():
    """PIT の正誤と、タグ別名の罠を実演する。"""
    files = sorted(OUT_FACTS.glob("*.parquet"))
    if not files:
        print("  先に --quarters か --all でデータを作ること")
        return
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    print("### 読み込み: %d 四半期 / %d ファクト / %d 社"
          % (len(files), len(df), df["cik"].nunique()))
    print("  filed の範囲: %s 〜 %s" % (df["filed"].min().date(), df["filed"].max().date()))

    print("\n### 罠1: タグ別名が同一コードに潰れる")
    n_multi = (df.groupby(["cik", "code", "ddate", "qtrs", "adsh"])["tag"].nunique() > 1).mean()
    print("  同一(企業,コード,期間,提出)に複数タグ: %.1f%%" % (100 * n_multi))
    print("  → 単純集約すると値が壊れる。resolve_codes() で TAG_MAP の優先順位に従って1つ選ぶ")
    res = resolve_codes(df)
    print("  解決後: %d → %d 行" % (len(df), len(res)))

    print("\n### 罠2: 訂正（同一 企業/タグ/期間 が再提出され値が変わる）")
    for label, d, key in [("コード単位（誤り。別名混入）", df, ["cik", "code", "ddate", "qtrs"]),
                          ("タグ単位（正しい）", df, ["cik", "tag", "ddate", "qtrs"])]:
        g = d.groupby(key).agg(nf=("filed", "nunique"), nv=("value", "nunique"))
        m = g[(g["nf"] > 1) & (g["nv"] > 1)]
        print("  %-28s 値が変わった組み合わせ %6d / %7d (%.2f%%)"
              % (label, len(m), len(g), 100 * len(m) / max(len(g), 1)))

    key = ["cik", "tag", "ddate", "qtrs"]
    g = df.groupby(key).agg(nf=("filed", "nunique"), nv=("value", "nunique"),
                            vmin=("value", "min"), vmax=("value", "max"))
    m = g[(g["nf"] > 1) & (g["nv"] > 1)].copy()
    m["spread"] = (m["vmax"] - m["vmin"]).abs() / m[["vmin", "vmax"]].abs().max(axis=1).clip(lower=1)
    m = m[m["vmin"].abs() > 1e6]                      # ゼロ近傍のノイズを除く
    print("\n実額が大きく変わった例（|値| > 100万）:")
    for idx, r in m.sort_values("spread", ascending=False).head(5).iterrows():
        print("    cik=%-8s %-42s %s qtrs=%s  %.4g → %.4g (%.0f%%)"
              % (idx[0], idx[1][:42], pd.Timestamp(idx[2]).date(), idx[3],
                 r["vmin"], r["vmax"], 100 * r["spread"]))

    print("\n### PIT の正しい読み方")
    print("  正: value_as_of(t) = filed <= t を満たす行のうち filed が最大の value")
    print("  誤: groupby([cik,tag,ddate,qtrs]).last()  … 未来の訂正を使う")
    if len(m):
        k = m.sort_values("spread", ascending=False).index[0]
        s = df[(df["cik"] == k[0]) & (df["tag"] == k[1]) &
               (df["ddate"] == k[2]) & (df["qtrs"] == k[3])].sort_values("filed")
        print("\n対象 cik=%s tag=%s ddate=%s" % (k[0], k[1], pd.Timestamp(k[2]).date()))
        for _, r in s.iterrows():
            print("    filed=%s form=%-6s value=%.6g" % (r["filed"].date(), r["form"], r["value"]))
        t_mid = s["filed"].iloc[0] + (s["filed"].iloc[-1] - s["filed"].iloc[0]) / 2
        asof = s[s["filed"] <= t_mid]["value"].iloc[-1]
        naive = s["value"].iloc[-1]
        print("    t=%s 時点  正=%.6g  誤(最新)=%.6g  ずれ %.1f%%"
              % (t_mid.date(), asof, naive, 100 * abs(naive - asof) / max(abs(asof), 1)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--quarters", nargs="*")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--demo", action="store_true")
    a = ap.parse_args()

    qs = all_quarters()
    if a.list:
        print("対象四半期 %d 件: %s ... %s" % (len(qs), qs[0], qs[-1]))
        print("勘定コード %d 種 / us-gaap タグ %d 個を抽出対象にしている" % (len(TAG_MAP), len(TAG2CODE)))
        return 0
    if a.all:
        build(qs)
    elif a.quarters:
        build(a.quarters)
    if a.demo:
        demo()
    if not (a.all or a.quarters or a.demo):
        ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
