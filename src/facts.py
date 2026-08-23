#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
PIT（point-in-time）ファクトの as-of 参照。**263AT で最も重要な1つの関数がここにある。**

docs/02_definition_spec.md §1.1.1 で決めた唯一の PIT ルール:

    value_as_of(code, period, t) = { filed <= t を満たす行のうち filed が最大 }.value

**素直に `groupby([code, ddate]).last()` と書くと訂正後データを使ってしまう。**
実測では AAPL の `NetIncomeLoss` 2009年6月期に 1,229 と 1,828（差 48.7%）の
2つの値があり、後者は後から訂正されたもの。
**訂正後を使うとバックテストが静かに良くなる。**

なぜ「最も新しい filed」であって「最初の filed」ではないか
--------------------------------------------------------
v0.1 では「最初の filed を取る」と書いていた。**それは誤りで、実測で訂正した。**
時点 t の投資家は、**t までに出た訂正を見ている。**
「最初の filed」は t で入手できた情報を捨てることになるので、
**保守的だが正しくない**（過度に保守的な仮定は、別の形のバイアスになる）。

ただし **`filed <= t` の境界は当日を含む**か否かで結果が変わる。
263AT は **含む**（当日開示は当日中に使える）。
ただし §1.5 の約定規約により**発注は翌営業日の始値**なので、
実質的なラグは1営業日ある。**ここを二重に引かないこと。**

自己テスト
    python src/facts.py
    python src/facts.py --demo    # 実データで訂正の実例を出す
"""
from __future__ import annotations

import argparse
import bisect
import dataclasses
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
PIT = ROOT / "data" / "pit"


@dataclasses.dataclass(frozen=True)
class Fact:
    """1つの (企業, 勘定コード, 期間) に対する1回の提出。

    **同じ (cik, code, ddate, qtrs) が複数回提出される。** それが訂正である。
    """

    cik: int
    code: str            # 正規化勘定コード（spec §2 の REV / NI / TA …）
    ddate: str           # 期間の末日（ISO）
    qtrs: int            # 0=時点値(B/S) 1=四半期 2=半期 4=通期
    value: float
    filed: str           # **提出日。PIT の判定はこれだけで行う**
    form: str
    tag: str             # 元の XBRL タグ（曖昧さの追跡用）


class AsOf:
    """as-of 参照。**`filed <= t` を満たす最後の提出だけを返す。**

    実装は単純だが、**単純であることが要件**である。
    ここに条件が増えるほどルックアヘッドの余地が増える。
    """

    def __init__(self, facts: list[Fact]):
        # (cik, code, ddate, qtrs) ごとに filed 昇順で並べる
        self._idx: dict[tuple, tuple[list[str], list[Fact]]] = {}
        buckets: dict[tuple, list[Fact]] = {}
        for f in facts:
            buckets.setdefault((f.cik, f.code, f.ddate, f.qtrs), []).append(f)
        for k, v in buckets.items():
            v.sort(key=lambda f: f.filed)
            self._idx[k] = ([f.filed for f in v], v)

    def get(self, cik: int, code: str, ddate: str, qtrs: int, t: str) -> Fact | None:
        """時点 t で入手できた値。**無ければ None。0 で埋めない。**"""
        e = self._idx.get((cik, code, ddate, qtrs))
        if not e:
            return None
        filed, rows = e
        i = bisect.bisect_right(filed, t)      # filed <= t の最後
        return rows[i - 1] if i else None

    def latest_period(self, cik: int, code: str, qtrs: int, t: str,
                      max_lag_days: int = 400) -> Fact | None:
        """時点 t で入手できている**最新の期間**の値。

        「直近の売上」のような参照はこれを使う。
        **`max_lag_days` より古い期間しか無ければ None を返す** —
        2年前の決算を「直近」として使うと、
        **上場廃止直前の企業が生きているように見える。**
        """
        best: Fact | None = None
        for (c, cd, ddate, q), (filed, rows) in self._idx.items():
            if c != cik or cd != code or q != qtrs:
                continue
            i = bisect.bisect_right(filed, t)
            if not i:
                continue
            f = rows[i - 1]
            if best is None or f.ddate > best.ddate:
                best = f
        if best is None:
            return None
        if _days(best.ddate, t) > max_lag_days:
            return None
        return best

    def restatements(self, min_rel_diff: float = 0.01) -> list[dict]:
        """**値が変わった (企業, コード, 期間) を列挙する。**

        訂正が「例外ではなく常態」であることを可視化するために持つ。
        Z14（PIT のテスト）で使う。
        """
        out = []
        for (cik, code, ddate, qtrs), (_, rows) in self._idx.items():
            if len(rows) < 2:
                continue
            vals = [r.value for r in rows]
            lo, hi = min(vals), max(vals)
            base = max(abs(lo), abs(hi))
            if base > 0 and (hi - lo) / base >= min_rel_diff:
                out.append({
                    "cik": cik, "code": code, "ddate": ddate, "qtrs": qtrs,
                    "n_filings": len(rows), "first": rows[0].value,
                    "last": rows[-1].value,
                    "rel_diff": (hi - lo) / base,
                })
        return out


def _days(a: str, b: str) -> int:
    import datetime as dt
    fa = dt.date.fromisoformat(a[:10])
    fb = dt.date.fromisoformat(b[:10])
    return (fb - fa).days


def resolve_tags(df):
    """**タグ別名を1つに解決する。** これを飛ばすと訂正率が嘘になる。

    実測（2026-08-23）で踏んだ罠:
    cik=34956 の同一提出 `0001654954-23-001107` に
    `NetIncomeLoss` = +29,263,804 と `ProfitLoss` = -29,263,804 が**両方入っている。**
    どちらも勘定コード `NI` に写像されるので、解決せずに as-of を取ると
    **同じ提出の中に符号が逆の2値がある**ことになり、
    「200% の訂正」として数えられてしまう。

    これを含めたまま測ると訂正率は **7.21%** に見えたが、
    解決後は**大きく下がる**（下の --demo で実測を出す）。

    規約は `tools/build_pit_fundamentals.py` の `resolve_codes()` と同じ:
    **TAG_MAP の並び順（tag_rank）が優先順位。**
    各 (cik, adsh, code, ddate, qtrs) につき最優先のタグだけを採る。
    **値の大小や欠損では選ばない**（恣意性を入れない）。
    """
    if "tag_rank" not in df.columns:
        return df                      # 古い形式。呼び出し側で警告する
    key = ["cik", "adsh", "code", "ddate", "qtrs"]
    return df.sort_values("tag_rank").drop_duplicates(subset=key, keep="first")


def load(quarters: list[str] | None = None) -> list[Fact]:
    """`data/pit/facts/*.parquet` を読む（tools/build_pit_fundamentals.py が作る）。"""
    import pandas as pd

    d = PIT / "facts"
    if not d.exists():
        return []
    files = sorted(d.glob("*.parquet"))
    if quarters:
        files = [f for f in files if f.stem in quarters]
    out: list[Fact] = []
    for f in files:
        df = pd.read_parquet(f)
        df = resolve_tags(df)
        for r in df.itertuples(index=False):
            out.append(Fact(
                cik=int(r.cik), code=str(r.code),
                ddate=str(r.ddate)[:10], qtrs=int(r.qtrs),
                value=float(r.value), filed=str(r.filed)[:10],
                form=str(r.form), tag=str(r.tag),
            ))
    return out


# ---------------------------------------------------------------- self-test
def _f(cik, code, ddate, qtrs, value, filed) -> Fact:
    return Fact(cik, code, ddate, qtrs, value, filed, "10-Q", code)


def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-60s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/facts.py 自己テスト")
    print("-" * 74)

    # AAPL の実例を模した訂正（1,229 → 1,828、差 48.7%）
    facts = [
        _f(320193, "NI", "2009-06-30", 1, 1229.0, "2009-07-22"),
        _f(320193, "NI", "2009-06-30", 1, 1828.0, "2010-01-25"),   # 訂正
    ]
    a = AsOf(facts)

    v = a.get(320193, "NI", "2009-06-30", 1, "2009-12-31")
    check("**訂正前の時点では訂正前の値を返す**", v is not None and v.value == 1229.0)
    v = a.get(320193, "NI", "2009-06-30", 1, "2010-06-30")
    check("訂正後の時点では訂正後の値", v is not None and v.value == 1828.0)
    v = a.get(320193, "NI", "2009-06-30", 1, "2009-07-22")
    check("**filed 当日は入手できる（境界は含む）**", v is not None and v.value == 1229.0)
    v = a.get(320193, "NI", "2009-06-30", 1, "2009-07-21")
    check("**提出前は None。0 で埋めない**", v is None)
    check("存在しない企業は None", a.get(1, "NI", "2009-06-30", 1, "2030-01-01") is None)

    # **これが最も重要なテスト。** 素直な実装との差を明示する
    naive = max(facts, key=lambda f: f.filed).value
    pit = a.get(320193, "NI", "2009-06-30", 1, "2009-12-31").value
    check("**素直な実装（最新 filed）と as-of の値が違う**", naive != pit)
    check("  素直=%.0f / as-of=%.0f（差 %.1f%%）"
          % (naive, pit, 100 * (naive - pit) / naive), True)

    # 訂正の検出
    rs = a.restatements()
    check("訂正を検出する", len(rs) == 1 and rs[0]["n_filings"] == 2)
    check("訂正幅を出す", abs(rs[0]["rel_diff"] - (1828 - 1229) / 1828) < 1e-9)
    check("**閾値未満の差は訂正としない**", AsOf(facts).restatements(min_rel_diff=0.6) == [])

    # latest_period
    facts2 = [
        _f(1, "REV", "2024-03-31", 1, 100.0, "2024-05-01"),
        _f(1, "REV", "2024-06-30", 1, 110.0, "2024-08-01"),
    ]
    b = AsOf(facts2)
    v = b.latest_period(1, "REV", 1, "2024-07-01")
    check("最新期間: 8月提出分はまだ見えない", v is not None and v.value == 100.0)
    v = b.latest_period(1, "REV", 1, "2024-09-01")
    check("最新期間: 提出後は新しい期間を返す", v is not None and v.value == 110.0)
    v = b.latest_period(1, "REV", 1, "2026-01-01", max_lag_days=400)
    check("**古すぎる期間しか無ければ None**（廃止直前の企業が生きて見えるのを防ぐ）",
          v is None)
    v = b.latest_period(1, "REV", 1, "2026-01-01", max_lag_days=1000)
    check("許容ラグを広げれば返る", v is not None)

    # 期間の型が混ざらないこと
    facts3 = [_f(2, "REV", "2024-03-31", 1, 10.0, "2024-05-01"),
              _f(2, "REV", "2024-03-31", 4, 40.0, "2024-05-01")]
    c = AsOf(facts3)
    check("**四半期(qtrs=1)と通期(qtrs=4)は別物として持つ**",
          c.get(2, "REV", "2024-03-31", 1, "2025-01-01").value == 10.0
          and c.get(2, "REV", "2024-03-31", 4, "2025-01-01").value == 40.0)

    # タグ別名の解決（実測で踏んだ罠、2026-08-23）
    try:
        import pandas as pd
        df = pd.DataFrame([
            # 同一提出に NetIncomeLoss(+) と ProfitLoss(-) が両方入る実例
            {"cik": 34956, "adsh": "X", "code": "NI", "ddate": "2021-09-30",
             "qtrs": 3, "tag": "NetIncomeLoss", "tag_rank": 0, "value": 29263804.0,
             "filed": "2023-01-31", "form": "10-K"},
            {"cik": 34956, "adsh": "X", "code": "NI", "ddate": "2021-09-30",
             "qtrs": 3, "tag": "ProfitLoss", "tag_rank": 2, "value": -29263804.0,
             "filed": "2023-01-31", "form": "10-K"},
        ])
        r = resolve_tags(df)
        check("**同一提出の別名タグを1つに解決する**", len(r) == 1)
        check("**優先順位が高いタグ（tag_rank 最小）を採る**",
              r.iloc[0]["tag"] == "NetIncomeLoss")
        check("解決前は符号が逆の2値がある（＝解決しないと偽の訂正になる）",
              len(df) == 2 and df["value"].min() < 0 < df["value"].max())
        check("tag_rank が無い古い形式はそのまま返す",
              len(resolve_tags(df.drop(columns=["tag_rank"]))) == 2)
    except ImportError:
        for _ in range(4):
            check("pandas が無い", False)

    print("-" * 74)
    total = 19
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


def _demo() -> int:
    """実データで訂正の実態を出す。"""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    fs = load()
    if not fs:
        print("PIT ファクトが無い。tools/build_pit_fundamentals.py を先に実行する。")
        return 1
    a = AsOf(fs)
    rs = a.restatements(min_rel_diff=0.01)
    n_keys = len(a._idx)
    print("ファクト %d 件 / (企業,コード,期間) %d 通り" % (len(fs), n_keys))
    print("**値が1%%以上変わった組み合わせ: %d 件（%.2f%%）**"
          % (len(rs), 100 * len(rs) / max(n_keys, 1)))
    if rs:
        rs.sort(key=lambda r: -r["rel_diff"])
        print()
        print("差が大きい上位10:")
        for r in rs[:10]:
            print("  cik=%-8d %-6s %s q%d  %d回提出  %.4g → %.4g（%.0f%%）"
                  % (r["cik"], r["code"], r["ddate"], r["qtrs"], r["n_filings"],
                     r["first"], r["last"], 100 * r["rel_diff"]))
    print()
    print("**タグ別名を解決していない状態では 7.21% に見えた。**")
    print("同一提出に NetIncomeLoss(+) と ProfitLoss(-) が同居しており、")
    print("どちらも NI に写像されるので「200% の訂正」として数えられていた。")
    print("→ **resolve_tags() を通した後の %.2f%% が本当の訂正率。**"
          % (100 * len(rs) / max(n_keys, 1)))
    print()
    print("残る符号反転は**本物の訂正**である。実例:")
    print("  cik=1683252 NI 2021-12-31 q2")
    print("    2023-02-21 10-Q   +160,391")
    print("    2023-05-05 10-Q/A +160,391")
    print("    2024-09-12 10-Q/A **-160,391**  ← 黒字が赤字に訂正された")
    print("  **2023年の投資家は黒字と見ていた。**")
    print("  訂正後の値で2023年をバックテストすればルックアヘッドになる。")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true")
    raise SystemExit(_demo() if ap.parse_args().demo else _test())
