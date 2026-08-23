#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
J-Quants API **V2** クライアント（日本株）。

**パスワードは要らない。API キー1本だけである。**

    x-api-key: <APIキー>

キーは J-Quants のダッシュボード「設定 » API キー」で発行する。

V1 は 2026-06-01 に終了した
------------------------------
このモジュールは当初 V1（メールアドレス + パスワード → リフレッシュトークン
→ ID トークン）で書かれていたが、**その方式は既に存在しない。**
`/v1/token/auth_user` を叩くと一律 403 が返る。

    2026-08-23 実測:
      https://api.jquants.com/v1/token/auth_user          → 403 Forbidden
      https://api.jquants.com/v2/equities/bars/daily      → "api key is invalid"
      https://api.jquants.com/v2/listed/info              → "endpoint does not exist"

**「認証情報が正しくない」と「エンドポイントが無い」を API 自身が
区別して返す**ので、上の3つは別々の意味を持つ。
V1 の 403 は**キーの問題ではなく、経路そのものの消滅**である。

**Free プランで取れるものは、当初の想定より遥かに狭い**
------------------------------------------------------
| | Free | Light ¥1,650 | Standard ¥3,300 |
|---|---|---|---|
| 上場銘柄一覧 | **○** | ○ | ○ |
| 株価四本値 | **○** | ○ | ○ |
| **財務情報** | **✕** | ○ | ○ |
| 決算発表予定日 | ✕ | ○ | ○ |
| 信用取引週末残高（K01-K05） | ✕ | ✕ | ○ |
| 業種別空売り比率（K08） | ✕ | ✕ | ○ |
| **政策保有株式（K17/M23）** | ✕ | ✕ | **○** |
| **大株主状況（K35）** | ✕ | ✕ | **○** |
| **大量保有報告書（M11）** | ✕ | ✕ | **○** |
| 過去データ | **2年（直近12週を除く）** | 5年 | 10年 |

→ **Free では日本株の財務が一切取れない。** 株価と銘柄一覧だけである。
  docs/06_accounts.md に「検証には十分」と書いたのは**誤りだった。**

→ **Free は1年で自動解約される**（再登録は可能）。

**Standard は EDINET 由来のデータを含む。**
政策保有株式・大株主状況・大量保有報告書は、
**EDINET の API キーを自分で取らなくても Standard で取れる。**

この層の規約
------------
1. **認証情報は `src/credentials.py` 経由でのみ読む。** 引数で受けない
2. **エラーを握りつぶさない。** 403 と「データが無い」を区別する
3. **プランで許可されていない呼び出しは、叩く前に落とす。**
   403 を「データが無い」と解釈させないための最後の砦
4. **取得したデータの available_at を記録する。**
   12週間遅延があるので、**「今日取れた」＝「今日時点の値」ではない**

自己テスト
    python src/jquants.py            # ネットワーク非依存の部分
    python src/jquants.py --probe    # 実際に叩く（.env が要る）
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import credentials as CR  # type: ignore  # noqa: E402

BASE = "https://api.jquants.com"

# **Free プランの遅延。** 公式に「12週間遅延して配信されます」とある
FREE_PLAN_DELAY_DAYS = 84
# Free の保有期間。**直近12週を除く2年分しかない**
FREE_PLAN_HISTORY_DAYS = 365 * 2

# 実測で存在を確認したパス（2026-08-23）。
# **存在しないパスは API が "endpoint does not exist" と明言する**ので、
# 推測でパスを書かず、確認できたものだけを持つ。
EP_MASTER = "/v2/equities/master"                 # 上場銘柄一覧
EP_BARS_DAILY = "/v2/equities/bars/daily"         # 株価四本値
EP_FINS = "/v2/fins/details"                      # 財務情報
EP_EARNINGS_CAL = "/v2/equities/earnings-calendar"  # 決算発表予定日
EP_CALENDAR = "/v2/markets/calendar"              # 取引カレンダー
EP_MARGIN = "/v2/markets/margin-interest"         # 信用取引週末残高
EP_BREAKDOWN = "/v2/markets/breakdown"            # 売買内訳
EP_MAJOR_HOLDERS = "/v2/edinet/major-shareholders"    # 大株主状況
EP_CROSS_HOLDINGS = "/v2/edinet/cross-shareholdings"  # 政策保有株式

# **Free で使えるのはこの2本だけ。**
FREE_ENDPOINTS = {EP_MASTER, EP_BARS_DAILY}

# どのプランから使えるか（人に見せるため）
NEEDS_PLAN = {
    EP_FINS: "Light",
    EP_EARNINGS_CAL: "Light",
    EP_CALENDAR: "Light",
    EP_MARGIN: "Standard",
    EP_BREAKDOWN: "Standard",
    EP_MAJOR_HOLDERS: "Standard",
    EP_CROSS_HOLDINGS: "Standard",
}


class AuthError(RuntimeError):
    """**キーの問題。** データ不在と区別するために独立した例外にする。"""


class PlanError(RuntimeError):
    """**契約プランで許可されていない。**

    これを `AuthError` と分けるのは、**打つ手が違う**から。
    キーの誤りは直せるが、プランの制約は課金しないと解けない。
    """


class RateLimited(RuntimeError):
    """レート制限。**リトライしてよい**唯一のエラー。"""


PLAN_RANK = {"Free": 0, "Light": 1, "Standard": 2, "Premium": 3}


def plan() -> str:
    """契約プラン。**`.env` の `JQUANTS_PLAN`。既定は Free。**

    **未設定を Free と見なすのは意図的。**
    未設定を上位プランと見なすと、叩いて 403 を食い、
    呼び出し側がそれを「データが無い」と解釈する余地が生まれる。
    **安全側は「取れないと思っておく」方。**
    """
    v = (CR.get("JQUANTS_PLAN") or "Free").strip().capitalize()
    return v if v in PLAN_RANK else "Free"


def _get(path: str, params: dict | None = None) -> dict:
    """V2 を叩く。**キーが無ければ落ちる。**

    **契約プランで使えないエンドポイントは、通信する前に落とす。**
    叩いてしまうと 403 が返り、呼び出し側がそれを
    「データが無い」と解釈する余地が生まれる。

    最初の実装では `allow_paid` という引数で壁を作ったが、
    **呼び出し側が全部 `allow_paid=True` を渡していて、壁が無効だった。**
    自己テストが検出した。**引数で無効化できる安全装置は、無効化される。**
    """
    need = NEEDS_PLAN.get(path, "Free")
    if PLAN_RANK[plan()] < PLAN_RANK[need]:
        raise PlanError(
            "%s は %s プラン以上が必要（現在 %s）。" % (path, need, plan())
            + "\n  契約済みなら .env に JQUANTS_PLAN=%s を書く" % need
            + "\n  プランごとの範囲は docs/06_accounts.md §1.2")

    key = CR.require("JQUANTS_API_KEY")
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers={"x-api-key": key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code == 429:
            raise RateLimited("レート制限（HTTP 429）")
        if e.code in (401, 403):
            # **API はこの2つを本文で区別する。** 読まないと取り違える
            if "does not exist" in detail:
                raise AuthError("パスが存在しない（V1 の経路ではないか）: %s"
                                % path)
            if "invalid or expired" in detail:
                raise AuthError("API キーが無効か期限切れ。"
                                "ダッシュボードで再発行する")
            raise PlanError("プランで許可されていない可能性: %s / %s"
                            % (path, detail))
        raise


def available_through(today: str | None = None, free_plan: bool = True) -> str:
    """**実際に取れる最新の日付。**

    「今日取れた」＝「今日時点の値」ではない。
    Free は**12週間前までしか無い**ので、
    バックテストの終端をここより後に置いてはいけない。
    """
    t = dt.date.fromisoformat((today or dt.date.today().isoformat())[:10])
    if not free_plan:
        return t.isoformat()
    return (t - dt.timedelta(days=FREE_PLAN_DELAY_DAYS)).isoformat()


def available_from(today: str | None = None, free_plan: bool = True) -> str:
    """**Free で遡れる最も古い日付。**

    `available_through` だけ見て「2年前から取れる」と思い込むと、
    **窓の長さを取り違える。** Free の窓は
    「12週間前 〜 2年12週間前」という**両端が動く帯**である。
    """
    end = dt.date.fromisoformat(available_through(today, free_plan))
    if not free_plan:
        return "1900-01-01"
    return (end - dt.timedelta(days=FREE_PLAN_HISTORY_DAYS)).isoformat()


def _rows(d: dict) -> list[dict]:
    """応答から行の配列を取り出す。**キー名は API 側の都合なので固定しない。**"""
    for k, v in d.items():
        if isinstance(v, list):
            return v
    return []


def master(date: str | None = None) -> list[dict]:
    """上場銘柄一覧。**東証33業種と17業種が付いてくる**（spec §4.1 で使う）。

    **Free で使える。**
    """
    return _rows(_get(EP_MASTER, {"date": date}))


def bars_daily(code: str | None = None, date: str | None = None,
               frm: str | None = None, to: str | None = None) -> list[dict]:
    """日次の株価。**Free で使える。**

    **調整済みの値が別カラムで来る**ので、
    `bars.py` に渡す前に**どちらを使うか決める**必要がある。
    DF-02（yfinance が黙って分割調整済み）と同じ罠なので、
    **`prices.verify()` を必ず通す。**
    """
    if not code and not date:
        raise ValueError("code か date のどちらかを指定する")
    return _rows(_get(EP_BARS_DAILY,
                      {"code": code, "date": date, "from": frm, "to": to}))


def fins(code: str | None = None, date: str | None = None) -> list[dict]:
    """財務情報。**Light 以上。Free では取れない。**

    決算短信ベースなので有報より速い（spec §1.2）。
    """
    if not code and not date:
        raise ValueError("code か date のどちらかを指定する")
    return _rows(_get(EP_FINS, {"code": code, "date": date}))


def margin_interest(code: str | None = None,
                    date: str | None = None) -> list[dict]:
    """信用取引週末残高。**Standard 以上。K01-K05 の入力。**"""
    return _rows(_get(EP_MARGIN, {"code": code, "date": date}))


def cross_shareholdings(code: str | None = None,
                        date: str | None = None) -> list[dict]:
    """政策保有株式。**Standard 以上。K17 / M23 / A18 の入力。**

    **EDINET 由来のデータだが、J-Quants 経由で取れる。**
    EDINET の API キーを自分で取らなくてよい。
    """
    return _rows(_get(EP_CROSS_HOLDINGS, {"code": code, "date": date}))


def major_shareholders(code: str | None = None,
                       date: str | None = None) -> list[dict]:
    """大株主状況。**Standard 以上。K35 / K18 の入力。**"""
    return _rows(_get(EP_MAJOR_HOLDERS, {"code": code, "date": date}))


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []
    ran = []

    def check(nm, cond):

        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/jquants.py 自己テスト（ネットワーク非依存の部分）")
    print("-" * 80)

    # V2 であること
    check("**V2 のパスを使う（V1 は 2026-06-01 に終了）**",
          EP_BARS_DAILY.startswith("/v2/"))
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    # **require で実際に読む名前を列挙する。**
    # 文字列の有無で見ると、「参照しない」と書いた検査文自身が
    # その文字列を含むので必ず落ちる（実際に2度そうなった）。
    import re as _re
    asked = set(_re.findall(r'CR\.require\("([A-Z_]+)"\)', src))
    check("**require するのは API キーだけ**", asked == {"JQUANTS_API_KEY"})
    check("認証は API キー1本",
          "x-api-key" in src and 'CR.require("JQUANTS_API_KEY")' in src)

    # 遅延と窓
    check("**Free は12週間遅延として扱う**", FREE_PLAN_DELAY_DAYS == 84)
    check("取れる最新日は84日前",
          available_through("2026-08-23") == "2026-05-31")
    check("**有料プランなら当日**",
          available_through("2026-08-23", free_plan=False) == "2026-08-23")
    check("**Free の窓は両端が動く（最古も2年前ではない）**",
          available_from("2026-08-23") < "2024-06-01"
          and available_from("2026-08-23") > "2024-05-01")

    # プランの壁
    check("**Free で使えるのは2本だけ**", FREE_ENDPOINTS == {EP_MASTER,
                                                             EP_BARS_DAILY})
    check("**財務情報は Free に入っていない**", EP_FINS not in FREE_ENDPOINTS)
    check("政策保有株式は Standard", NEEDS_PLAN[EP_CROSS_HOLDINGS] == "Standard")

    # **叩く前に落とすこと。** 403 をデータ不在に化けさせない
    import os
    keep = os.environ.get("JQUANTS_PLAN")
    os.environ["JQUANTS_PLAN"] = "Free"
    try:
        check("**未設定・不正な値は Free に落とす（安全側）**",
              plan() == "Free")
        try:
            fins("7203")
            check("**Free では財務情報を叩く前に落とす**", False)
            check("必要なプランを例外文に書く", False)
        except PlanError as e:
            check("**Free では財務情報を叩く前に落とす**", True)
            check("必要なプランを例外文に書く", "Light" in str(e))
        except CR.MissingCredential:
            # **キーを要求する前に落ちなければならない。**
            # 順序が逆だと、キーが無い人にプランの問題が見えない
            check("**Free では財務情報を叩く前に落とす**", False)
            check("必要なプランを例外文に書く", False)
        os.environ["JQUANTS_PLAN"] = "Standard"
        # **ここでネットワークを叩かない。** 壁の判定だけを見る。
        # 最初は fins() を呼んでいたが、.env に鍵が入った途端に
        # **自己テストが実際の API を叩き始めた**（そして Free 契約なので
        # サーバ側の 403 を「壁が通らなかった」と誤判定した）。
        # **自己テストは外部に依存してはいけない。**
        check("**Standard なら壁を通す**",
              PLAN_RANK[plan()] >= PLAN_RANK[NEEDS_PLAN[EP_FINS]])
        check("**Premium は Standard の壁も通る**",
              PLAN_RANK["Premium"] >= PLAN_RANK[NEEDS_PLAN[EP_MARGIN]])
    finally:
        if keep is None:
            os.environ.pop("JQUANTS_PLAN", None)
        else:
            os.environ["JQUANTS_PLAN"] = keep

    # 引数の検証は認証より先
    try:
        bars_daily(None, None)
        check("**code も date も無ければ拒否する**", False)
    except ValueError:
        check("**code も date も無ければ拒否する**", True)
    except CR.MissingCredential:
        check("**code も date も無ければ拒否する**", False)

    # 例外の型
    check("**キーの誤りとプランの制約が別の型**", AuthError is not PlanError)
    check("**どちらもデータ不在と区別される**",
          not issubclass(AuthError, KeyError)
          and not issubclass(PlanError, KeyError))
    check("レート制限も独立", RateLimited is not AuthError)

    # 応答の取り出し
    check("行の配列を取り出す", _rows({"info": [{"a": 1}]}) == [{"a": 1}])
    check("**配列が無ければ空**（None を返さない）", _rows({"x": 1}) == [])

    # 認証情報が無い状態
    if not CR.get("JQUANTS_API_KEY"):
        try:
            master()
            check("**キーが無ければ落ちる（空で続行しない）**", False)
        except CR.MissingCredential as e:
            check("**キーが無ければ落ちる（空で続行しない）**", True)
            check("何をすればよいか例外文に書く", "docs/06_accounts.md" in str(e))
    else:
        check("（キーがあるのでこの検査は省略）", True)
        check("（同上）", True)

    print("-" * 80)
    declared = 23
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


def _probe() -> int:
    """**実際に叩いてみる。** `.env` に JQUANTS_API_KEY が要る。"""
    print(CR.status())
    print()
    try:
        info = master()
    except CR.MissingCredential as e:
        print("**まだ登録されていない。**")
        print(e)
        return 1
    except AuthError as e:
        print("**キーが通らなかった。**")
        print(e)
        return 1
    print("上場銘柄一覧: **%d 件**" % len(info))
    if info:
        k = info[0]
        print("  例のカラム: %s" % list(k)[:8])
        for f in ("Sector33CodeName", "sector33CodeName", "Sector33Code"):
            secs = {x.get(f) for x in info if x.get(f)}
            if secs:
                print("  **%s が %d 種類**（§4.1 の設計がそのまま動く）"
                      % (f, len(secs)))
                break
    print()
    print("Free で取れる窓: **%s 〜 %s**"
          % (available_from(), available_through()))
    print()
    try:
        fins("72030")
        print("財務情報: **取れた**（Light 以上を契約している）")
    except PlanError as e:
        print("財務情報: **取れない** — %s" % str(e).splitlines()[0])
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_probe() if "--probe" in sys.argv else _test())
