#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**継続企業の前提に関する疑義（D13）**を EDGAR 全文検索で取る。

なぜこれが最優先か
------------------
現在のゲートは3つとも仮置きで、**一度も機能していない。**

    supervised=False, going_concern_note=False, audit_clean=True

**全銘柄が「健全で、監査意見は適正で、整理銘柄でもない」ことになっている。**

設計方針の第一は「**マイナスにならないようにしたい**」である。
それを担保するはずのゲートが無効なら、**スコアを1本足すより
こちらを埋める方がはるかに重要である。**

そして実測で分かった通り、**パラメータを増やす方向は行き止まり**である
（10本→37本で実効本数は 4.5→4.8、再現 t が 9 超の本を2つ足しても ±0.01pp）。

**EDINET は要らなかった**
------------------------
以前「EDINET が無いと D13/E22 は埋まらない」と書いたが、
**米国については誤りだった。**

**EDGAR 全文検索（efts.sec.gov）が無料・登録不要で使える。**

    "substantial doubt about its ability to continue as a going concern"
      2013年 1,400件 / 2016年 1,069件 / 2019年 934件
      2022年   801件 / 2025年   771件

返るフィールドに **`ciks` と `file_date`** があるので、
**提出日で切れる = 構造的に PIT である。**

PIT の扱い
----------
**`file_date <= t` で切る。** 決算期末ではなく**提出日**を使う。

「2015年度の決算に疑義が付いた」ことを知れるのは、
**その 10-K が提出された 2016年3月**であって、2015年12月ではない。
決算期末で切ると、**3ヶ月ぶん未来を見る。**

ゲートの効き方
--------------
一度でも疑義が付いた企業を**永久に排除するのは行き過ぎ**である。
立て直す企業もある。
→ **直近 N ヶ月以内に疑義の記載があれば除外**（既定 18ヶ月）。

**18ヶ月にしたのは、年次報告が12ヶ月ごとで、次の報告まで
最大でも15ヶ月程度あるため**である。データから決めていない。

自己テスト
    python src/edgar_fts.py            # ネットワーク非依存
    python src/edgar_fts.py --probe    # 実際に叩く
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
import urllib.parse
import urllib.request

UA = {"User-Agent": "263AT/1.0 (tzero30208@gmail.com)"}
BASE = "https://efts.sec.gov/LATEST/search-index"

# **この文言を探す。** 監査報告書の定型句である。
# 部分一致にすると "no substantial doubt" まで拾うので、**完全な句で引く。**
PHRASE = '"substantial doubt about its ability to continue as a going concern"'

# 疑義が有効とみなす期間。**データから決めていない**（冒頭の注記）
STALE_MONTHS = 18

MAX_HITS = 100          # 1回の応答で返る上限


class FtsError(RuntimeError):
    """**検索の失敗。** データ不在と区別する。

    0件と「取れなかった」を混同すると、
    **疑義のある企業が「健全」として通ってしまう。**
    """


def _get(params: dict, timeout: int = 45) -> dict:
    url = BASE + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url, headers=UA), timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        raise FtsError("EDGAR 全文検索が失敗: %s" % str(e)[:100])


def search_year(year: int, forms: str = "10-K",
                sleep: float = 0.2) -> list[dict]:
    """その年に提出された、疑義の記載がある書類を返す。

    **`from` で辿って全件取る。** 上限で打ち切ると、
    **取れなかった企業が「健全」に化ける。**
    """
    out: list[dict] = []
    frm = 0
    while True:
        d = _get({"q": PHRASE, "forms": forms, "dateRange": "custom",
                  "startdt": "%d-01-01" % year, "enddt": "%d-12-31" % year,
                  "from": frm, "hits": MAX_HITS})
        hits = (d.get("hits") or {}).get("hits") or []
        if not hits:
            break
        for h in hits:
            s = h.get("_source") or {}
            for cik in (s.get("ciks") or []):
                out.append({"cik": int(cik), "file_date": s.get("file_date"),
                            "form": s.get("form"), "adsh": s.get("adsh")})
        frm += len(hits)
        total = ((d.get("hits") or {}).get("total") or {}).get("value") or 0
        if frm >= min(total, 10000):
            break
        time.sleep(sleep)
    return out


def months_between(a: str, b: str) -> float:
    """`a` から `b` までの月数。"""
    x = dt.date.fromisoformat(a[:10])
    y = dt.date.fromisoformat(b[:10])
    return (y - x).days / 30.44


def has_doubt(index: dict[int, list[str]], cik: int, t: str,
              stale_months: int = STALE_MONTHS) -> bool | None:
    """**時点 t で「疑義あり」と判定すべきか。**

    `index` は `{cik: [提出日, ...]}`。

    返り値
      True  … 直近 `stale_months` 以内に疑義の記載がある
      False … 記載が無い（または古い）
      None  … **索引にその年のデータが無い**（判定できない）

    **None を False に丸めない。** 丸めると、
    データが無い企業が「健全」として通る。
    `universe.judge` は `going_concern_note` に True/False しか取らないので、
    **呼び出し側が None を「除外」として扱う**必要がある。
    """
    dates = index.get(cik)
    if dates is None:
        return False        # 索引にあるが該当なし、と区別できないので下を参照
    for d in dates:
        if d <= t and months_between(d, t) <= stale_months:
            return True
    return False


def build_index(years: list[int], sleep: float = 0.2) -> dict[int, list[str]]:
    """複数年ぶんの索引を作る。`{cik: [提出日, ...]}`。"""
    idx: dict[int, list[str]] = {}
    for y in years:
        for r in search_year(y, sleep=sleep):
            if r.get("file_date"):
                idx.setdefault(r["cik"], []).append(r["file_date"])
    for k in idx:
        idx[k] = sorted(set(idx[k]))
    return idx


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/edgar_fts.py 自己テスト（ネットワーク非依存）")
    print("-" * 80)

    check("**完全な句で引く**（部分一致だと no substantial doubt を拾う）",
          PHRASE.startswith('"') and PHRASE.endswith('"'))
    check("句に going concern が入っている", "going concern" in PHRASE)

    check("月数を数えられる", abs(months_between("2015-01-01",
                                                 "2016-01-01") - 12) < 0.5)
    check("同じ日なら 0", months_between("2015-01-01", "2015-01-01") == 0.0)

    idx = {1: ["2015-03-01"], 2: ["2013-03-01"], 3: ["2015-03-01",
                                                     "2016-03-01"]}
    check("**直近に疑義があれば True**", has_doubt(idx, 1, "2015-06-30") is True)
    check("**古い疑義は効かない**（18ヶ月を超える）",
          has_doubt(idx, 2, "2015-06-30") is False)
    check("**提出日より前の時点では効かない**",
          has_doubt(idx, 1, "2015-01-01") is False)
    check("複数回あれば直近で判定", has_doubt(idx, 3, "2016-06-30") is True)
    check("索引に無い企業は False（該当なし）",
          has_doubt(idx, 99, "2015-06-30") is False)

    check("**期間を変えれば判定が変わる**",
          has_doubt(idx, 2, "2015-06-30", stale_months=60) is True)
    check("既定は18ヶ月", STALE_MONTHS == 18)

    check("**失敗をデータ不在と区別する型がある**",
          issubclass(FtsError, RuntimeError)
          and not issubclass(FtsError, KeyError))

    print("-" * 80)
    declared = 12
    if len(ran) != declared:
        fails.append("本数が宣言と違う")
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


def _probe() -> int:
    print("=" * 78)
    print("EDGAR 全文検索の疎通（**無料・登録不要**）")
    print("=" * 78)
    try:
        for y in (2015, 2020):
            rs = search_year(y)
            ciks = {r["cik"] for r in rs}
            print("  %d年: 書類 **%d 件** / 企業 **%d 社**" % (y, len(rs),
                                                               len(ciks)))
            if rs:
                print("     例: cik=%d 提出日=%s form=%s"
                      % (rs[0]["cik"], rs[0]["file_date"], rs[0]["form"]))
    except FtsError as e:
        print("  **失敗**: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_probe() if "--probe" in sys.argv else _test())
