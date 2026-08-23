#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
EDINET API v2 クライアント（日本の開示書類）。

**遅延なし。API キーだけで全機能が使える**（無料）。
docs/06_accounts.md §2 を参照。

263AT にとって EDINET が重要な理由
----------------------------------
**現在ゲートが仮置きになっている2つを埋められる。**

| 取れるもの | パラメータ | 現状 |
|---|---|---|
| **継続企業の前提に関する注記** | **D13（ゲート）** | `True` で仮置き |
| **監査意見** | **E22（ゲート）** | `True` で仮置き |
| 政策保有株式 | K17 / A18 / M23 | **日本固有の優位性の中核** |
| 大量保有報告書 | M11 | アクティビストの参戦 |
| 有報のリスク項目 | T08 / E14 | 企業が自ら書いた悪材料 |

`universe.py` は **`None` を「適正」に丸めない**設計なので、
**データを入れるだけでゲートが正しく厳しくなる。**

PIT の扱い — **EDINET は構造的に PIT である**
--------------------------------------------
書類一覧 API は**日付を指定して「その日に提出された書類」を返す。**
DERA と同じく **PIT がデータ構造として保証される。**

→ `submitDateTime` が `available_at` そのものになる。
  spec §1.1 の3つの時刻のうち、**disclosure_date が正確に取れる**数少ない例。

自己テスト
    python src/edinet.py            # ネットワーク非依存の部分
    python src/edinet.py --probe    # 実際に叩く（.env が要る）
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import credentials as CR  # type: ignore  # noqa: E402

BASE = "https://api.edinet-fsa.go.jp/api/v2"

# 書類種別コード（EDINET の仕様）。**必要なものだけ持つ。**
DOC_TYPES = {
    "120": "有価証券報告書",
    "130": "訂正有価証券報告書",
    "140": "四半期報告書",
    "160": "半期報告書",
    "180": "臨時報告書",
    "350": "大量保有報告書",          # M11
    "360": "変更報告書",              # M11 の変化
    "030": "有価証券届出書",          # F05 増資
}

# 263AT が実際に使う書類。**全部取ると量が膨大になる。**
WANTED = {"120", "130", "140", "160", "350", "360"}


class EdinetError(RuntimeError):
    """**API のエラー。** データ不在と区別する。"""


@dataclasses.dataclass(frozen=True)
class Document:
    """1件の開示書類。**提出日時が available_at になる。**"""

    doc_id: str
    edinet_code: str | None
    sec_code: str | None          # 証券コード（5桁。末尾0を落とすと4桁）
    filer: str
    doc_type: str
    doc_description: str
    submit_datetime: str          # **これが available_at**
    period_end: str | None
    has_pdf: bool
    has_csv: bool

    def ticker(self) -> str | None:
        """証券コード5桁 → yfinance 形式の4桁 + `.T`。

        **EDINET の secCode は5桁**（末尾に 0 が付く）。
        4桁に落とさないと、他のデータ源と突合できない。
        """
        if not self.sec_code:
            return None
        c = self.sec_code.strip()
        if len(c) == 5 and c.endswith("0"):
            c = c[:4]
        return c + ".T"


def _get_json(path: str, params: dict) -> dict:
    """API を叩く。**キーが無ければ落ちる。**"""
    key = CR.require("EDINET_API_KEY")
    q = dict(params)
    q["Subscription-Key"] = key
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise EdinetError("HTTP %d" % e.code)
    # **EDINET は HTTP 200 で中身にエラーを返す。**
    # ここを見ないと、認証失敗が「書類 0 件」に化ける。
    # 実測（2026-08-23、キー無しで叩いた）:
    #   HTTP 200 / {"StatusCode": 401, "message": "invalid subscription key"}
    meta = data.get("metadata", {})
    sc = meta.get("status") or data.get("StatusCode")
    if sc and str(sc) not in ("200",):
        raise EdinetError("API が %s を返した: %s"
                          % (sc, data.get("message")
                             or meta.get("message") or ""))
    return data


def documents(date: str, only_wanted: bool = True) -> list[Document]:
    """**その日に提出された書類の一覧。**

    `date` は取引日でなくてよい（土日は 0 件が返る）。
    **提出日で引くので、構造的に PIT。**
    """
    data = _get_json("/documents.json", {"date": date, "type": 2})
    out = []
    for r in data.get("results", []) or []:
        t = r.get("docTypeCode")
        if only_wanted and t not in WANTED:
            continue
        out.append(Document(
            doc_id=r.get("docID") or "",
            edinet_code=r.get("edinetCode"),
            sec_code=r.get("secCode"),
            filer=r.get("filerName") or "",
            doc_type=t or "",
            doc_description=r.get("docDescription") or "",
            submit_datetime=r.get("submitDateTime") or "",
            period_end=r.get("periodEnd"),
            has_pdf=bool(r.get("pdfFlag") == "1"),
            has_csv=bool(r.get("csvFlag") == "1"),
        ))
    return out


def fetch_document(doc_id: str, fmt: str = "csv") -> bytes:
    """書類の実体を取る。

    `fmt`: `csv`（type=5、**XBRL を CSV 化したもの。最も扱いやすい**）
           / `pdf`（type=2） / `xbrl`（type=1）

    **CSV を既定にする。** XBRL を自前で解析するより、
    EDINET が用意した CSV の方が誤りが入りにくい。
    """
    # **引数の検証を先に行う。** 認証情報より前。
    # 逆にすると「fmt が間違っている」ことを知るのに .env が要ることになり、
    # **登録前に使い方の誤りを見つけられない。**（自己テストが検出した）
    t = {"xbrl": 1, "pdf": 2, "csv": 5}.get(fmt)
    if t is None:
        raise ValueError("fmt は csv / pdf / xbrl のいずれか")
    key = CR.require("EDINET_API_KEY")
    url = "%s/documents/%s?type=%d&Subscription-Key=%s" % (BASE, doc_id, t, key)
    try:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise EdinetError("書類の取得に失敗（HTTP %d）: %s" % (e.code, doc_id))


def business_days_back(n: int, today: str | None = None) -> list[str]:
    """直近 n 日ぶんの日付。**土日を除く**（提出は平日のみ）。"""
    d = dt.date.fromisoformat((today or dt.date.today().isoformat())[:10])
    out = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []
    ran = []

    def check(nm, cond):

        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/edinet.py 自己テスト（ネットワーク非依存の部分）")
    print("-" * 80)

    d = Document(doc_id="S100XXXX", edinet_code="E01234", sec_code="72030",
                 filer="トヨタ自動車株式会社", doc_type="120",
                 doc_description="有価証券報告書", submit_datetime="2025-06-25 09:00",
                 period_end="2025-03-31", has_pdf=True, has_csv=True)
    check("**証券コード5桁を4桁+.T に直す**", d.ticker() == "7203.T")
    check("**末尾が0でなければ落とさない**",
          dataclasses.replace(d, sec_code="12345").ticker() == "12345.T")
    check("証券コードが無ければ None",
          dataclasses.replace(d, sec_code=None).ticker() is None)
    check("**提出日時を持つ（これが available_at）**",
          d.submit_datetime.startswith("2025-06-25"))

    check("**必要な書類種別だけを取る**", "120" in WANTED and "180" not in WANTED)
    check("大量保有報告書を含む（M11）", "350" in WANTED and "360" in WANTED)
    check("有報と四半期報告書を含む", {"120", "140"} <= WANTED)

    bd = business_days_back(5, "2026-08-23")   # 日曜
    check("**土日を除く**", all(dt.date.fromisoformat(x).weekday() < 5 for x in bd))
    check("新しい順に返る", bd == sorted(bd, reverse=True))
    check("指定した日数だけ返る", len(bd) == 5)

    try:
        fetch_document("X", fmt="doc")
        check("**未知の形式を拒否する**", False)
    except ValueError:
        check("**未知の形式を拒否する**", True)
    except CR.MissingCredential:
        check("**未知の形式を拒否する**", False)

    check("**API エラーはデータ不在と別の型**",
          issubclass(EdinetError, RuntimeError)
          and not issubclass(EdinetError, KeyError))

    if not CR.get("EDINET_API_KEY"):
        try:
            documents("2025-01-06")
            check("**キーが無ければ落ちる（0件で続行しない）**", False)
        except CR.MissingCredential:
            check("**キーが無ければ落ちる（0件で続行しない）**", True)
    else:
        check("（キーがあるのでこの検査は省略）", True)

    print("-" * 80)
    declared = 13
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


def _probe() -> int:
    print(CR.status())
    print()
    try:
        # 平日を選ぶ
        for d in business_days_back(6):
            docs = documents(d)
            print("%s: **%d 件**（263AT が使う種別のみ）" % (d, len(docs)))
            if docs:
                for x in docs[:5]:
                    print("   %-10s %-6s %-28s %s"
                          % (x.ticker() or "-", x.doc_type,
                             x.filer[:28], x.submit_datetime))
                by = {}
                for x in docs:
                    by[DOC_TYPES.get(x.doc_type, x.doc_type)] = \
                        by.get(DOC_TYPES.get(x.doc_type, x.doc_type), 0) + 1
                print("   内訳: %s" % by)
                print("   **CSV が取れる書類: %d 件**"
                      % sum(1 for x in docs if x.has_csv))
                break
    except CR.MissingCredential as e:
        print("**まだ登録されていない。**")
        print(e)
        return 1
    except EdinetError as e:
        print("**API がエラーを返した。** キーを確認する。")
        print(e)
        return 1
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_probe() if "--probe" in sys.argv else _test())
