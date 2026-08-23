#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
認証情報の読み込み。**docs/04_security.md §2 の規約をコードで守る。**

規約
----
| 規約 | 実装 |
|---|---|
| **認証情報をコードに書かない** | `.env` からのみ読む |
| **`.env` を git に入れない** | `.gitignore` 済み + `tools/security_check.py` が検査 |
| **ログに出さない** | `mask()` を通す。生の値を返す口は1つだけ |
| **無い場合は落とす。空文字で続行しない** | `require()` が例外を投げる |

**なぜ空文字で続行させないか。**
空のキーで API を叩くと 401 が返り、
呼び出し側が「データが無い」と解釈して**静かに欠損として扱う。**
**認証の失敗とデータの不在は別物**なので、区別できる形で落とす。

`.env` の書き方
---------------
    JQUANTS_MAILADDRESS=you@example.com
    JQUANTS_PASSWORD=xxxxxxxx
    EDINET_API_KEY=xxxxxxxx

**このファイルは絶対に git に入れない。**
`tools/security_check.py` が pre-commit で検査している。

自己テスト
    python src/credentials.py
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV = ROOT / ".env"


class MissingCredential(RuntimeError):
    """**認証情報が無いときに投げる。**

    空文字を返して続行すると、認証の失敗がデータの不在に化ける。
    """


def load_env(path: pathlib.Path | None = None) -> dict[str, str]:
    """`.env` を読む。**環境変数を上書きしない**（環境側を優先）。

    形式は `KEY=VALUE` の1行1件。`#` で始まる行と空行は無視。
    **値のクォートは剥がす**（コピペで `"..."` が付くことがある）。
    """
    f = path or ENV
    out: dict[str, str] = {}
    if not f.exists():
        return out
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def get(name: str, path: pathlib.Path | None = None) -> str | None:
    """環境変数 → `.env` の順で探す。**無ければ None。**"""
    v = os.environ.get(name)
    if v:
        return v
    v = load_env(path).get(name)
    return v or None


def require(name: str, path: pathlib.Path | None = None) -> str:
    """**無ければ例外。** 何をすればよいかを例外文に書く。"""
    v = get(name, path)
    if not v:
        raise MissingCredential(
            "%s が設定されていない。\n"
            "  1. プロジェクト直下に .env を作る\n"
            "  2. %s=値 の行を足す\n"
            "  3. **.env は git に入れない**（.gitignore 済み）\n"
            "  取得方法は docs/06_accounts.md を参照" % (name, name))
    why = problem(name, v)
    if why:
        # **値はあるが本物でない。** ここで落とさないと 401 になり、
        # 呼び出し側が「データが無い」と解釈する — 未設定より質が悪い
        raise MissingCredential(
            "%s の値が成立していない: %s" % (name, why)
            + "\n  .env の該当行を、実際に発行された値に書き換える"
            + "\n  手順は docs/06_accounts.md")
    # **return を忘れていて None を返していた**（2026-08-23、自己テストが検出）。
    # このモジュールは「認証の失敗がデータの不在に化けるのを防ぐ」ために
    # 書いたのに、**実装がまさにその失敗を起こしていた。**
    # None を API キーとして渡すと 401 になり、呼び出し側が
    # 「データが無い」と解釈する — 防ぎたかった経路そのもの。
    return v


# docs/06_accounts.md がテンプレートとして載せている文言。
# **これをそのまま .env に書き写す事故が実際に起きた。**
PLACEHOLDERS = {
    "登録したメールアドレス", "登録したパスワード", "発行されたキー",
    "you@example.com", "xxxxxxxx", "値", "ここにキー",
}


def problem(name: str, value: str) -> str | None:
    """**値が本物らしいか。** 問題があれば理由を返す。無ければ None。

    なぜ要るか。
    `.env` に説明文をそのまま書き写しても、`get()` は「値がある」と答える。
    **`require()` も通ってしまい、API が 401 を返し、
    呼び出し側がそれを「データが無い」と解釈する。**
    このモジュールが防ぐと宣言した経路に、**別の入口から入られる。**

    実際に起きた（2026-08-23）:
      JQUANTS_PASSWORD=登録したパスワード
      EDINET_API_KEY=発行されたキー
    に対して status() が **「すべて揃っている」と答えた。**

    形の検査は最小限にする。**本物かどうかは通信するまで分からない**ので、
    ここで弾くのは「明らかに本物でないもの」だけに留める。
    """
    if value in PLACEHOLDERS:
        return "**説明文のまま**（docs/06_accounts.md の例をそのまま書いている）"
    if any(ord(c) > 127 for c in value):
        return "**日本語が入っている**（認証情報は通常 ASCII）"
    if value != value.strip() or " " in value:
        return "空白が入っている"
    if name.endswith("MAILADDRESS"):
        if "@" not in value or "." not in value.split("@")[-1]:
            return "**メールアドレスの形になっていない**（@ が無い）"
    return None


def mask(secret: str | None) -> str:
    """**ログに出してよい形。** 先頭2文字だけ残す。

    `guard.mask_secrets()` と同じ思想。
    **短い秘密は全部隠す**（先頭2文字で推測できてしまうため）。
    """
    if not secret:
        return "（未設定）"
    if len(secret) <= 6:
        return "*" * len(secret)
    return secret[:2] + "*" * (len(secret) - 2)


def status(path: pathlib.Path | None = None) -> str:
    """**何が揃っていて何が足りないか**を人が読む形で返す。"""
    keys = [
        ("JQUANTS_MAILADDRESS", "J-Quants の登録メールアドレス"),
        ("JQUANTS_PASSWORD", "J-Quants のパスワード"),
        ("EDINET_API_KEY", "EDINET API v2 のサブスクリプションキー"),
    ]
    lines = [".env: %s" % ("あり" if (path or ENV).exists() else "**未作成**")]
    missing, bad = [], []
    for k, desc in keys:
        v = get(k, path)
        why = problem(k, v) if v else None
        if not v:
            missing.append(k)
        elif why:
            bad.append(k)
        lines.append("  %-22s %-12s %s" % (k, mask(v), desc))
        if why:
            lines.append("  %-22s → %s" % ("", why))
    if bad:
        # **「未設定」より強く言う。** 設定済みに見えるぶん気づきにくい
        lines.append("  → **%d 件が値として成立していない。**" % len(bad))
        lines.append("     **この状態で API を叩くと 401 になり、"
                     "「データが無い」ように見える。**")
    if missing:
        lines.append("  → **%d 件が未設定。** docs/06_accounts.md の手順を参照"
                     % len(missing))
    if not bad and not missing:
        lines.append("  → すべて揃っている")
    return "\n".join(lines)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    import tempfile
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-64s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/credentials.py 自己テスト")
    print("-" * 80)

    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / ".env"
        p.write_text(
            "# コメント\n"
            "\n"
            "JQUANTS_MAILADDRESS=user@example.com\n"
            'JQUANTS_PASSWORD="pw12345678"\n'
            "EMPTY=\n"
            "SPACED =  value  \n", encoding="utf-8")

        e = load_env(p)
        check("KEY=VALUE を読む", e["JQUANTS_MAILADDRESS"] == "user@example.com")
        check("**クォートを剥がす**", e["JQUANTS_PASSWORD"] == "pw12345678")
        check("コメントと空行を無視する", "#" not in "".join(e))
        check("前後の空白を落とす", e["SPACED"] == "value")
        check("空の値は空文字", e["EMPTY"] == "")

        check("get が読める", get("JQUANTS_MAILADDRESS", p) == "user@example.com")
        check("**空の値は None 扱い**", get("EMPTY", p) is None)
        check("無いキーは None", get("NOPE", p) is None)

        try:
            require("NOPE", p)
            check("**無いキーで require したら例外**", False)
        except MissingCredential as ex:
            check("**無いキーで require したら例外**", True)
            check("**例外文に何をすればよいか書く**", ".env" in str(ex))
            check("例外文にキー名が入る", "NOPE" in str(ex))

        check("あるキーなら値を返す",
              require("JQUANTS_MAILADDRESS", p) == "user@example.com")

        # マスク
        check("**長い秘密は先頭2文字だけ残す**", mask("abcdefghij") == "ab********")
        check("**短い秘密は全部隠す**", mask("abc123") == "******")
        check("未設定は明示する", mask(None) == "（未設定）")
        check("**マスクした値から元が復元できない**",
              "cdefghij" not in mask("abcdefghij"))

        # --- 値が成立しているかの検査 -------------------------------------
        # **説明文をそのまま .env に書き写す事故が実際に起きた**ので、
        # ここは実例そのままを検査に入れる
        check("**説明文のままなら弾く（パスワード）**",
              problem("JQUANTS_PASSWORD", "登録したパスワード") is not None)
        check("**説明文のままなら弾く（キー）**",
              problem("EDINET_API_KEY", "発行されたキー") is not None)
        check("**@ の無い値はメールアドレスとして弾く**",
              problem("JQUANTS_MAILADDRESS", "abcdefghij") is not None)
        check("**日本語が入っていたら弾く**",
              problem("EDINET_API_KEY", "キー") is not None)
        check("空白が入っていたら弾く",
              problem("EDINET_API_KEY", "abc def") is not None)
        check("本物らしい値は通す",
              problem("EDINET_API_KEY", "a1b2c3d4e5f6") is None)
        check("本物らしいメールアドレスは通す",
              problem("JQUANTS_MAILADDRESS", "user@example.com") is None)

        # **require もここで落とす。** get は通るが require は通さない
        pp = pathlib.Path(d) / "ph.env"
        pp.write_text("EDINET_API_KEY=発行されたキー" + chr(10), encoding="utf-8")
        check("get は値があると答える", get("EDINET_API_KEY", pp) is not None)
        try:
            require("EDINET_API_KEY", pp)
            check("**説明文のままなら require が落ちる**", False)
        except MissingCredential as ex:
            check("**説明文のままなら require が落ちる**", True)
            check("何が問題か例外文に書く", "説明文" in str(ex))

        st2 = status(pp)
        check("**status が「揃っている」と言わない**",
              "すべて揃っている" not in st2)
        check("**401 に化けることを警告する**", "401" in st2)

        st = status(p)
        check("状態が読める", "JQUANTS_MAILADDRESS" in st)
        check("**足りないものを明示する**", "未設定" in st)
        check("**status に生の秘密が出ない**", "pw12345678" not in st)

        # 環境変数が .env より優先
        os.environ["JQUANTS_MAILADDRESS"] = "env@example.com"
        try:
            check("**環境変数が .env より優先される**",
                  get("JQUANTS_MAILADDRESS", p) == "env@example.com")
        finally:
            del os.environ["JQUANTS_MAILADDRESS"]

    # .env が無い場合
    with tempfile.TemporaryDirectory() as d:
        p2 = pathlib.Path(d) / "nope.env"
        check("**.env が無くても落ちない（空を返す）**", load_env(p2) == {})
        check("その場合 status は未作成と言う", "未作成" in status(p2))

    print("-" * 80)
    total = 33
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--status" in sys.argv:
        print(status())
        raise SystemExit(0)
    raise SystemExit(_test())
