#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
価格データの取得アダプタ。**データ源ごとの癖をここに閉じ込める。**

`src/bars.py` は「生の価格 + 分割係数」という理想的な入力を前提にしている。
**現実のデータ源はその形で来ない。** 変換の責任はアダプタ側にある。

yfinance の癖（実測、docs/03 DF-02）
-----------------------------------
**`auto_adjust=False` を指定しても、分割については既に調整済みの価格を返す。**
未調整なのは配当だけ。ドキュメントに明記が無い。

  6758.T 2024-09-27（5分割）: Close が 2848 → 2861 と5分の1に落ちていない

`Stock Splits` 列の値をそのまま `Bar.split_factor` に渡すと**二重調整**になり、
分割日に `+161% = ln(5)` の偽のリターンが立つ。
**モメンタム（G）は偽の急騰を買い、リバーサル（H）は偽の急落を買う。**

→ **このアダプタは `split_factor=1.0` を固定する。**
   分割イベントは別に記録し、`bars.detect_split_misadjustment()` で毎回検査する。

**新しいデータ源を足すときは、必ず `verify()` を通してから使う。**

自己テスト
    python src/prices.py
    python src/prices.py --fetch AAPL MSFT     # 実際に取得して検査する
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "prices"

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import bars as B  # type: ignore  # noqa: E402


@dataclasses.dataclass
class Series:
    """1銘柄の価格系列と、取得元の癖の記録。"""

    ticker: str
    bars: list[B.Bar]
    splits: list[tuple[str, float]]     # (日付, 比率)。**調整には使わない**
    source: str
    source_pre_adjusted: bool           # 取得元が既に分割調整済みか
    note: str = ""


def from_yfinance(tickers: list[str], period: str = "3y",
                  batch: int = 60) -> dict[str, Series]:
    """yfinance からまとめて取る。

    **`auto_adjust=False` は「配当を調整しない」という意味であって、
    「分割も調整しない」ではない**（DF-02）。
    したがって `split_factor` は 1.0 に固定する。
    """
    import yfinance as yf

    out: dict[str, Series] = {}
    for i in range(0, len(tickers), batch):
        chunk = tickers[i:i + batch]
        try:
            df = yf.download(chunk, period=period, auto_adjust=False,
                             actions=True, progress=False, threads=True,
                             group_by="ticker")
        except Exception as e:
            print("  取得失敗 %s: %s" % (chunk[:3], str(e)[:70]))
            continue
        if df is None or df.empty:
            continue
        for t in chunk:
            try:
                sub = df[t] if len(chunk) > 1 else df
            except (KeyError, TypeError):
                continue
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            bs, sp = [], []
            for idx, r in sub.iterrows():
                sf = float(r.get("Stock Splits") or 0.0)
                if sf > 0:
                    sp.append((str(idx.date()), sf))
                bs.append(B.Bar(
                    date=str(idx.date()),
                    open=float(r["Open"]), high=float(r["High"]),
                    low=float(r["Low"]), close=float(r["Close"]),
                    volume=float(r["Volume"] or 0.0),
                    split_factor=1.0,        # ← **固定。DF-02**
                    dividend=float(r.get("Dividends") or 0.0),
                ))
            out[t] = Series(ticker=t, bars=bs, splits=sp, source="yfinance",
                            source_pre_adjusted=True,
                            note="auto_adjust=False でも分割は調整済み（DF-02）")
    return out


def verify(s: Series) -> list[str]:
    """**取得元の癖の仮定が正しいかを、毎回検査する。**

    `source_pre_adjusted=True` と宣言したなら、
    **分割係数を再適用したときに二重調整が検出されるはず**である。
    検出されなければ、仮定が間違っているか分割が無いかのどちらか。
    """
    problems = []
    rows = B.adjust(s.bars)

    # 1) 異常なリターンが**分割の近くで**起きていないか。
    #
    # **実測（2026-08-23）で閾値を作り直した。**
    # 最初は「|日次リターン| > 60% なら調整漏れを疑う」としたが、
    # 1,383銘柄で **309銘柄（22%）が引っかかった。**
    # 調べると AREB / ASBP / ATEK のような**極小型株の本物の値動き**で、
    # 分割日とは無関係だった（0.065 → 1.1 が1日で起きる世界）。
    #
    # → **「大きく動いた」ではなく「分割の近くで大きく動いた」を見る。**
    #   調整漏れなら必ず分割日の前後に出る。
    # **「分割の近く」だけでは足りなかった**（2026-08-23、2度目の較正）。
    # ±3日で見ると 146/1383 が残ったが、調べると
    # **株式併合は暴落に伴って起きる**（上場維持基準に抵触したから併合する）ので、
    # 「近い」だけでは本物の暴落と区別できない。
    #
    # → **変動幅が ln(分割比率) と一致するか**で判定する。
    #   調整漏れなら必ず ±ln(f) ちょうどになる。それが唯一の識別子。
    import datetime as _dt
    import math as _m
    r = B.log_return(rows)
    for i, x in enumerate(r):
        if x is None or abs(x) < 0.35 or i == 0:
            continue
        d = _dt.date.fromisoformat(rows[i]["date"])
        for sd, f in s.splits:
            if f <= 0 or abs((d - _dt.date.fromisoformat(sd)).days) > 3:
                continue
            if abs(abs(x) - abs(_m.log(f))) < 0.15:
                problems.append(
                    "%s に |日次リターン| %.0f%% = ln(%.4g)。"
                    "**分割比率とちょうど一致するので調整漏れ**"
                    % (rows[i]["date"], 100 * abs(x), f))
                break

    # 2) 宣言と実態が合っているか
    if s.splits:
        probe = [dataclasses.replace(b, split_factor=dict(s.splits).get(b.date, 1.0))
                 for b in s.bars]
        mis = B.detect_split_misadjustment(B.adjust(probe), probe)
        if s.source_pre_adjusted and not mis:
            problems.append(
                "**分割調整済みと宣言したが、再適用しても壊れない。**"
                " 実は未調整の可能性がある")
        if not s.source_pre_adjusted and mis:
            problems.append(
                "**未調整と宣言したが、再適用すると壊れる。** 実は調整済み")

    # 3) 配当込みが価格リターンを下回っていないか
    tr = B.total_return(rows)
    pr_sum = sum(x for x in r if x is not None)
    tr_sum = sum(x for x in tr if x is not None)
    if tr_sum < pr_sum - 1e-9:
        problems.append("配当込みリターンが価格リターンを下回っている")

    return problems


def reverse_splits(s: Series, t: str, years: float = 2.0) -> int:
    """時点 t までの `years` 年間の**株式併合の回数**。

    **実データで気づいた**（2026-08-23）。AREB は18ヶ月で5回併合していた
    （1:9 → 1:25 → 1:20 → 1:20 → 1:100）。
    これは希薄化の連鎖そのもので、**F18（株式併合）の検証で
    「上場維持基準対策のことが多く悪材料」と書いた現象の極端な実例。**

    F18 は「実施の有無」のフラグだったが、
    **回数の方が情報量が大きい。** 1回なら整理、5回なら死の螺旋。
    → F18 の定義を「有無」から「直近N年の回数」に見直す材料になる。
    """
    import datetime as _dt
    tt = _dt.date.fromisoformat(t[:10])
    lo = tt - _dt.timedelta(days=int(365.25 * years))
    return sum(1 for d, f in s.splits
               if f < 1.0 and lo <= _dt.date.fromisoformat(d) <= tt)


def unadjust_factor(s: Series, t: str) -> float:
    """**時点 t より後の分割の累積比率。**

    yfinance が返す価格は**遡及調整済み**なので、
    調整後価格にこれを掛けると**その時点で実際に付いていた株価**になる。

    なぜ要るか（2026-08-24、実データで踏んだ）
    -------------------------------------------
    NVDA の 2015-12-31 の実際の終値は **$32.94** だが、
    2021年の4分割と2024年の10分割が遡及適用され、
    データ上は **$0.82** になっている。

    **これを「当時の株価」として使うと、深刻なルックアヘッドになる。**

        遡及調整後の株価が低い
          ⟺ その後に大きく分割した
          ⟺ **その後に大きく上がった**

    分割は株価が大きく上がった後に行われるので、
    **「過去の調整後株価が低いこと」は未来のリターンそのものである。**

    実際にこれを踏んで、次の2つが汚染されていた。
      - **最低株価ゲート**（$1未満を除外 → **将来の勝ち馬を系統的に除外していた**）
      - **J25（株価水準）** のパラメータ
      - 株価帯別の裾の測定（$1-2 の成績が良く見えたのは**分割した勝ち馬が入っていた**から）

    **リターンの計算には調整後が正しい。** 使い分けが要る。
    """
    f = 1.0
    for d, r in s.splits:
        if d > t and r > 0:
            f *= r
    return f


def price_at(s: Series, t: str) -> float | None:
    """**その時点で実際に付いていた株価。** 調整後ではない。

    ゲートや「株価水準」に使うのはこちら。
    **リターンの計算に使ってはいけない**（分割の段差が入る）。
    """
    rows = [x for x in B.adjust(s.bars) if x["date"] <= t]
    if not rows:
        return None
    return rows[-1]["close"] * unadjust_factor(s, t)


def snapshot(s: Series, t: str) -> dict | None:
    """時点 t までの情報だけで、ユニバース判定に要る量を作る。

    **`t` より後のバーは一切見ない。** ここが甘いと J01/J10 に未来が入る。
    """
    rows = [x for x in B.adjust(s.bars) if x["date"] <= t]
    if len(rows) < 60:
        return None
    return {
        "ticker": s.ticker,
        "date": rows[-1]["date"],
        "close": rows[-1]["close"],
        "adv20": B.adv(rows, 20)[-1],
        "zero_vol_60": B.zero_volume_days(rows, 60)[-1],
        "n_bars": len(rows),
        "reverse_splits_2y": reverse_splits(s, t, 2.0),
    }


def save(series: dict[str, Series]) -> None:
    """キャッシュ。**data/ 配下なので git には入らない**（.gitignore）。"""
    CACHE.mkdir(parents=True, exist_ok=True)
    for t, s in series.items():
        safe = t.replace("/", "_").replace("\\", "_")
        (CACHE / (safe + ".json")).write_text(json.dumps({
            "ticker": s.ticker, "source": s.source,
            "source_pre_adjusted": s.source_pre_adjusted,
            "splits": s.splits, "note": s.note,
            "bars": [dataclasses.asdict(b) for b in s.bars],
        }), encoding="utf-8")


def load(tickers: list[str]) -> dict[str, Series]:
    out = {}
    for t in tickers:
        f = CACHE / (t.replace("/", "_").replace("\\", "_") + ".json")
        if not f.exists():
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        # **Bar が知らないキーは捨てる。** clean_price_glitches.py が
        # 清掃の印（glitch）を足すので、素通しの ** 展開だと落ちる
        fields = {fl.name for fl in dataclasses.fields(B.Bar)}
        out[t] = Series(
            ticker=d["ticker"],
            bars=[B.Bar(**{k: v for k, v in b.items() if k in fields})
                  for b in d["bars"]],
            splits=[tuple(x) for x in d["splits"]], source=d["source"],
            source_pre_adjusted=d["source_pre_adjusted"], note=d.get("note", ""))
    return out


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []
    ran = []

    def check(nm, cond):

        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-62s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/prices.py 自己テスト")
    print("-" * 76)

    def mk(closes, splits=(), pre=True, vols=None):
        bs = [B.Bar(date="2024-01-%02d" % (i + 1), open=c, high=c, low=c, close=c,
                    volume=(vols[i] if vols else 1000.0), split_factor=1.0)
              for i, c in enumerate(closes)]
        return Series("T", bs, list(splits), "test", pre)

    # 調整済みを正しく宣言している場合
    s = mk([100.0, 101.0, 102.0], splits=[("2024-01-03", 5.0)], pre=True)
    check("**調整済みと宣言し、実際に調整済みなら問題なし**", verify(s) == [])

    # 未調整と誤って宣言した場合
    s2 = mk([100.0, 101.0, 102.0], splits=[("2024-01-03", 5.0)], pre=False)
    p = verify(s2)
    check("**未調整と宣言したのに調整済みなら検出する**",
          any("実は調整済み" in x for x in p))

    # 生価格（本当に未調整）を調整済みと誤って宣言した場合
    s3 = mk([500.0, 505.0, 102.0], splits=[("2024-01-03", 5.0)], pre=True)
    p3 = verify(s3)
    check("**調整済みと宣言したのに未調整なら検出する**",
          any("再適用しても壊れない" in x for x in p3))

    # 異常なリターン。**分割の近くかどうかで扱いを変える**
    s4 = mk([100.0, 101.0, 500.0], pre=True)
    check("**分割と無関係な急騰は警告しない（極小型株の本物の値動き）**",
          verify(s4) == [])
    s4b = mk([100.0, 101.0, 500.0], splits=[("2024-01-03", 5.0)], pre=True)
    check("**分割日の近くの急騰は調整漏れとして警告する**",
          any("調整漏れ" in x for x in verify(s4b)))

    # 株式併合の回数
    s7 = Series("T", [B.Bar(date="2024-06-01", open=1, high=1, low=1, close=1,
                            volume=1.0)], [], "test", True)
    s7.splits = [("2023-01-05", 0.1), ("2023-06-05", 0.05),
                 ("2024-01-05", 0.04), ("2019-01-05", 0.1)]
    check("**株式併合（比率<1）の回数を数える**",
          reverse_splits(s7, "2024-06-30", 2.0) == 3)
    check("期間外の併合は数えない", reverse_splits(s7, "2024-06-30", 1.0) == 1)
    s8 = Series("T", s7.bars, [("2024-01-05", 5.0)], "test", True)
    check("**通常の分割（比率>1）は数えない**", reverse_splits(s8, "2024-06-30") == 0)

    def dated(n, vols=None):
        """n 本の日次バーを 2024-01-01 から連番で作る（月28日制の簡易カレンダー）。"""
        s = mk([100.0] * n, pre=True, vols=vols)
        s.bars = [dataclasses.replace(
            b, date="2024-%02d-%02d" % (1 + i // 28, 1 + i % 28))
            for i, b in enumerate(s.bars)]
        return s

    # snapshot が未来を見ないこと。**t の後にもバーがある状態で切る**
    s5 = dated(140)                       # 5ヶ月分
    snap = snapshot(s5, "2024-03-28")     # 3ヶ月目で切る = 84本
    check("**snapshot は t より後のバーを見ない**",
          snap is not None and snap["date"] <= "2024-03-28")
    check("**t 以前のバーだけを使う（140本のうち84本）**",
          snap is not None and snap["n_bars"] == 84)
    check("バーが60本未満なら None", snapshot(dated(40), "2024-12-31") is None)
    check("J01 の入力（平均売買代金）が出る", snap is not None and snap["adv20"] is not None)
    check("J10 の入力（出来高ゼロ日数）が出る",
          snap is not None and snap["zero_vol_60"] == 0)

    # 出来高ゼロを数える
    v = [1000.0] * 100
    v[70] = v[75] = 0.0                   # 直近60日に入る位置
    sn6 = snapshot(dated(100, vols=v), "2024-04-28")
    check("**出来高ゼロ日を数える（J10 のゲート）**",
          sn6 is not None and sn6["zero_vol_60"] == 2)

    check("**アダプタは split_factor を 1.0 に固定する（DF-02）**",
          all(b.split_factor == 1.0 for b in s.bars))

    print("-" * 76)
    declared = 15
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", nargs="*", help="取得して検査する銘柄")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if args.fetch is None:
        return _test()
    ss = from_yfinance(args.fetch or ["AAPL", "MSFT", "6758.T"])
    for t, s in ss.items():
        p = verify(s)
        print("%-10s バー%4d 分割%d  %s"
              % (t, len(s.bars), len(s.splits), "OK" if not p else "**問題あり**"))
        for x in p:
            print("    ! %s" % x)
    save(ss)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
