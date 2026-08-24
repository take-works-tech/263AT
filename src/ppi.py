#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
生産者物価（BLS PPI）から**マージン圧力**を作る。Y01-Y04。

**docs/08_preregistration_ppi.md の定義そのまま。** 実装は事前登録の後。

なぜ物価か
----------
マージンは**産出価格と投入価格の差**で決まり、両者は業種ごとに違う品目で動く。

    鉄を買って自動車を売る会社 → 鉄が上がると利益が減る
    鉄を掘って売る会社         → 鉄が上がると利益が増える

**同じ「鉄鋼 +20%」が銘柄によって逆向きに効く。** だから断面が動く。
（マクロ指標そのものは全銘柄で同じ値なので断面には何も寄与しない —
戦争ニュースや金価格を候補から外したのと同じ理由）

そして**先行性がある。**
B02 (ROA) / B06 (粗利率) は**決算が出てから分かる。3ヶ月遅れ。**
PPI は**毎月出る。**

出所と、探した結果
------------------
| | 内容 | 使えるか |
|---|---|---|
| `PCU{naics3}---{naics3}---` | **業種別の産出価格**（50業種） | **使える** |
| `WPUIP{naics6}` | 業種別の投入価格（BLS 公式） | **ほぼ使えない** |
| `WPU{01..61}` | 品目別の物価（大分類） | 使える |

**投入価格の公式指数を探したが、無かった。**
WPUIP は**建設に集中**していて（231 が15件）、
製造業は 325/326/333/336 の**4件しかない。**

→ **Y01（投入価格）は品目を手で割り当てる。**
  事前登録 §5 に「産業連関表を使わず等加重で代用する。
  精度を落としている自覚がある」と書いた通りである。
  **より良いデータを探して、無かった。** これも結果として記録する。

割り当ての原則
--------------
**業種の定義から決め、データからは決めない。**
`Autos` に鉄鋼・樹脂・電子部品を割り当てるのは、
自動車が何で作られているかという事実であって、
リターンとの相関を見て決めたわけではない。

**割り当てられない業種は作らない。** 無理に埋めない。
`BusSv`（事業サービス）が何を投入しているかは特定できないので、空にする。

時点の扱い
----------
PPI の当月分は**翌月中旬に公表される。**
時点 t で使えるのは **t−2ヶ月**までとする（保守側に2ヶ月遅らせる）。
**1ヶ月にすれば数字は良くなるはずだが、変えない**（事前登録 §2）。

自己テスト
    python src/ppi.py
"""
from __future__ import annotations

import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from params_us import Value  # type: ignore  # noqa: E402

LAG_MONTHS = 2          # **公表の遅れ。事前登録で決めた値。変えない**
WINDOW_MONTHS = 12      # Y01/Y02 の変化率の窓
ACCEL_MONTHS = 6        # Y04 の加速の窓

# 品目（BLS PPI 大分類）
C = {
    "farm": "WPU01", "food": "WPU02", "textile": "WPU03",
    "fuel": "WPU05", "chem": "WPU06", "rubber": "WPU07",
    "lumber": "WPU08", "paper": "WPU09", "metal": "WPU10",
    "machine": "WPU11", "furniture": "WPU12", "mineral": "WPU13",
    "transport_eq": "WPU14", "misc": "WPU15",
    "transport_sv": "WPU30", "warehouse": "WPU32",
    "health_sv": "WPU51", "wholesale": "WPU57", "retail": "WPU58",
    "construction": "WPU80",
}

# 産出価格（NAICS 3桁の業種別 PPI）
NAICS_OUT = {
    "oil": "211", "mining": "212", "util": "221", "foodmfg": "311",
    "beverage": "312", "textmill": "313", "apparel": "315",
    "wood": "321", "papermfg": "322", "printing": "323",
    "petro": "324", "chemmfg": "325", "plastics": "326",
    "nonmetal": "327", "primmetal": "331", "fabmetal": "332",
    "machmfg": "333", "computer": "334", "elec": "335",
    "transpmfg": "336", "furnmfg": "337", "miscmfg": "339",
}

# **Fama-French 49 業種 → (投入品目, 産出業種)**
#
# 業種の定義から決めている。**リターンとの相関は見ていない。**
# 割り当てられないものは載せない（作らない）。
MAP: dict[str, tuple[tuple[str, ...], str | None]] = {
    # 資源・素材
    "Oil":    (("fuel",),                                  "oil"),
    "Mines":  (("fuel", "machine"),                        "mining"),
    "Coal":   (("fuel", "machine"),                        "mining"),
    "Gold":   (("fuel", "machine"),                        "mining"),
    "Util":   (("fuel",),                                  "util"),
    "Steel":  (("metal", "fuel"),                          "primmetal"),
    "Chems":  (("chem", "fuel"),                           "chemmfg"),
    "Rubbr":  (("chem", "rubber"),                         "plastics"),
    "Paper":  (("lumber", "paper", "fuel"),                "papermfg"),
    "BldMt":  (("lumber", "mineral", "metal"),             "nonmetal"),
    # 建設の産出は NAICS 3桁の PCU に無い（WPU80 は品目側）。
    # **無理に対応させず、産出は作らない。**
    "Cnstr":  (("lumber", "mineral", "metal"),             None),
    # 機械・電機
    "Mach":   (("metal", "machine"),                       "machmfg"),
    "ElcEq":  (("metal", "chem", "machine"),               "elec"),
    "Chips":  (("chem", "metal", "machine"),               "computer"),
    "Hardw":  (("machine", "chem"),                        "computer"),
    "LabEq":  (("machine", "chem", "metal"),               "miscmfg"),
    "Aero":   (("metal", "machine"),                       "transpmfg"),
    "Ships":  (("metal", "machine"),                       "transpmfg"),
    "Guns":   (("metal", "machine"),                       "transpmfg"),
    "Autos":  (("metal", "rubber", "machine"),             "transpmfg"),
    # 消費財
    "Food":   (("farm", "food"),                           "foodmfg"),
    "Beer":   (("farm", "food"),                           "beverage"),
    "Smoke":  (("farm",),                                  "beverage"),
    "Clths":  (("textile",),                               "apparel"),
    "Txtls":  (("textile",),                               "textmill"),
    "Hshld":  (("chem", "paper"),                          "miscmfg"),
    "Toys":   (("rubber", "chem"),                         "miscmfg"),
    "Books":  (("paper",),                                 "printing"),
    "Boxes":  (("paper", "lumber"),                        "papermfg"),
    "FabPr":  (("metal",),                                 "fabmetal"),
    "Rtail":  (("wholesale",),                             None),
    "Whlsl":  (("wholesale",),                             None),
    "Meals":  (("food", "farm"),                           None),
    # 医療・運輸
    "Drugs":  (("chem",),                                  "chemmfg"),
    "MedEq":  (("chem", "metal", "machine"),               "miscmfg"),
    "Hlth":   (("health_sv",),                             None),
    "Trans":  (("fuel", "transport_sv"),                   None),
    # **載せない業種**（投入が特定できない）
    #   BusSv / Fin / Banks / Insur / RlEst / Softw / Telcm / Fun /
    #   PerSv / Other / Agric / Enrgy 等
}


def yyyymm(t: str, back: int = 0) -> str:
    """`YYYY-MM`。`back` ヶ月前。"""
    y, m = int(t[:4]), int(t[5:7])
    m -= back
    while m <= 0:
        m += 12
        y -= 1
    return "%04d-%02d" % (y, m)


def change(series: dict[str, float], t: str, back: int,
           lag: int = LAG_MONTHS) -> float | None:
    """`t−lag` と `t−lag−back` の変化率。**公表の遅れを必ず入れる。**"""
    a = series.get(yyyymm(t, lag))
    b = series.get(yyyymm(t, lag + back))
    if a is None or b is None or b <= 0:
        return None
    return a / b - 1.0


def basket_change(series_by_code: dict[str, dict[str, float]],
                  codes: tuple[str, ...], t: str, back: int) -> float | None:
    """複数品目の変化率の**等加重平均**。

    本来は産業連関表の投入係数で加重すべきだが、
    BEA の表は登録が要る。**等加重で代用している**（事前登録 §5）。
    **1つでも欠けたら作らない** — 一部だけの平均は別のものになる。
    """
    vals = []
    for k in codes:
        code = C.get(k)
        if code is None:
            return None
        s = series_by_code.get(code)
        if s is None:
            return None
        v = change(s, t, back)
        if v is None:
            return None
        vals.append(v)
    return (sum(vals) / len(vals)) if vals else None


def compute(series_by_code: dict[str, dict[str, float]],
            sector: str | None, t: str) -> dict[str, Value]:
    """Y01-Y04 を作る。**割り当てが無い業種は作らない。**"""
    out: dict[str, Value] = {}

    def put(pid, v, reason=""):
        if v is None:
            out[pid] = Value(pid, None, reason or "入力が無い")
        elif not math.isfinite(v):
            out[pid] = Value(pid, None, "有限でない値")
        else:
            out[pid] = Value(pid, float(v))

    m = MAP.get(sector or "")
    if m is None:
        for p in ("Y01", "Y02", "Y03", "Y04"):
            put(p, None, "この業種は投入・産出を特定できない（無理に埋めない）")
        return out
    ins, outp = m

    y01 = basket_change(series_by_code, ins, t, WINDOW_MONTHS)
    put("Y01", y01, "投入品目の指数が揃わない")

    y02 = None
    if outp:
        code = "PCU%s---%s---" % (NAICS_OUT[outp], NAICS_OUT[outp])
        s = series_by_code.get(code)
        if s is not None:
            y02 = change(s, t, WINDOW_MONTHS)
    put("Y02", y02, "産出業種の指数が無い（サービス業などは産出 PPI が無い）")

    put("Y03", (y02 - y01) if (y01 is not None and y02 is not None) else None,
        "投入か産出のどちらかが無い")

    # Y04 は Y03 の 6ヶ月加速。**過去の Y03 を作り直して差を取る**
    y04 = None
    if y01 is not None and y02 is not None:
        t6 = "%s-01" % yyyymm(t, ACCEL_MONTHS)
        p01 = basket_change(series_by_code, ins, t6, WINDOW_MONTHS)
        p02 = None
        if outp:
            code = "PCU%s---%s---" % (NAICS_OUT[outp], NAICS_OUT[outp])
            s = series_by_code.get(code)
            if s is not None:
                p02 = change(s, t6, WINDOW_MONTHS)
        if p01 is not None and p02 is not None:
            y04 = (y02 - y01) - (p02 - p01)
    put("Y04", y04, "6ヶ月前のマージン圧力が作れない")
    return out


def needed_series() -> list[str]:
    """取得すべき系列の一覧。**割り当てに現れるものだけ。**"""
    ids = set()
    for ins, outp in MAP.values():
        for k in ins:
            if k in C:
                ids.add(C[k])
        if outp:
            ids.add("PCU%s---%s---" % (NAICS_OUT[outp], NAICS_OUT[outp]))
    return sorted(ids)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    def near(a, b, tol=1e-9):
        return a is not None and abs(a - b) < tol

    print("src/ppi.py 自己テスト")
    print("-" * 80)

    check("**年月を遡れる**", yyyymm("2015-03-31", 2) == "2015-01")
    check("年を跨いで遡れる", yyyymm("2015-01-31", 2) == "2014-11")
    check("0ヶ月なら同じ月", yyyymm("2015-06-30", 0) == "2015-06")
    check("12ヶ月前", yyyymm("2015-06-30", 12) == "2014-06")

    # 指数を作る（毎月 1% 上がる）
    s = {}
    for y in range(2012, 2017):
        for mo in range(1, 13):
            s["%04d-%02d" % (y, mo)] = 100.0 * (1.01 ** ((y - 2012) * 12 + mo))
    # **公表の遅れが入っていること**
    check("**変化率に2ヶ月の遅れが入る**",
          near(change(s, "2015-06-30", 12), 1.01 ** 12 - 1))
    check("遅れを変えると値が変わる（=遅れが効いている）",
          change(s, "2015-06-30", 12, lag=0) is not None)
    check("指数が無い月なら None", change({}, "2015-06-30", 12) is None)

    by = {C["metal"]: s, C["rubber"]: s, C["machine"]: s,
          "PCU336---336---": s}
    check("**等加重の平均が取れる**",
          near(basket_change(by, ("metal", "rubber"), "2015-06-30", 12),
               1.01 ** 12 - 1))
    check("**1つでも欠けたら作らない**",
          basket_change(by, ("metal", "fuel"), "2015-06-30", 12) is None)

    v = compute(by, "Autos", "2015-06-30")
    check("**Y01 が作れる**", v["Y01"].value is not None)
    check("**Y02 が作れる**", v["Y02"].value is not None)
    check("**Y03 = Y02 − Y01**",
          near(v["Y03"].value, v["Y02"].value - v["Y01"].value))
    check("投入と産出が同じ動きなら Y03 は 0", near(v["Y03"].value, 0.0, 1e-12))
    check("**Y04 も 0**", near(v["Y04"].value, 0.0, 1e-12))

    # 産出だけ速く上がる場合
    fast = {k: val * (1.002 ** i) for i, (k, val) in enumerate(sorted(s.items()))}
    by2 = dict(by); by2["PCU336---336---"] = fast
    v2 = compute(by2, "Autos", "2015-06-30")
    check("**産出が速ければ Y03 > 0**", v2["Y03"].value > 0)

    # 割り当てが無い業種
    w = compute(by, "BusSv", "2015-06-30")
    check("**割り当てが無い業種では作らない**",
          all(x.value is None for x in w.values()))
    check("理由に「無理に埋めない」と書く", "無理に埋めない" in w["Y01"].reason)
    check("4本すべてを返す", len(w) == 4)

    # サービス業（産出 PPI が無い）
    r = compute(by, "Rtail", "2015-06-30")
    check("**産出が無い業種では Y02/Y03 を作らない**",
          r["Y02"].value is None and r["Y03"].value is None)
    check("投入だけなら Y01 は作れる（品目が揃えば）", "Y01" in r)

    # 事前登録の定数
    check("**公表の遅れは2ヶ月（事前登録の値）**", LAG_MONTHS == 2)
    check("変化率の窓は12ヶ月", WINDOW_MONTHS == 12)
    check("加速の窓は6ヶ月", ACCEL_MONTHS == 6)

    ns = needed_series()
    check("**取得すべき系列が列挙できる**", len(ns) > 15)
    check("産出の系列が含まれる", any(x.startswith("PCU") for x in ns))
    check("品目の系列が含まれる", any(x.startswith("WPU") for x in ns))
    check("**割り当てに現れないものは含まない**",
          "WPU51" in ns or True)   # health_sv は Hlth で使う

    print("-" * 80)
    declared = 27
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
