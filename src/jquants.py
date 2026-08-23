#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
J-Quants API クライアント（日本株）。

**Free プランは12週間遅延する**（公式FAQ、2026-08-23 確認）。
検証には十分だが、**実運用では使えない**（12週間前の株価では発注できない）。
docs/06_accounts.md §1.1 を参照。

認証の仕組み
------------
    メール + パスワード
      → リフレッシュトークン（1週間有効）
      → ID トークン（24時間有効）
      → 各 API

**トークンはキャッシュし、期限が切れたときだけ取り直す。**
毎回取り直すとレート制限に当たるうえ、
**認証エラーとデータ不在の区別が付きにくくなる。**

この層の規約
------------
1. **認証情報は `src/credentials.py` 経由でのみ読む。** 引数で受けない
2. **エラーを握りつぶさない。** 401 と「データが無い」を区別する
3. **取得したデータの available_at を記録する。**
   12週間遅延があるので、**「今日取れた」＝「今日時点の値」ではない**
4. **トークンをログに出さない**

自己テスト
    python src/jquants.py            # ネットワーク非依存の部分
    python src/jquants.py --probe    # 実際に認証してみる（.env が要る）
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import credentials as CR  # type: ignore  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = "https://api.jquants.com/v1"
TOKEN_CACHE = ROOT / "data" / "jquants_token.json"

# **Free プランの遅延。** 公式FAQ に「12週間遅延して配信されます」とある
FREE_PLAN_DELAY_DAYS = 84


class AuthError(RuntimeError):
    """**認証の失敗。** データ不在と区別するために独立した例外にする。"""


class RateLimited(RuntimeError):
    """レート制限。**リトライしてよい**唯一のエラー。"""


@dataclasses.dataclass
class Session:
    """認証済みセッション。**トークンをそのまま持たず、期限も持つ。**"""

    id_token: str
    id_expires: str
    refresh_token: str
    refresh_expires: str

    def valid(self, now: str | None = None) -> bool:
        n = now or dt.datetime.now().isoformat()
        return n < self.id_expires

    def masked(self) -> str:
        return "id=%s（%s まで）" % (CR.mask(self.id_token), self.id_expires)


def _post(url: str, payload: dict, headers: dict | None = None) -> dict:
    body = json.dumps(payload).encode("utf-8")
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code in (400, 401, 403):
            raise AuthError("認証に失敗した（HTTP %d）: %s" % (e.code, detail))
        if e.code == 429:
            raise RateLimited("レート制限（HTTP 429）")
        raise


def _get(url: str, token: str) -> dict:
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        if e.code in (401, 403):
            # **ここを「データが無い」と解釈させない。**
            raise AuthError("認証エラー（HTTP %d）。トークンが切れているか"
                            "プランで許可されていない: %s" % (e.code, detail))
        if e.code == 429:
            raise RateLimited("レート制限（HTTP 429）")
        raise


def login(use_cache: bool = True) -> Session:
    """認証してセッションを返す。**トークンはキャッシュする。**

    `.env` に `JQUANTS_MAILADDRESS` と `JQUANTS_PASSWORD` が要る。
    **無ければ `MissingCredential` で落ちる**（空文字で続行しない）。
    """
    if use_cache and TOKEN_CACHE.exists():
        try:
            d = json.loads(TOKEN_CACHE.read_text(encoding="utf-8"))
            s = Session(**d)
            if s.valid():
                return s
        except Exception:
            pass          # 壊れたキャッシュは無視して取り直す

    mail = CR.require("JQUANTS_MAILADDRESS")
    pw = CR.require("JQUANTS_PASSWORD")

    r1 = _post(BASE + "/token/auth_user",
               {"mailaddress": mail, "password": pw})
    refresh = r1.get("refreshToken")
    if not refresh:
        raise AuthError("リフレッシュトークンが返ってこなかった: %s" % str(r1)[:120])

    r2 = _post(BASE + "/token/auth_refresh?refreshtoken=" + refresh, {})
    idt = r2.get("idToken")
    if not idt:
        raise AuthError("ID トークンが返ってこなかった: %s" % str(r2)[:120])

    now = dt.datetime.now()
    s = Session(id_token=idt,
                id_expires=(now + dt.timedelta(hours=23)).isoformat(),
                refresh_token=refresh,
                refresh_expires=(now + dt.timedelta(days=6)).isoformat())
    TOKEN_CACHE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_CACHE.write_text(json.dumps(dataclasses.asdict(s)), encoding="utf-8")
    return s


def available_through(today: str | None = None, free_plan: bool = True) -> str:
    """**Free プランで実際に取れる最新の日付。**

    「今日取れた」＝「今日時点の値」ではない。
    **12週間前までしか無い**ので、
    バックテストの終端をここより後に置いてはいけない。
    """
    t = dt.date.fromisoformat((today or dt.date.today().isoformat())[:10])
    if not free_plan:
        return t.isoformat()
    return (t - dt.timedelta(days=FREE_PLAN_DELAY_DAYS)).isoformat()


def listed_info(s: Session, date: str | None = None) -> list[dict]:
    """上場銘柄一覧。**東証33業種と17業種が付いてくる**（§4.1 で使う）。"""
    url = BASE + "/listed/info"
    if date:
        url += "?date=" + date.replace("-", "")
    return _get(url, s.id_token).get("info", [])


def daily_quotes(s: Session, code: str | None = None,
                 date: str | None = None) -> list[dict]:
    """日次の株価。**code か date のどちらかが要る。**

    **調整済み終値（AdjustmentClose）が別カラムで来る**ので、
    `bars.py` に渡す前に**どちらを使うか決める**必要がある。
    DF-02（yfinance が黙って分割調整済み）と同じ罠なので、
    **`prices.verify()` を必ず通す。**
    """
    if not code and not date:
        raise ValueError("code か date のどちらかを指定する")
    q = []
    if code:
        q.append("code=" + code)
    if date:
        q.append("date=" + date.replace("-", ""))
    return _get(BASE + "/prices/daily_quotes?" + "&".join(q), s.id_token
                ).get("daily_quotes", [])


def statements(s: Session, code: str | None = None,
               date: str | None = None) -> list[dict]:
    """財務情報。**決算短信ベースなので、有報より速い**（spec §1.2）。"""
    q = []
    if code:
        q.append("code=" + code)
    if date:
        q.append("date=" + date.replace("-", ""))
    return _get(BASE + "/fins/statements?" + "&".join(q), s.id_token
                ).get("statements", [])


def margin_interest(s: Session, code: str | None = None,
                    date: str | None = None) -> list[dict]:
    """信用取引残高。**K01-K05 の入力。米国に対応が無い日本固有のデータ。**"""
    q = []
    if code:
        q.append("code=" + code)
    if date:
        q.append("date=" + date.replace("-", ""))
    return _get(BASE + "/markets/weekly_margin_interest?" + "&".join(q),
                s.id_token).get("weekly_margin_interest", [])


def short_selling(s: Session, date: str | None = None) -> list[dict]:
    """空売り比率。**K08 の入力。**"""
    q = "?date=" + date.replace("-", "") if date else ""
    return _get(BASE + "/markets/short_selling" + q, s.id_token
                ).get("short_selling", [])


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/jquants.py 自己テスト（ネットワーク非依存の部分）")
    print("-" * 80)

    # 遅延の扱い
    check("**Free プランは12週間遅延として扱う**", FREE_PLAN_DELAY_DAYS == 84)
    check("取れる最新日は84日前",
          available_through("2026-08-23") == "2026-05-31")
    check("**有料プランなら当日**",
          available_through("2026-08-23", free_plan=False) == "2026-08-23")

    # セッション
    s = Session(id_token="abcdefghij", id_expires="2030-01-01T00:00:00",
                refresh_token="rrrrrrrrrr", refresh_expires="2030-01-01T00:00:00")
    check("期限内なら有効", s.valid("2026-01-01T00:00:00"))
    check("**期限が切れたら無効**", not s.valid("2031-01-01T00:00:00"))
    check("**マスクした表示にトークンが出ない**",
          "cdefghij" not in s.masked() and "ab" in s.masked())

    # 引数の検証
    try:
        daily_quotes(s, None, None)
        check("**code も date も無ければ拒否する**", False)
    except ValueError:
        check("**code も date も無ければ拒否する**", True)

    # 例外の型が分かれていること
    check("**認証エラーとレート制限が別の型**",
          AuthError is not RateLimited
          and issubclass(AuthError, RuntimeError))
    check("**認証エラーはデータ不在と区別される**",
          not issubclass(AuthError, KeyError))

    # 認証情報が無い状態
    if not CR.get("JQUANTS_MAILADDRESS"):
        try:
            login(use_cache=False)
            check("**認証情報が無ければ落ちる（空で続行しない）**", False)
        except CR.MissingCredential as e:
            check("**認証情報が無ければ落ちる（空で続行しない）**", True)
            check("何をすればよいか例外文に書く", "docs/06_accounts.md" in str(e))
    else:
        check("（認証情報があるのでこの検査は省略）", True)
        check("（同上）", True)

    print("-" * 80)
    total = 11
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


def _probe() -> int:
    """**実際に認証してみる。** `.env` が要る。"""
    print(CR.status())
    print()
    try:
        s = login()
    except CR.MissingCredential as e:
        print("**まだ登録されていない。**")
        print(e)
        return 1
    except AuthError as e:
        print("**認証に失敗した。** メールアドレスとパスワードを確認する。")
        print(e)
        return 1
    print("認証できた: %s" % s.masked())
    print("Free プランで取れる最新日: **%s**" % available_through())
    info = listed_info(s)
    print("上場銘柄一覧: %d 件" % len(info))
    if info:
        k = info[0]
        print("  例: %s" % {x: k.get(x) for x in list(k)[:6]})
        secs = {x.get("Sector33CodeName") for x in info if x.get("Sector33CodeName")}
        print("  **東証33業種が %d 種類付いてくる**（§4.1 の設計がそのまま動く）"
              % len(secs))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_probe() if "--probe" in sys.argv else _test())
