#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**匿名化した企業プロファイル**を作る。LLM に「後知恵を使わせない」ための道具。

なぜ要るか
----------
LLM 由来のスコアは、出典を公開日で絞っても**汚染される。**
汚染されるのは入力ではなく**モデルの重み**で、
2012年のテスラの記事だけを見せても、モデルは 100倍になったことを知っている。

**思い出せなければ、後知恵は使えない。**
だから社名・ティッカー・固有名詞を消し、**数字と業種だけ**を見せる。

**そして「本当に思い出せないか」を測れる。**
匿名化したプロファイルを見せて「どこの会社か」と聞く。
当てられるなら匿名化が不十分で、そのスコアは信用できない。
**汚染されていないことを測れるのは、この方法だけである。**

匿名化の強さは3段階
-------------------
| 段階 | 何を見せるか | 判断力 | 特定されやすさ |
|---|---|---|---|
| `raw` | 実額・業種名 | 高い | **高い**（売上7.0Bの電気自動車＝1社しかない） |
| `bucket` | **桁と比率だけ**・業種名 | 中 | 中 |
| `strict` | 桁と比率・**業種は大分類のみ** | 低い | 低い |

**強く匿名化するほど判断力が落ちる。** これは避けられない交換で、
**どこで釣り合うかは測って決める。**

金額を桁に丸めるのは、**実額が指紋になる**から。
「売上 7,000百万ドル」は検索すれば1社に絞れるが、
「売上 数十億ドル規模」なら絞れない。

使い方
    .venv/Scripts/python.exe tools/anon_profile.py --tickers AAPL NVDA --asof 2015-12-31
    .venv/Scripts/python.exe tools/anon_profile.py --sample 12 --asof 2015-12-31 --level bucket
"""
from __future__ import annotations

import argparse
import json
import math
import pathlib
import random
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import bars as BR             # noqa: E402
import facts as FA            # noqa: E402
import ff49                   # noqa: E402
import listing as LS          # noqa: E402
import periods as PE          # noqa: E402
import prices as PR           # noqa: E402

# FF49 → もっと粗い分類。**strict で業種を潰すため**
COARSE = {
    "Software": "情報技術", "Hardware": "情報技術", "Chips": "情報技術",
    "LabEq": "情報技術", "BusSv": "サービス", "Telcm": "通信",
    "Drugs": "医療", "MedEq": "医療", "Hlth": "医療",
    "Banks": "金融", "Insur": "金融", "RlEst": "金融", "Fin": "金融",
    "Oil": "資源", "Mines": "資源", "Gold": "資源", "Coal": "資源",
    "Util": "公益", "Rtail": "消費", "Meals": "消費", "Food": "消費",
    "Beer": "消費", "Smoke": "消費", "Toys": "消費", "Clths": "消費",
    "Hshld": "消費", "Fun": "消費", "Books": "消費",
}


def _mag(x: float) -> str:
    """金額を**桁**にする。実額は指紋になるので出さない。"""
    if x is None:
        return "不明"
    a = abs(x)
    if a < 1e6:
        s = "100万ドル未満"
    elif a < 1e7:
        s = "1千万ドル規模"
    elif a < 1e8:
        s = "1億ドル規模"
    elif a < 1e9:
        s = "数億ドル規模"
    elif a < 1e10:
        s = "数十億ドル規模"
    elif a < 1e11:
        s = "数百億ドル規模"
    else:
        s = "1千億ドル超"
    return ("マイナス" + s) if x < 0 else s


def _pct(a, b, nd=1):
    if a is None or not b:
        return "不明"
    return "%.*f%%" % (nd, 100.0 * a / b)


def profile(asof: FA.AsOf, cik: int, ticker: str, t: str,
            sic: int | None, rows: list[dict],
            level: str = "bucket") -> dict | None:
    """時点 t の匿名プロファイル。**t 以降の情報は一切使わない。**"""
    def ttm(code):
        v = PE.ttm(asof, cik, code, t)
        return v.value if v else None

    def pt(code):
        v = asof.latest_period(cik, code, 0, t, max_lag_days=400)
        return v.value if v else None

    rev, ni, gp, op = ttm("REV"), ttm("NI"), ttm("GP"), ttm("OP")
    cfo, capex, rd = ttm("CFO"), ttm("CAPEX"), ttm("RD")
    ta, eq, cash = pt("TA"), pt("EQ"), pt("CASH")
    dl, ds, sh = pt("DEBT_LT"), pt("DEBT_ST"), pt("SHARES")
    if rev is None or ta is None or not sh:
        return None

    px = rows[-1]["close"] if rows else None
    if not px:
        return None
    mcap = sh * px
    debt = (dl or 0) + (ds or 0)

    ind = ff49.industry(sic) if sic else None
    shown_ind = (COARSE.get(ind, "その他") if level == "strict"
                 else (ind or "不明"))

    # 価格の推移は**比率でだけ**出す（実額の株価も指紋になる）
    def ret(n):
        if len(rows) <= n or rows[-1 - n]["close"] <= 0:
            return None
        return rows[-1]["close"] / rows[-1 - n]["close"] - 1.0

    body = {
        "業種": shown_ind,
        "売上規模": _mag(rev) if level != "raw" else "%.0f" % rev,
        "総資産規模": _mag(ta) if level != "raw" else "%.0f" % ta,
        "時価総額規模": _mag(mcap) if level != "raw" else "%.0f" % mcap,
        "粗利率": _pct(gp, rev),
        "営業利益率": _pct(op, rev),
        "純利益率": _pct(ni, rev),
        "研究開発費/売上": _pct(rd, rev),
        "営業CF/売上": _pct(cfo, rev),
        "設備投資/売上": _pct(abs(capex) if capex else None, rev),
        "自己資本比率": _pct(eq, ta),
        "有利子負債/総資産": _pct(debt, ta),
        "現金/総資産": _pct(cash, ta),
        "PSR": ("%.2f" % (mcap / rev)) if rev else "不明",
        "PBR": ("%.2f" % (mcap / eq)) if eq else "不明",
        "株価水準帯": ("$1未満" if px < 1 else "$1-5" if px < 5
                       else "$5-20" if px < 20 else "$20-100"
                       if px < 100 else "$100超"),
        "直近1年の株価変化": _pct(ret(252), 1.0, 0) if ret(252) is not None
                              else "不明",
        "直近3年の株価変化": _pct(ret(756), 1.0, 0) if ret(756) is not None
                              else "不明",
    }
    if level == "strict":
        # **PSR/PBR と株価変化の組み合わせは、業種と合わせると指紋になる**
        for k in ("PSR", "PBR", "直近3年の株価変化"):
            body.pop(k, None)
    return {"ticker": ticker, "asof": t, "level": level, "profile": body}


def render(p: dict) -> str:
    lines = ["【匿名企業プロファイル / 基準日 %s】" % p["asof"]]
    for k, v in p["profile"].items():
        lines.append("  %-16s %s" % (k, v))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", nargs="*")
    ap.add_argument("--sample", type=int, default=0)
    ap.add_argument("--asof", default="2015-12-31")
    ap.add_argument("--level", default="bucket",
                    choices=["raw", "bucket", "strict"])
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    y = int(a.asof[:4])
    qs = ["%dq%d" % (yy, q) for yy in range(y - 2, y + 1) for q in (1, 2, 3, 4)]
    by = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    sic_asof = LS.SicAsOf.from_dera()

    if a.tickers:
        cand = a.tickers
    else:
        all_t = sorted(p.stem for p in (ROOT / "data" / "prices").glob("*.json"))
        random.seed(0)
        cand = random.sample(all_t, min(len(all_t), a.sample * 8))

    need = {int(by[t].cik) for t in cand if by.get(t) and by[t].cik}
    asof = FA.AsOf(FA.load(qs, ciks=need))

    out = []
    for tk in cand:
        m = by.get(tk)
        if not m or not m.cik:
            continue
        s = PR.load([tk]).get(tk)
        if not s:
            continue
        rows = [x for x in BR.adjust(s.bars) if x["date"] <= a.asof]
        if len(rows) < 260:
            continue
        cik = int(m.cik)
        p = profile(asof, cik, tk, a.asof, sic_asof.get(cik, a.asof), rows,
                    a.level)
        if p:
            out.append(p)
        if a.sample and len(out) >= a.sample:
            break

    print("匿名プロファイル %d 件（基準日 %s / 匿名化 %s）"
          % (len(out), a.asof, a.level))
    for p in out:
        print()
        print(render(p))
    if a.out:
        pathlib.Path(a.out).write_text(json.dumps(out, ensure_ascii=False,
                                                  indent=1), encoding="utf-8")
        print("\n→ %s に書き出した（**ticker を含むので LLM には profile だけ渡す**）"
              % a.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
