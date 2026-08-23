#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
ウォークフォワードで縮小推定を回す。

**この道具の核心は、訓練窓に何を入れてよいかの判定である。**

見落としやすい罠
----------------
「訓練窓 = 生成日 T より前の観測」だけでは**足りない。**

    観測の日付が T-30日、将来リターンの期間が90日
      → その観測のリターンが確定するのは T+60日
      → **T の時点ではまだ分からない**

**日付が過去でも、ラベルが未来なら使ってはいけない。**
データ時点 PIT（filed <= t）も生成時点 PIT（設計が T 以前）も守った上で、
**さらにこの3つ目の条件が要る。**

    観測日 + 将来リターンの期間 <= T

これを `usable_at()` として実装し、テストで守る。

使い方
    .venv/Scripts/python.exe tools/run_shrink_wf.py
    .venv/Scripts/python.exe tools/run_shrink_wf.py --horizon-days 90 --cost-bps 30
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

import shrink as SH        # noqa: E402

def params_in(rows: list[dict]) -> list[str]:
    """**パネルに実際に入っている本数を使う。**

    以前はここに10本を直書きしていた。**実装を増やしてもこの一覧を
    直し忘れると、新しい本が黙って無視される**（重みが付かないのではなく、
    そもそも候補に入らない）。パネルから読むようにして直書きをやめた。
    """
    seen: dict[str, int] = {}
    for r in rows:
        for k in r["z"]:
            seen[k] = seen.get(k, 0) + 1
    return sorted(seen, key=lambda k: (-seen[k], k))


def load_panel(horizon_days: int, branch: str = "") -> list[dict]:
    d = ROOT / "data" / "panel"
    if branch:
        d = d / branch
    rows = []
    for f in sorted(d.glob("*_h%d.json" % horizon_days)):
        rows += json.loads(f.read_text(encoding="utf-8"))
    return rows


def usable_at(row: dict, T: str, horizon_days: int) -> bool:
    """**その観測を時点 T の訓練に使ってよいか。**

    条件は2つ。
    1. 観測日が T より前（生成時点 PIT）
    2. **将来リターンが T までに確定している**（`観測日 + H <= T`）

    2つ目を落とすと、**日付は過去なのにラベルが未来**という
    最も気づきにくいルックアヘッドが入る。
    """
    if row["date"] >= T:
        return False
    resolved = (dt.date.fromisoformat(row["date"])
                + dt.timedelta(days=horizon_days)).isoformat()
    return resolved <= T


def survivorship(dates: list[str]) -> list[tuple[str, int, int, float]]:
    """**その時点に存在した企業のうち、今もデータがある割合。**

    銘柄一覧を `company_tickers.json`（**今日のスナップショット**）から
    作っているため、過去に遡るほど**「今日まで生き残った企業」だけ**を
    見ることになる。

    DERA の `sub.txt` は提出日で区切られているので、
    **その時点に実際に存在した企業の数**が分かる。
    今日の登録簿と突き合わせれば、消えた企業の割合が測れる。

    実測（2026-08-23）:
        2013年に年次報告を出した 7,130 社のうち、
        **今も登録があるのは 2,500 社（35.1%）**

    → **2013年を対象にした検証は、生き残った 35% だけを見ている。**
      上場廃止の理由は倒産・業績不振が多いので、**成績は上振れする。**
      買収による廃止（勝ち組）も混じるので一方向ではないが、
      Bessembinder (2018) の通り分布は極端に歪んでおり、
      **打ち消し合うと考える根拠は無い。**

    無料データでは直せない。**yfinance は上場廃止銘柄の価格を返さない。**
    直せない以上、**測って出し続ける**のが唯一の正しい扱いである。
    """
    import json
    try:
        import pandas as pd
    except Exception:
        return []
    d = ROOT / "data" / "pit" / "subs"
    f = ROOT / "data" / "listing" / "company_tickers.json"
    if not d.exists() or not f.exists():
        return []
    df = pd.concat([pd.read_parquet(x) for x in sorted(d.glob("*.parquet"))],
                   ignore_index=True)
    df = df[df.form.isin({"10-K", "20-F", "40-F"})]
    df["yr"] = df["filed"].astype(str).str[:4]
    j = json.loads(f.read_text(encoding="utf-8"))
    alive = {int(v["cik_str"]) for v in j.values()}
    out = []
    for y in sorted({t[:4] for t in dates}):
        s = set(df[df.yr == y].cik)
        if not s:
            continue
        k = len(s & alive)
        out.append((y, len(s), k, k / len(s)))
    return out


def spread_ic(rows: list[dict], f: SH.Fit, q: float = 0.2,
              top_n: int = 20) -> dict:
    """推定した重みで、**上位分位と下位分位のリターン差**を測る。

    IC（順位相関）ではなく分位差にするのは、
    **実際に上位だけを買うから。** 全体の相関が高くても
    上位が儲からないなら意味がない。
    """
    scored = []
    for r in rows:
        s = SH.score(r["z"], f)
        if s is not None and r.get("fwd") is not None:
            scored.append((s, r["fwd"]))
    if len(scored) < 50:
        return {"n": len(scored)}
    scored.sort(key=lambda x: -x[0])
    k = max(1, int(len(scored) * q))
    top = sum(x[1] for x in scored[:k]) / k
    bot = sum(x[1] for x in scored[-k:]) / k
    allm = sum(x[1] for x in scored) / len(scored)
    return {"n": len(scored), "top": top, "bottom": bot,
            "spread": top - bot, "mean": allm, "k": k,
            # **263AT はロングオンリーである。**
            # 買うのは上位分位だけで、下位分位は**一度も持たない。**
            # それなのに評価指標を「上位 − 下位」にしていた。
            # これはロングショートの指標であって、この設計のものではない。
            #
            # 実害があった（2026-08-23）。2020-10-31 の差が -206.9% に
            # なったのは、**下位分位にサブペニー株が入って +250% に化けた**
            # からで、**上位分位は何も悪くない。**
            # 買わない銘柄の値動きで、買う銘柄の評価が壊れていた。
            #
            # 正しい問いは「上位分位は、何もしないより儲かるか」である。
            # → **上位 − 全体平均**（等加重で全部買った場合との差）
            "excess": top - allm,
            # **実際に持つ銘柄数で測る。**
            # 上位20% は 600銘柄の断面なら 120銘柄になるが、
            # **3M円で最小ポジション2%なら、持てるのは数十銘柄**である。
            # 120銘柄の平均は「その分位が良いか」を測るが、
            # **十数銘柄に集中したときに何が起きるか**は測らない。
            # 集中するほど分散が効かず、上位の1〜2銘柄で結果が決まる。
            # **最終的な利益はこちらで決まる。**
            "top_n": (sum(x[1] for x in scored[:top_n]) / top_n
                      - allm) if len(scored) >= top_n else None,
            "n_top": top_n}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon-days", type=int, default=90)
    ap.add_argument("--panel", default="",
                    help="data/panel 配下の枝（対照条件を測るため）")
    ap.add_argument("--min-train-obs", type=int, default=1000)
    ap.add_argument("--top-n", type=int, default=20,
                    help="**実際に持つ銘柄数。** 上位分位ではなくこの本数で測る")
    ap.add_argument("--cost-bps", type=float, default=30.0,
                    help="片道コスト。**リバランスのたびに両側で払う**")
    args = ap.parse_args()

    panel = load_panel(args.horizon_days, args.panel)
    PARAMS = params_in(panel) if panel else []
    if not panel:
        print("パネルが無い。tools/build_panel.py を先に実行する。")
        return 1
    dates = sorted({r["date"] for r in panel})
    print("=" * 80)
    print("ウォークフォワード縮小推定（将来リターン %d 日）" % args.horizon_days)
    print("=" * 80)
    print("パネル %d 行 / %d 日付（%s 〜 %s）"
          % (len(panel), len(dates), dates[0], dates[-1]))
    print()
    print("**訓練に使えるのは「日付が T より前」かつ")
    print("  「将来リターンが T までに確定している」観測だけ。**")
    print("  日付が過去でもラベルが未来なら使えない。")
    print()

    print("-" * 80)
    print("%-12s %8s %8s %6s %7s %9s %9s %9s"
          % ("生成日 T", "訓練obs", "検証obs", "λ", "実効本数",
             "上位20%", "下位20%", "差"))
    print("-" * 80)

    results = []
    for T in dates:
        train = [r for r in panel if usable_at(r, T, args.horizon_days)]
        if len(train) < args.min_train_obs:
            print("%-12s %8d  （訓練が %d 未満なので生成しない）"
                  % (T, len(train), args.min_train_obs))
            continue
        f = SH.fit(train, PARAMS)
        # **検証は T 当日の断面。** 訓練には一切入っていない
        test = [r for r in panel if r["date"] == T]
        st = spread_ic(test, f, top_n=args.top_n)
        results.append((T, f, st))
        if "spread" in st:
            print("%-12s %8d %8d %6g %7.1f %+8.2f%% %+8.2f%% %+8.2f%%"
                  % (T, len(train), st["n"], f.lam, f.effective_breadth,
                     100 * st["top"], 100 * st["bottom"], 100 * st["spread"]))
        else:
            print("%-12s %8d %8d  （検証の観測が少なすぎる）"
                  % (T, len(train), st.get("n", 0)))

    if not results:
        print()
        print("**1回も生成できなかった。** 訓練の観測が足りない。")
        print("パネルの期間を延ばすか --min-train-obs を下げる。")
        return 0

    # --- まとめ ---------------------------------------------------------------
    # **主指標はロングオンリーの超過（上位 − 全体平均）。**
    sp = [st["excess"] for _, _, st in results if "excess" in st]
    ls = [st["spread"] for _, _, st in results if "spread" in st]
    print()
    print("-" * 80)
    print("まとめ")
    print("-" * 80)
    if sp:
        n_pos = sum(1 for x in sp if x > 0)
        mean = sum(sp) / len(sp)
        print("  生成回数              %d" % len(sp))
        print("  **上位20%% − 全体平均   %+.2f%%**（%d日の保有あたり）"
              % (100 * mean, args.horizon_days))
        print("  **正だった回数         %d / %d**" % (n_pos, len(sp)))
        tn = [st["top_n"] for _, _, st in results if st.get("top_n") is not None]
        if tn:
            tm = sum(tn) / len(tn)
            n_pos_n = sum(1 for x in tn if x > 0)
            print("  **上位%d銘柄 − 全体平均 %+.2f%%**（%d日）  正 %d / %d"
                  % (args.top_n, 100 * tm, args.horizon_days,
                     n_pos_n, len(tn)))
            print("     ← **実際に持つ集中度。最終的な利益はこちらで決まる**")
        if ls:
            lm = sum(ls) / len(ls)
            print("  （参考）上位−下位      %+.2f%%  ← **ロングショートの指標。"
                  "この設計では買わない銘柄が入る**" % (100 * lm))
        # 回転コスト。**上位分位を入れ替えるたびに両側で払う**
        turns_per_year = 365.0 / args.horizon_days
        gross = mean * turns_per_year
        cost = 2 * (args.cost_bps / 10000.0) * turns_per_year
        print()
        print("  **年率の粗超過        %+.2f%%**" % (100 * gross))
        print("  年率の取引コスト      %.2f%%（片道 %.0fbps × 往復 × 年%.1f回）"
              % (100 * cost, args.cost_bps, turns_per_year))
        print("  **年率の正味超過      %+.2f%%**" % (100 * (gross - cost)))
        print()
        if gross - cost <= 0:
            print("  → **正味がマイナス。** この構成では手数料負けする。")

        # --- **実効サンプル数の警告。これが無いと数字を読み違える** -----------
        #
        # 重なり合う将来リターンは独立ではない。
        # 250日の期間を月次でずらすと、**隣接する観測は 92% が同じ期間**を見ている。
        # 見かけの観測数を実効数と取り違えると、
        # **偶然の数字を発見と誤認する。**
        span_days = ((dt.date.fromisoformat(dates[-1])
                      - dt.date.fromisoformat(dates[0])).days) or 1
        n_indep = max(1.0, span_days / args.horizon_days)
        print()
        print("  " + "!" * 60)
        print("  **実効サンプル数の警告**")
        print("  " + "!" * 60)
        print("  生成回数 %d に対し、**重なりを除いた独立な期間は %.1f 個しかない。**"
              % (len(sp), n_indep))
        print("  （観測期間 %d 日 ÷ 将来リターン %d 日）"
              % (span_days, args.horizon_days))
        if n_indep < 5:
            print("  → **%.1f 個の独立観測から結論を出してはいけない。**" % n_indep)
            print("     上の年率 %+.2f%% は、**偶然と区別できない。**"
                  % (100 * (gross - cost)))
            print("     符号すら信用できない。")
        # t 値の目安（独立数で計算する。見かけの数では過大になる）
        import statistics as _st
        if len(sp) >= 2:
            sd = _st.pstdev(sp) or 1e-12
            t_naive = mean / (sd / (len(sp) ** 0.5))
            t_indep = mean / (sd / (n_indep ** 0.5))
            print("  参考: t 値 = %.2f（見かけの %d 個）→ **%.2f（独立 %.1f 個）**"
                  % (t_naive, len(sp), t_indep, n_indep))
            print("     Harvey-Liu-Zhu は **t > 3.0** を要求する（§0）。")
        # --- **生存者バイアスの警告。これが無いと成績を読み違える** ----------
        sv = survivorship(dates)
        if sv:
            print()
            print("  " + "!" * 60)
            print("  **生存者バイアスの警告**")
            print("  " + "!" * 60)
            print("  銘柄一覧を**今日のスナップショット**から作っているため、")
            print("  過去ほど「今日まで生き残った企業」だけを見ている。")
            print("  %-6s %10s %10s %8s" % ("年", "当時の社数", "今も登録", "残存率"))
            worst = 1.0
            for y, n_then, n_now, rate in sv:
                mark = " **" if rate < 0.6 else ""
                print("  %-6s %10d %10d %7.1f%%%s"
                      % (y, n_then, n_now, 100 * rate, mark))
                worst = min(worst, rate)
            print("  → **最も薄い年で残存率 %.1f%%。**" % (100 * worst))
            print("     上場廃止は倒産・業績不振が多いので、**成績は上振れする。**")
            print("     **無料データでは直せない**"
                  "（yfinance は廃止銘柄の価格を返さない）。")
            print("     直せない以上、**測って出し続ける**しかない。")

        print("  **これは検証ではない**（docs/05 §1.3）。")
        print("  パラメータの選定と符号を 2024年までのデータを見て決めているため、")
        print("  2025年以降の期間でもカタログ設計の漏れが残る。")

    # 重みの推移
    print()
    print("-" * 80)
    print("重みの推移（最後の生成日の値と、全期間で 0 でなかった回数）")
    print("-" * 80)
    last = results[-1][1]
    cnt = {p: sum(1 for _, f, _ in results if f.weights.get(p, 0) > 1e-9)
           for p in PARAMS}
    for p in sorted(PARAMS, key=lambda x: -last.weights.get(x, 0)):
        print("  %-5s 最終 %8.4f   0でなかった回数 %d / %d"
              % (p, last.weights.get(p, 0.0), cnt[p], len(results)))
    print()
    print("  実効本数の平均 %.1f / %d"
          % (sum(f.effective_breadth for _, f, _ in results) / len(results),
             len(PARAMS)))
    print("  → **等加重（実効 %d）でも1本集中（実効 1）でもない中間。**"
          % len(PARAMS))
    print("     OQ-24 で「非負 ridge は soft selection として働く」と")
    print("     書いたことの、自分のデータでの再現。")
    for n in last.notes:
        print("  注: %s" % n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
