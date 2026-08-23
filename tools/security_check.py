#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
コミット前の秘密情報スキャン。docs/04_security.md §2.3 の実装。

検出するもの
  - API キーらしき文字列（既知プレフィックス / 高エントロピーの長い英数字）
  - .env などの秘密ファイルが追跡対象に入っていないか
  - 保有銘柄・資産額を含みうるファイル（data/ forward_log/）の混入
  - ローカル絶対パス（ユーザー名が漏れる）
  - メールアドレス（規約上必要な User-Agent は許可リストで除外）

使い方
    python tools/security_check.py            # 作業ツリー全体
    python tools/security_check.py --staged   # git add 済みのものだけ（pre-commit 用）

pre-commit に仕込む:
    printf '#!/bin/sh\\npython tools/security_check.py --staged || exit 1\\n' > .git/hooks/pre-commit
    chmod +x .git/hooks/pre-commit
"""
from __future__ import annotations

import argparse
import math
import pathlib
import re
import subprocess
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 既知のトークン形式。増やすほど良い
KEY_PATTERNS = [
    (r"gh[pousr]_[A-Za-z0-9]{20,}", "GitHub token"),
    (r"sk-[A-Za-z0-9_\-]{20,}", "OpenAI 形式のキー"),
    (r"sk-ant-[A-Za-z0-9_\-]{20,}", "Anthropic キー"),
    (r"AKIA[0-9A-Z]{16}", "AWS アクセスキー"),
    (r"AIza[0-9A-Za-z_\-]{35}", "Google API キー"),
    (r"xox[baprs]-[A-Za-z0-9\-]{10,}", "Slack トークン"),
    (r"PK[A-Z0-9]{18}", "Alpaca キーID の形式"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "秘密鍵"),
    (r"(?i)\b(password|passwd|passphrase|secret|api[_-]?key|access[_-]?token)\b\s*[:=]\s*['\"][^'\"]{8,}['\"]",
     "コード内に直書きされた資格情報"),
]

# 追跡してはいけないパス
FORBIDDEN_PATHS = [
    (re.compile(r"(^|/)\.env(\.|$)"), ".env は追跡しない"),
    (re.compile(r"^data/(?!\.gitkeep)"), "data/ は保有・実データを含みうる"),
    (re.compile(r"^forward_log/.*\.jsonl$"), "forward_log の実データは追跡しない"),
    (re.compile(r"(^|/)credentials?\.json$"), "認証情報ファイル"),
    (re.compile(r"\.(pem|key|p12|pfx)$"), "鍵ファイル"),
    (re.compile(r"(^|/)kill\.flag$"), "kill.flag はローカル運用状態"),
]

# 走査対象外（バイナリ・生成物）
SKIP_SUFFIX = {".parquet", ".zip", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".xls", ".xlsx",
               ".pyc", ".whl", ".gz", ".7z", ".ico"}
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules", "data", "forward_log"}

# 許可（規約上必要 / 本人の連絡先として意図的に置いているもの）
ALLOW_SUBSTRINGS = [
    "tzero30208@gmail.com",          # SEC EDGAR の User-Agent 要件
    "noreply@anthropic.com",
]

# RFC 2606 / 6761 が「実在しえない」と定めたドメイン。**個別のアドレスを
# 列挙するのではなく、ドメインの側で許可する。** 列挙にすると、文書や
# テストを書き足すたびに検査器を触ることになり、**そのうち面倒になって
# 検査器の方を緩める。** 実在しえないものだけを、恒久的に除外する。
RESERVED_MAIL_DOMAINS = re.compile(
    r"@(example\.(com|org|net)|[\w.-]*\.(invalid|test|example|localhost))$", re.I)

ENTROPY_MIN_LEN = 32
ENTROPY_THRESHOLD = 4.3


def shannon(s: str) -> float:
    if not s:
        return 0.0
    return -sum((s.count(c) / len(s)) * math.log2(s.count(c) / len(s)) for c in set(s))


def staged_files():
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
                         cwd=ROOT, capture_output=True, text=True)
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def tracked_files():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    return [l.strip() for l in out.stdout.splitlines() if l.strip()]


def url_spans(text):
    """URL の範囲を返す。URL 断片は高エントロピー検出の誤検出源なので除外する。"""
    return [(m.start(), m.end()) for m in re.finditer(r"(?:https?://|www\.)[^\s)\"'<>]+", text)]


def in_spans(pos, spans):
    return any(a <= pos < b for a, b in spans)


def scan_text(rel, text, findings):
    urls = url_spans(text)
    for pat, label in KEY_PATTERNS:
        for m in re.finditer(pat, text):
            frag = m.group(0)
            if any(a in frag for a in ALLOW_SUBSTRINGS):
                continue
            line = text[:m.start()].count("\n") + 1
            findings.append(("KEY", rel, line, "%s: %s…" % (label, frag[:18])))

    # ローカル絶対パス（ユーザー名が漏れる）
    for m in re.finditer(r"[A-Za-z]:\\+Users\\+([^\\\s\"']+)", text):
        line = text[:m.start()].count("\n") + 1
        findings.append(("PATH", rel, line, "ローカル絶対パス（ユーザー名 %s）" % m.group(1)))

    # メールアドレス
    for m in re.finditer(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
        if (in_spans(m.start(), urls)
                or any(a in m.group(0) for a in ALLOW_SUBSTRINGS)
                or RESERVED_MAIL_DOMAINS.search(m.group(0))):
            continue
        line = text[:m.start()].count("\n") + 1
        findings.append(("MAIL", rel, line, "メールアドレス %s" % m.group(0)))

    # 高エントロピー文字列
    for m in re.finditer(r"[A-Za-z0-9+/=_\-]{%d,}" % ENTROPY_MIN_LEN, text):
        tok = m.group(0)
        if in_spans(m.start(), urls):
            continue
        if any(a in tok for a in ALLOW_SUBSTRINGS):
            continue
        # 16進のハッシュや URL の一部は誤検出しやすいので除外
        if re.fullmatch(r"[0-9a-f]+", tok) or re.fullmatch(r"[0-9]+", tok):
            continue
        # CamelCase の識別子（XBRL タグ名・関数名など）は誤検出源。数字も記号も無いものは除外
        if re.fullmatch(r"[A-Za-z]+", tok):
            continue
        if shannon(tok) >= ENTROPY_THRESHOLD:
            line = text[:m.start()].count("\n") + 1
            findings.append(("ENTROPY", rel, line,
                             "高エントロピー文字列（%.1f bit）: %s…" % (shannon(tok), tok[:16])))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--staged", action="store_true", help="git add 済みのものだけ検査")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    files = staged_files() if a.staged else tracked_files()
    findings, blockers = [], []

    for rel in files:
        for pat, why in FORBIDDEN_PATHS:
            if pat.search(rel):
                blockers.append(("FORBIDDEN", rel, 0, why))

    for rel in files:
        p = ROOT / rel
        if not p.exists() or p.is_dir():
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        if any(part in SKIP_DIRS for part in pathlib.PurePath(rel).parts[:-1]):
            continue
        if p.stat().st_size > 3_000_000:
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        scan_text(rel, text, findings)

    print("=" * 70)
    print("263AT セキュリティ検査  対象 %d ファイル（%s）"
          % (len(files), "staged" if a.staged else "tracked"))
    print("=" * 70)

    # .env の実在と権限
    env = ROOT / ".env"
    if env.exists():
        tracked = ".env" in tracked_files()
        print("  .env: 存在する / git 追跡=%s" % ("**されている（危険）**" if tracked else "されていない"))
        if tracked:
            blockers.append(("FORBIDDEN", ".env", 0, ".env が git に追跡されている"))
    else:
        print("  .env: 未作成（認証情報をまだ持っていない状態）")

    kill = ROOT / "kill.flag"
    print("  kill.flag: %s" % ("**存在する → 全処理は停止状態**" if kill.exists() else "なし（通常運転）"))

    if blockers:
        print("\n■ ブロッカー（コミットしてはいけない）")
        for kind, rel, line, why in blockers:
            print("  x [%s] %s — %s" % (kind, rel, why))
    if findings:
        print("\n■ 要確認 %d 件" % len(findings))
        by = {}
        for kind, rel, line, why in findings:
            by.setdefault(kind, []).append((rel, line, why))
        for kind in ("KEY", "ENTROPY", "PATH", "MAIL"):
            v = by.get(kind, [])
            if not v:
                continue
            print("  [%s] %d 件" % (kind, len(v)))
            for rel, line, why in v[:8]:
                print("     %s:%d  %s" % (rel, line, why))
            if len(v) > 8:
                print("     ... 他 %d 件" % (len(v) - 8))

    hard = [f for f in findings if f[0] == "KEY"] + blockers
    if hard:
        print("\n結果: **NG** — 上記を解消してからコミットすること")
        return 1
    if findings:
        print("\n結果: 警告あり（KEY 以外）。内容を確認してよければコミット可")
        return 0
    print("\n結果: OK — 秘密情報らしきものは見つからなかった")
    return 0


if __name__ == "__main__":
    sys.exit(main())
