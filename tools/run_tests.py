#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""src/ の自己テストをまとめて走らせる。pre-commit から呼ぶ。

**src/ のモジュールは外部依存を持たない**（標準ライブラリのみ）。
guard.py が「LLM に生成させない安全装置」であるのと同じ理由で、
bars/universe/normalize も**壊れたら静かに損をする層**なので、
依存を最小にして自己テストを常に走らせる。
"""
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULES = ["guard.py", "bars.py", "universe.py", "normalize.py", "listing.py", "ff49.py", "facts.py", "periods.py", "prices.py", "params_us.py", "params_px.py", "factors.py", "params_fx.py", "pipeline.py",
           "sell.py", "sizing.py", "backtest.py", "portfolio.py", "shrink.py",
           "credentials.py", "jquants.py", "edinet.py"]

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main() -> int:
    bad = []
    for m in MODULES:
        p = ROOT / "src" / m
        if not p.exists():
            print("  %-16s **見つからない**" % m)
            bad.append(m)
            continue
        r = subprocess.run([sys.executable, str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        tail = [l for l in (r.stdout or "").splitlines()
                if re.match(r"^\s*\d+/\d+ ", l)]
        print("  %-16s %s  %s" % (m, "OK" if r.returncode == 0 else "**FAIL**",
                                  tail[-1] if tail else ""))
        if r.returncode != 0:
            bad.append(m)
            print((r.stdout or "")[-1500:])
            print((r.stderr or "")[-800:])
    print("-" * 50)
    print("%d/%d モジュールが通過" % (len(MODULES) - len(bad), len(MODULES)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
