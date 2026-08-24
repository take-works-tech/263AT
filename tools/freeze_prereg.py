#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
事前登録を凍結する。**測る前に書いたことを、後から書き換えられなくする。**

事前登録は「測る前に予測を書く」という約束だが、
**約束だけでは守れない。** 結果を見た後に「そういうつもりだった」と
書き足せてしまう。

内容のハッシュを記録し、**変わっていたら知らせる。**
変更を禁じるのではなく、**変更があったことを隠せなくする。**

    python tools/freeze_prereg.py --freeze    最初に1回（記録する）
    python tools/freeze_prereg.py             照合する（pre-commit で走る）
"""
from __future__ import annotations
import hashlib, json, pathlib, sys, datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]
def _paths():
    """**複数の事前登録を扱う。** --doc で指定、既定は第1回。"""
    d = "docs/07_preregistration.md"
    if "--doc" in sys.argv:
        d = sys.argv[sys.argv.index("--doc") + 1]
    doc = ROOT / d
    return doc, doc.with_suffix(".lock.json")


DOC, LOCK = _paths()

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def sha() -> str:
    return hashlib.sha256(DOC.read_bytes()).hexdigest()


def main() -> int:
    if not DOC.exists():
        print("事前登録の文書が無い: %s" % DOC)
        return 1
    cur = sha()
    if "--freeze" in sys.argv:
        hist = []
        if LOCK.exists():
            hist = json.loads(LOCK.read_text(encoding="utf-8")).get("history", [])
        hist.append({"sha256": cur,
                     "frozen_at": datetime.datetime.now().isoformat(timespec="seconds")})
        LOCK.write_text(json.dumps({"history": hist}, indent=1), encoding="utf-8")
        print("凍結した: %s" % cur[:16])
        if len(hist) > 1:
            print("  **これは %d 回目の凍結である。**" % len(hist))
            print("  **測定の後に書き換えたなら、それは事前登録ではない。**")
        return 0
    if not LOCK.exists():
        print("**まだ凍結されていない。** --freeze を先に実行する")
        return 1
    hist = json.loads(LOCK.read_text(encoding="utf-8"))["history"]
    if cur == hist[-1]["sha256"]:
        print("事前登録は凍結時のまま（%s、%s に凍結）"
              % (cur[:16], hist[-1]["frozen_at"]))
        return 0
    print("=" * 70)
    print("**事前登録が書き換えられている。**")
    print("  凍結時 %s（%s）" % (hist[-1]["sha256"][:16], hist[-1]["frozen_at"]))
    print("  現在   %s" % cur[:16])
    print("  変更してよいが、**変更したことを記録に残すこと**（--freeze）。")
    print("  **測定の後の変更は、事前登録の意味を失わせる。**")
    print("=" * 70)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
