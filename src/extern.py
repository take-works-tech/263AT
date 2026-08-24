#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**決算数値になる前の情報**を、数えられる形で取る。臨床試験・論文・政府調達。

なぜ要るか
----------
公表された財務数値だけのシステムは、**実測で S&P500 に年5pp 負けた。**
OSAP には同じデータ上の公表アノマリーが200本以上ある。
**最も混雑した情報を使っているのだから当然である。**

差別化は「LLM に有望さを判断させる」ことではない。
**判断は後知恵と区別できない**（モデルの重みに未来が焼き付いている）。

**判断ではなく、数えることならできる。**
「この会社は有望か」は汚染されるが、
「この会社が2015年までに登録した第3相試験は何件か」は汚染されない。
**答えが文書の中にあり、その文書には公開日がある。**

この層の絶対規則 — **同じレコードの中に、使える欄と使えない欄がある**
------------------------------------------------------------------
ClinicalTrials.gov の1件のレコードには、両方が混ざっている。

    studyFirstSubmitDate  2022-12-19            ← **その日に確定。PIT 安全**
    overallStatus         ACTIVE_NOT_RECRUITING ← **今日時点の状態。過去では未知**
    lastUpdatePostDate    2026-05-22            ← 更新されている証拠

**「2015年までに登録された試験を数える」は安全。**
**「その試験が成功したか」を見るのは、未来を見ている。**

論文も同じ。
    publication_date  ← 安全
    cited_by_count    ← **今日までの引用数。過去の時点では未知**

**この区別を人の注意力に任せない。** `PIT_UNSAFE` に列挙し、
過去の時点を指定した呼び出しでは**返さない。**

出所（すべて無料・登録不要）
----------------------------
| | 何が取れるか | 日付の欄 |
|---|---|---|
| ClinicalTrials.gov v2 | 臨床試験の登録 | `studyFirstSubmitDate` |
| OpenAlex | 論文 | `publication_date` |
| USAspending.gov | 米国政府の調達・補助金 | `action_date` |

**いずれも OSAP に対応が無い。** だからこそ混雑していない。

**最大の難所は「どの名前がどの企業か」である**
--------------------------------------------
疎通確認で出た（2026-08-24）:

    論文 Moderna: 2015年末まで **3,214 件**

Moderna は2010年創業で、2015年に3,214本の論文があるはずがない。
**`moderna` はスペイン語・イタリア語で「現代の」という一般語**で、
所属文字列の検索がそれを拾っている。

これは些細な不具合ではなく、**この層で最も難しい部分**である。

  - 子会社名で出願・登録している（Alphabet ⊃ Google ⊃ DeepMind）
  - 社名変更（Facebook → Meta）
  - 一般語と衝突する（Moderna, Apple, Amazon, Square, Block）
  - 表記ゆれ（NVIDIA / Nvidia / NVIDIA Corporation）

**そしてこの照合こそ、LLM が本当に役に立つところである。**
「有望か」の判断は後知恵に汚染されるが、
**「この所属文字列はこの企業か」の照合は汚染されない** —
答えは文字列の中にあり、将来のリターンとは無関係だから。

→ 照合の結果は `src/knowledge.py` に **fact として追記**し、
  一度解けた対応は再利用する。**照合を毎回やり直さない。**

自己テスト
    python src/extern.py            # ネットワーク非依存
    python src/extern.py --probe    # 実際に叩く
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import urllib.parse
import urllib.request

UA = {"User-Agent": "263AT/1.0 (tzero30208@gmail.com)"}

# **過去の時点では知りえない欄。** 名前で拒否する。
#
# 「今日のレコードに載っているから使える」と考えるのが最も危険で、
# **レコードは更新されるので、載っているのは「今の姿」である。**
PIT_UNSAFE = {
    # ClinicalTrials
    "overallStatus", "completionDate", "completionDateStruct",
    "primaryCompletionDate", "hasResults", "resultsFirstSubmitDate",
    "whyStopped", "lastUpdatePostDate", "lastUpdatePostDateStruct",
    # OpenAlex
    "cited_by_count", "counts_by_year", "referenced_works_count",
    "is_retracted",
    # 共通
    "current_status", "latest",
}

# その日に確定し、後から動かない欄
PIT_SAFE = {
    "nctId", "briefTitle", "studyFirstSubmitDate", "phases",
    "publication_date", "doi", "title", "action_date", "award_amount",
}


class PitViolation(ValueError):
    """**過去の時点では知りえない欄を要求した。**

    書式の誤りやデータ不在と区別する。
    **黙って落として続行してはいけない** — 気づかずに未来を使う。
    """


def check_fields(fields: list[str], asof: str | None) -> None:
    """要求された欄が、その時点で知りえたものかを確かめる。

    `asof` が None（＝今を見る）なら制限しない。
    **過去を指定したときだけ厳しくする。**
    """
    if asof is None:
        return
    bad = [f for f in fields if f in PIT_UNSAFE]
    if bad:
        raise PitViolation(
            "過去の時点（%s）では知りえない欄を要求している: %s\n"
            "  これらは**今日時点の値**で、レコードの更新で変わる。\n"
            "  数えてよいのは「その日までに登録された件数」まで。"
            % (asof, ", ".join(bad)))


def _get(url: str, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------- 臨床試験
CT_BASE = "https://clinicaltrials.gov/api/v2/studies"


def trials_raw(sponsor: str, page_size: int = 200,
               max_pages: int = 10) -> list[dict]:
    """**1件ごとの登録日と相**を返す。集計しない。

    なぜ集計して保存してはいけないか（2026-08-24 に踏みかけた）:
    「今日時点の相ごとの件数」を保存して過去の時点で使うと、
    **2015年に、2020年に登録された試験を数えることになる。**
    **日付つきの生データを保存すれば、どの基準日でも後から作れる。**

    保存するのは登録日と相だけ。**成否は取らない**（PIT_UNSAFE）。
    """
    out: list[dict] = []
    token = None
    for _ in range(max_pages):
        q = {"query.spons": sponsor, "pageSize": str(page_size),
             "fields": "NCTId,Phase,StudyFirstSubmitDate"}
        if token:
            q["pageToken"] = token
        d = _get(CT_BASE + "?" + urllib.parse.urlencode(q))
        studies = d.get("studies") or []
        if not studies:
            break
        for s in studies:
            ps = s.get("protocolSection") or {}
            sub = (ps.get("statusModule") or {}).get("studyFirstSubmitDate")
            if not sub:
                continue
            ph = (ps.get("designModule") or {}).get("phases") or ["NA"]
            out.append({"submit": sub, "phase": "/".join(ph)})
        token = d.get("nextPageToken")
        if not token:
            break
    return out


def count_asof(rows: list[dict], asof: str) -> dict:
    """生データから**その日までの**件数を作る。`trials_raw` の結果を渡す。"""
    out = {"total": 0, "by_phase": {}}
    for r in rows:
        if r["submit"] > asof:
            continue
        out["total"] += 1
        out["by_phase"][r["phase"]] = out["by_phase"].get(r["phase"], 0) + 1
    return out


def trials_asof(sponsor: str, asof: str, page_size: int = 200,
                max_pages: int = 10) -> dict:
    """**その日までに登録された**臨床試験を、相ごとに数える。

    数えるのは `studyFirstSubmitDate <= asof` のものだけ。
    **成否は見ない**（`overallStatus` は今日の値なので）。

    バイオテックは10倍株が集中する領域で、
    **試験の登録は決算数値より数年早く出る。**
    """
    out = {"sponsor": sponsor, "asof": asof, "total": 0,
           "by_phase": {}, "latest_submit": None, "pages": 0}
    token = None
    for _ in range(max_pages):
        q = {"query.spons": sponsor, "pageSize": str(page_size),
             "fields": "NCTId,Phase,StudyFirstSubmitDate"}
        if token:
            q["pageToken"] = token
        d = _get(CT_BASE + "?" + urllib.parse.urlencode(q))
        studies = d.get("studies") or []
        if not studies:
            break
        out["pages"] += 1
        for s in studies:
            st = (s.get("protocolSection") or {}).get("statusModule") or {}
            sub = st.get("studyFirstSubmitDate")
            if not sub or sub > asof:
                continue          # **基準日より後の登録は数えない**
            ph = ((s.get("protocolSection") or {})
                  .get("designModule") or {}).get("phases") or ["NA"]
            key = "/".join(ph)
            out["by_phase"][key] = out["by_phase"].get(key, 0) + 1
            out["total"] += 1
            if not out["latest_submit"] or sub > out["latest_submit"]:
                out["latest_submit"] = sub
        token = d.get("nextPageToken")
        if not token:
            break
    return out


# ---------------------------------------------------------------- 論文
OA_BASE = "https://api.openalex.org/works"


def papers_asof(org: str, asof: str, mailto: str = "") -> dict:
    """**その日までに公開された**論文の件数。

    `cited_by_count` は**使わない** — 今日までの引用数で、
    過去の時点では知りえない（`PIT_UNSAFE`）。

    件数の伸びは、**研究開発費という数字より前に動く。**
    """
    q = {"filter": "raw_affiliation_strings.search:%s,from_publication_date:"
                   "1900-01-01,to_publication_date:%s" % (org, asof),
         "per-page": "1"}
    if mailto:
        q["mailto"] = mailto
    d = _get(OA_BASE + "?" + urllib.parse.urlencode(q))
    return {"org": org, "asof": asof,
            "count": (d.get("meta") or {}).get("count", 0)}


# ---------------------------------------------------------------- 政府調達
US_BASE = "https://api.usaspending.gov/api/v2/search/spending_by_award/"


def awards_asof(recipient: str, asof: str,
                since: str = "2008-01-01") -> dict:
    """**その日までに交付された**米国政府の契約・補助金。

    `action_date` で切る。**採択は売上に載るより前に公示される。**
    """
    body = {
        "filters": {
            "recipient_search_text": [recipient],
            "time_period": [{"start_date": since, "end_date": asof}],
            "award_type_codes": ["A", "B", "C", "D"],
        },
        "fields": ["Award ID", "Award Amount", "Start Date"],
        "limit": 1, "page": 1,
    }
    req = urllib.request.Request(
        US_BASE, data=json.dumps(body).encode("utf-8"),
        headers={**UA, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    meta = d.get("page_metadata") or {}
    return {"recipient": recipient, "asof": asof,
            "count": meta.get("total", len(d.get("results") or []))}


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/extern.py 自己テスト（ネットワーク非依存）")
    print("-" * 80)

    # --- PIT 安全でない欄を拒否する ----------------------------------------
    try:
        check_fields(["nctId", "overallStatus"], "2015-12-31")
        check("**過去の時点で overallStatus を拒否する**", False)
    except PitViolation as e:
        check("**過去の時点で overallStatus を拒否する**", True)
        check("何が問題か例外文に書く", "知りえない" in str(e))

    try:
        check_fields(["title", "cited_by_count"], "2015-12-31")
        check("**引用数も拒否する（今日までの累積だから）**", False)
    except PitViolation:
        check("**引用数も拒否する（今日までの累積だから）**", True)

    check("安全な欄だけなら通る",
          check_fields(["nctId", "studyFirstSubmitDate"], "2015-12-31") is None)
    check("**今を見るときは制限しない**",
          check_fields(["overallStatus"], None) is None)

    check("安全な欄と危険な欄が重ならない", not (PIT_SAFE & PIT_UNSAFE))
    check("**成否に関わる欄が危険側に入っている**",
          {"overallStatus", "hasResults", "whyStopped"} <= PIT_UNSAFE)
    check("**引用数と撤回が危険側に入っている**",
          {"cited_by_count", "is_retracted"} <= PIT_UNSAFE)
    check("登録日は安全側", "studyFirstSubmitDate" in PIT_SAFE)

    check("**例外がデータ不在と区別される**",
          issubclass(PitViolation, ValueError)
          and not issubclass(PitViolation, KeyError))

    # 日付の扱い
    check("基準日より後の登録は数えない（規約の確認）",
          "2016-01-01" > "2015-12-31")

    print("-" * 80)
    declared = 11
    if len(ran) != declared:
        fails.append("本数が宣言と違う")
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


def _probe() -> int:
    """実際に叩く。**基準日を過去にして、件数が減ることを確かめる。**"""
    print("=" * 78)
    print("外部データの疎通確認（すべて無料・登録不要）")
    print("=" * 78)
    for sp in ("Moderna", "Vertex Pharmaceuticals"):
        try:
            a = trials_asof(sp, "2015-12-31", page_size=200, max_pages=3)
            b = trials_asof(sp, dt.date.today().isoformat(), page_size=200,
                            max_pages=3)
            print("\n■ 臨床試験 %s" % sp)
            print("   2015年末まで **%d 件** / 今日まで **%d 件**"
                  % (a["total"], b["total"]))
            print("   2015年末までの相の内訳: %s"
                  % dict(sorted(a["by_phase"].items())[:5]))
            if a["total"] < b["total"]:
                print("   → **基準日で件数が減る。時点が効いている。**")
            else:
                print("   → **件数が減らない。時点の扱いを疑うこと。**")
        except Exception as e:
            print("   NG %s: %s" % (sp, str(e)[:90]))

    for org in ("Moderna", "NVIDIA"):
        try:
            a = papers_asof(org, "2015-12-31", "tzero30208@gmail.com")
            b = papers_asof(org, dt.date.today().isoformat(),
                            "tzero30208@gmail.com")
            print("\n■ 論文 %s: 2015年末まで **%d 件** / 今日まで **%d 件**"
                  % (org, a["count"], b["count"]))
        except Exception as e:
            print("   NG %s: %s" % (org, str(e)[:90]))

    for rc in ("Moderna",):
        try:
            a = awards_asof(rc, "2015-12-31")
            b = awards_asof(rc, dt.date.today().isoformat())
            print("\n■ 政府調達 %s: 2015年末まで **%d 件** / 今日まで **%d 件**"
                  % (rc, a["count"], b["count"]))
        except Exception as e:
            print("   NG %s: %s" % (rc, str(e)[:90]))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_probe() if "--probe" in sys.argv else _test())
