#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
通しの検証。**買いスコアだけでなく、売り・サイズ・コストまで含めた最終利益。**

これまで測っていたのは**買いスコアの分位差だけ**だった。
しかし最終的な利益は、**売りルール・ポジションサイズ・執行コスト**で決まる。
分位差が +5% でも、**損切りが早すぎれば全部消え、遅すぎれば裾で殺される。**

**売りは日次で判定する。**
月末だけで判定すると、損切りもトレーリングも**ほとんど発動しない。**

    買い     … 月末のみ（断面が月次だから）
    売り     … **毎営業日**（損切りは指値として置いてあるものと同じ）
    約定     … 翌営業日の始値（spec §1.5）

2026-08-29 の再監査（事前登録 第5回、docs/12）で入った変更
----------------------------------------------------------
| フラグ | 内容 | 出典 |
|---|---|---|
| （無条件） | 売り先行の二段執行・強制売りの min_trade_frac 迂回・出来高ゼロ約定の禁止 | PM-2/V6/EXE-1 |
| （無条件） | 日次の評価額と最大DD・実現後の投資比率・執行の通知の集計 | V1/V3 |
| （無条件） | 価格系列が尽きた保有（ゾンビ）の強制手仕舞いと計数 | V4 |
| --dividends | 配当を現金計上する（既定 on） | DIV-1 |
| --cost-model real | SBI実費: 手数料 min(0.495%,$22) + 銘柄別スプレッド + TAF | COST-1/2 |
| --cash-yield | 現金に短期金利（年次3M T-bill、docs/12の固定表） | PM-7 |
| --tax | 税引後も表示（譲渡益 20.315%・配当 約28.3%、損失繰越） | R-11 |
| --policy hysteresis | 参入 rank<=N / 退出 rank>2N。**継続保有は再ターゲットしない** | PM-1 |
| --entry-weight equal | 新規参入を等加重（score/vol は高ボラの裾候補を排除するため） | PM-3 |
| --min-price | 最低株価の掃引（パネルの px を使う。旧パネルでは使えない） | L7 |

使い方
    .venv/Scripts/python.exe tools/run_system.py --panel gate --horizon 250
    .venv/Scripts/python.exe tools/run_system.py --panel gate_v2 --horizon 250 \
        --cost-model real --cash-yield on --policy hysteresis
"""
from __future__ import annotations

import argparse
import collections
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

import bars as BR             # noqa: E402
import portfolio as PF        # noqa: E402
import prices as PR           # noqa: E402
import prior as PRIOR         # noqa: E402
import sell as SL             # noqa: E402
import shrink as SH           # noqa: E402
import sizing as SZ           # noqa: E402

# 現金の年利（年次の3M T-bill平均）。**事前登録 docs/12 §1 の固定表。掃引しない**
CASH_APR = {2012: 0.0005, 2013: 0.0005, 2014: 0.0005, 2015: 0.0005,
            2016: 0.003, 2017: 0.009, 2018: 0.019, 2019: 0.021,
            2020: 0.004, 2021: 0.0005, 2022: 0.020, 2023: 0.050,
            2024: 0.052, 2025: 0.044, 2026: 0.043}

# 税率（日本の個人・特定口座、米国株）。docs/12 R-11
TAX_GAIN = 0.20315            # 譲渡益
TAX_DIV = 1.0 - 0.90 * (1.0 - 0.20315)   # 米国源泉10% + 国内20.315% ≈ 28.3%


class BarStore:
    """**触った銘柄だけ**を読む。9,631銘柄を全部載せるとメモリが持たない。"""

    def __init__(self):
        self._c: dict[str, list[dict]] = {}
        self.misses = 0

    def get(self, tk: str) -> list[dict]:
        v = self._c.get(tk)
        if v is None:
            s = PR.load([tk]).get(tk)
            v = BR.adjust(s.bars) if s else []
            if not v:
                self.misses += 1
            self._c[tk] = v
        return v

    def upto(self, tk: str, date: str) -> list[dict]:
        return [x for x in self.get(tk) if x["date"] <= date]


def load_panel(branch: str, horizon: int) -> dict[str, list[dict]]:
    d = ROOT / "data" / "panel" / branch
    out: dict[str, list[dict]] = {}
    for f in sorted(d.glob("*_h%d.json" % horizon)):
        rows = json.loads(f.read_text(encoding="utf-8"))
        if rows:
            out[rows[0]["date"]] = rows
    return out


def usable_at(row: dict, T: str, horizon: int) -> bool:
    """**訓練に使ってよいか。** 日付が過去でもラベルが未来なら使えない。"""
    if row["date"] >= T or row.get("fwd") is None:
        return False
    resolved = (dt.date.fromisoformat(row["date"])
                + dt.timedelta(days=horizon)).isoformat()
    return resolved <= T


def _rand_score(tk: str, T: str) -> float:
    """スコアを使わない対照。銘柄名と日付から決まるので再現する。"""
    import hashlib
    return int(hashlib.md5((tk + T).encode()).hexdigest()[:8], 16) \
        / float(0xFFFFFFFF)


def hysteresis_targets(pf, cands, cur_w, limits, entry_weight,
                       exit_rank_mult, forced):
    """**入替ヒステリシス**（事前登録 第5回 L5/L6、PM-1）。

    - 参入: スコア順位 <= N（空き枠のぶんだけ）
    - 退出: 順位 > exit_rank_mult × N、または スコア <= 0
    - **継続保有は再ターゲットしない**（上限超過の切り下げのみ）。
      月次の再ターゲットが全コストの84%を占めていた（監査 PM-1: 入退場
      1,308/1,280回・平均保有3.2ヶ月。「250日保有」は実装されていなかった）

    Returns: (target_w, exits, n_kept, n_entered)
    """
    n_target = int(limits.max_invested / limits.min_position)
    ranked = sorted([c for c in cands if c.score > 0],
                    key=lambda c: -c.score)
    rank_of = {c.ticker: i + 1 for i, c in enumerate(ranked)}
    by_tk = {c.ticker: c for c in cands}

    held = set(pf.positions)
    keep = [tk for tk in held
            if tk not in forced
            and rank_of.get(tk, 10 ** 9) <= exit_rank_mult * n_target]
    exits = (held - set(keep)) | (forced & held)

    tw: dict[str, float] = {}
    sec_sum: dict[str | None, float] = {}
    for tk in keep:
        w = min(cur_w.get(tk, 0.0), limits.max_per_name)
        tw[tk] = w
        sec = by_tk[tk].sector if tk in by_tk else None
        sec_sum[sec] = sec_sum.get(sec, 0.0) + w

    slots = max(0, n_target - len(keep))
    budget = limits.max_invested - sum(tw.values())
    # **枠数は「予算が最小ポジション何枠ぶんあるか」で切り詰める。**
    # 本数の空きだけで1枠の大きさを決めると、継続保有が値上がりして
    # 予算が僅かに縮んだだけで per が床を割り、**参入が全滅する**
    # （実測: 予算54.1%を28枠に割ると1.93% < 2% → 参入ゼロが続き、
    # 保有が1銘柄まで痩せて投資比率6%になった。2026-08-29）
    slots = min(slots, int(budget / limits.min_position + 1e-9))
    entered = 0
    if slots and budget > limits.min_position:
        pool = [c for c in ranked if c.ticker not in held][: 3 * slots]
        # 等加重（L6）か score/vol（L5）か
        if entry_weight == "equal":
            raw = {c.ticker: 1.0 for c in pool}
        else:
            raw = {c.ticker: c.score / max(c.volatility, 0.10)
                   for c in pool if c.volatility is not None}
        chosen: dict[str, float] = {}
        for c in pool:
            if entered >= slots or budget < limits.min_position:
                break
            if c.ticker not in raw:
                continue
            # 仮の頭数で1枠のサイズを決める（等加重なら budget/残り枠）
            per = min(budget / max(slots - entered, 1),
                      limits.max_per_name)
            if per < limits.min_position - 1e-9:
                break
            if sec_sum.get(c.sector, 0.0) + per > limits.max_per_sector:
                continue          # 業種上限に当たるので次の候補へ
            chosen[c.ticker] = per
            sec_sum[c.sector] = sec_sum.get(c.sector, 0.0) + per
            budget -= per
            entered += 1
        if entry_weight != "equal" and chosen:
            # score/vol 比例に配り直す（合計は同じ、床と上限は守る）
            tot_raw = sum(raw[t] for t in chosen)
            tot_w = sum(chosen.values())
            if tot_raw > 0:
                prop = {t: tot_w * raw[t] / tot_raw for t in chosen}
                # 床割れ・上限超えは端に寄せる（1回で十分。厳密解は不要）
                for t in prop:
                    prop[t] = min(max(prop[t], limits.min_position),
                                  limits.max_per_name)
                sc = tot_w / sum(prop.values())
                chosen = {t: v * sc for t, v in prop.items()}
        tw.update(chosen)
    for tk in exits:
        tw[tk] = 0.0
    return tw, exits, len(keep), entered


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="gate")
    ap.add_argument("--horizon", type=int, default=90)
    ap.add_argument("--capital", type=float, default=3_000_000.0)
    ap.add_argument("--stop-loss", type=float, default=None)
    ap.add_argument("--trailing", type=float, default=None)
    ap.add_argument("--max-hold-years", type=float, default=None)
    ap.add_argument("--spread-bps", type=float, default=25.0)
    ap.add_argument("--min-train", type=int, default=2000)
    ap.add_argument("--min-trade-frac", type=float, default=0.005,
                    help="**この比率未満の調整はしない。** 回転率＝コストの主因")
    ap.add_argument("--all-params", action="store_true")
    ap.add_argument("--max-invested", type=float, default=0.90,
                    help="投資比率の目標かつ上限。**残りは現金**")
    ap.add_argument("--min-position", type=float, default=0.02,
                    help="最小ポジション。**銘柄数の上限を決める**（90%%÷2%%=45）")
    ap.add_argument("--max-names", type=int, default=0,
                    help="保有銘柄数の上限（0 で無制限）。**集中度の効果を測る**")
    ap.add_argument("--benchmark", default="",
                    choices=["", "random"],
                    help="**スコアを使わない対照。** random は銘柄名から決まる"
                         "疑似乱数で選ぶ（同じゲート・同じサイジング・同じコスト）")
    # ---- 事前登録 第5回で入った軸 ----------------------------------------
    ap.add_argument("--dividends", default="on", choices=["on", "off"],
                    help="配当を現金計上する（DIV-1）。off は L0/L1 の再現用")
    ap.add_argument("--cost-model", default="flat", choices=["flat", "real"],
                    help="real = SBI実費（手数料 min(0.495%%,$22) + "
                         "銘柄別スプレッド + 売りTAF）")
    ap.add_argument("--cash-yield", default="off", choices=["on", "off"],
                    help="現金に短期金利（docs/12 の固定表）")
    ap.add_argument("--tax", default="off", choices=["on", "off"],
                    help="税引後を計算する（譲渡益20.315%%・配当約28.3%%・損失繰越）。"
                         "**現金から実際に引く**ので複利にも効く")
    ap.add_argument("--policy", default="retarget",
                    choices=["retarget", "hysteresis"],
                    help="retarget = 毎月目標へ再執行（従来）。"
                         "hysteresis = 参入 rank<=N / 退出 rank>mult*N、"
                         "**継続保有は触らない**")
    ap.add_argument("--entry-weight", default="kelly",
                    choices=["kelly", "equal"],
                    help="（hysteresis のみ）新規参入の配分")
    ap.add_argument("--exit-rank-mult", type=float, default=2.0)
    ap.add_argument("--min-price", type=float, default=0.0,
                    help="最低株価の追加ゲート（現地通貨）。0 で無効。"
                         "**パネルに px が要る**（gate_v2 以降）")
    ap.add_argument("--dump", default="",
                    help="評価額の推移を書き出す（年ごとのばらつきを見るため）")
    a = ap.parse_args()

    panel = load_panel(a.panel, a.horizon)
    if not panel:
        print("パネルが無い: data/panel/%s" % a.panel)
        return 1
    dates = sorted(panel)
    flat = [r for t in dates for r in panel[t]]
    names = sorted({k for r in flat for k in r["z"]})
    if not a.all_params:
        names = [n for n in names if n in PRIOR.ADOPTED]
    if a.min_price > 0 and not any(r.get("px") for r in panel[dates[-1]]):
        print("**--min-price はこのパネルでは使えない**（px が無い。gate_v2 を使う）")
        return 1

    print("=" * 78)
    print("通しの検証（%s / 保有 %d日 / %s 〜 %s）"
          % (a.panel, a.horizon, dates[0], dates[-1]))
    print("=" * 78)
    print("**目的は成績の自慢ではない。** カタログ自体が 2024年までの OSAP を")
    print("見て書かれているので、この期間の成績は検証にならない（docs/05 §1.3）。")
    print()
    print("パネル %d 日付 / %d 行 / パラメータ %d 本" % (len(dates), len(flat), len(names)))
    print("方式: %s / 参入配分 %s / 配当 %s / コスト %s / 金利 %s / 税 %s"
          % (a.policy, a.entry_weight, a.dividends, a.cost_model,
             a.cash_yield, a.tax))
    print("売りルール: 損切り %s / トレーリング %s / 保有上限 %s"
          % (a.stop_loss, a.trailing, a.max_hold_years))
    print()

    store = BarStore()
    rules = SL.SellRules(stop_loss=a.stop_loss, trailing_stop=a.trailing,
                         max_hold_years=a.max_hold_years)
    kw = {"max_invested": a.max_invested, "min_position": a.min_position}
    if a.max_names:
        kw["max_names"] = a.max_names
    limits = SZ.RiskLimits(**kw)
    costs = PF.Costs(spread_bps=a.spread_bps, real=(a.cost_model == "real"))
    pf = PF.Portfolio(cash=a.capital)
    divmark: dict[str, str] = {}

    reasons: collections.Counter = collections.Counter()
    equity: list[tuple[str, float]] = []
    daily_equity: list[tuple[str, float]] = []
    #: (月末, 目標投資比率, 目標銘柄数, 実現投資比率, 実現銘柄数, 候補数)
    invested: list[tuple[str, float, int, float, int, int]] = []
    note_count: collections.Counter = collections.Counter()
    n_gen = 0
    zombie_exits = 0
    tax_paid = 0.0
    loss_carry = 0.0          # 損失の繰越（負の値を貯める）
    tax_year_done: set[int] = set()
    prev_T: str | None = None

    def year_tax(upto_year: int):
        """`upto_year` **より前**の年の税を精算する（--tax on のとき）。"""
        nonlocal tax_paid, loss_carry
        if a.tax != "on":
            return
        for y in sorted({int(d[:4]) for d, _ in pf.realized}
                        | {int(d[:4]) for d, _ in pf.divs}):
            if y >= upto_year or y in tax_year_done:
                continue
            g = sum(v for d, v in pf.realized if int(d[:4]) == y)
            dv = sum(v for d, v in pf.divs if int(d[:4]) == y)
            t_div = max(dv, 0.0) * TAX_DIV
            net = g + loss_carry
            if net > 0:
                t_gain = net * TAX_GAIN
                loss_carry = 0.0
            else:
                t_gain = 0.0
                loss_carry = net
            pf.cash -= (t_div + t_gain)
            tax_paid += t_div + t_gain
            tax_year_done.add(y)

    for k, T in enumerate(dates):
        train = [r for r in flat if usable_at(r, T, a.horizon)]
        if len(train) < a.min_train:
            continue
        fit = SH.fit(train, names)
        n_gen += 1

        # --- 0) 現金の金利と、年替わりの税 -------------------------------
        if a.cash_yield == "on" and prev_T is not None and pf.cash > 0:
            days = (dt.date.fromisoformat(T) - dt.date.fromisoformat(prev_T)).days
            apr = CASH_APR.get(int(T[:4]), 0.0)
            pf.cash *= (1.0 + apr) ** (days / 365.0)
        year_tax(int(T[:4]))
        prev_T = T

        # --- 0b) 配当（前回の評価日からこの日まで）------------------------
        if a.dividends == "on":
            PF.credit_dividends(pf, {t: store.get(t) for t in pf.positions},
                                T, divmark)

        # --- 0c) ゾンビ保有（価格系列が尽きた銘柄）を手仕舞う --------------
        # **V4。** 系列が2週間以上前に終わっていたら、最終値で強制手仕舞い。
        # 生存者ユニバースでは起きないはずだが、**起きたら必ず表に出す。**
        lim = (dt.date.fromisoformat(T) - dt.timedelta(days=14)).isoformat()
        for tk, pos in list(pf.positions.items()):
            b = store.get(tk)
            if b and b[-1]["date"] < lim:
                pf.cash += pos.shares * b[-1]["close"]
                pf.realized.append((T, pos.shares * (b[-1]["close"]
                                                     - pos.entry_price)))
                del pf.positions[tk]
                zombie_exits += 1

        # --- 1) 評価（**執行より先**。t+1 の約定を t の価格で見ない）------
        px = PF.mark_to_market(pf, {t: store.get(t) for t in pf.positions}, T)
        equity.append((T, pf.value(px)))

        # --- 2) 売り判定（この時点）--------------------------------------
        forced = set()
        for tk, pos in list(pf.positions.items()):
            r, _ = SL.decide(pos, SL.MarketState(), rules, T)
            if r is not SL.SellReason.HOLD:
                forced.add(tk)
                reasons[r.name] += 1

        # --- 3) スコアと候補 ----------------------------------------------
        cands = []
        for r in panel[T]:
            tk = r["ticker"]
            if tk in forced:
                continue                  # **売ると決めた銘柄は買い直さない**
            if a.min_price > 0 and (r.get("px") or 0) < a.min_price:
                continue
            s = (_rand_score(tk, T) if a.benchmark == "random"
                 else SH.score(r["z"], fit))
            if s is None or s <= 0:
                continue
            b = store.upto(tk, T)
            rr = [x for x in BR.log_return(b[-61:]) if x is not None]
            vol = ((sum(x * x for x in rr) / len(rr)) ** 0.5 * (252 ** 0.5)
                   if len(rr) >= 20 else None)
            cands.append(SZ.Candidate(ticker=tk, sector=r["sector"], score=s,
                                      volatility=vol, adv_jpy=r["adv_jpy"]))

        cur_val = pf.value(px) or a.capital
        if a.policy == "hysteresis":
            cur_w = pf.weights(px)
            w, exits, n_keep, n_new = hysteresis_targets(
                pf, cands, cur_w, limits, a.entry_weight,
                a.exit_rank_mult, forced)
            force_set = set(exits) | forced
        else:
            w, _ = SZ.target_positions(cands, cur_val, limits)
            for tk in forced:
                w[tk] = 0.0
            force_set = set(forced)

        # --- 4) 執行（翌営業日の始値）------------------------------------
        touch = set(w) | set(pf.positions)
        notes = PF.execute(pf, w, {t: store.get(t) for t in touch}, T, costs,
                           min_trade_frac=a.min_trade_frac, force=force_set)
        for x in notes:
            key = ("現金切り詰め" if "切り詰め" in x
                   else "約定不能" if "約定できなかった" in x
                   else "価格なし" if "価格が無い" in x else "その他")
            note_count[key] += 1
        # **実現後**の投資比率（次の評価時点まで分からないので、
        # ここでは T の終値で見た「執行後ただちに」の近似を取る）
        px2 = PF.mark_to_market(pf, {t: store.get(t) for t in pf.positions}, T)
        rw = pf.weights(px2)
        invested.append((T, sum(v for v in w.values() if v > 0),
                         sum(1 for v in w.values() if v > 0),
                         sum(rw.values()), len(rw), len(cands)))
        # 新規建ての配当の起点を約定日にする
        for tk in pf.positions:
            divmark.setdefault(tk, pf.positions[tk].entry_date)

        # --- 5) 次の月末までは**毎営業日**売りと配当だけ見る ---------------
        # **最終パネル日で止める。** 従来は "9999-12-31" で、最終日以降も
        # 日次処理がデータの末尾まで走っていた（成績には売りルール無しなら
        # 影響しないが、配当計上と日次DDを期間外に汚す。スモークで発見）
        nxt = dates[k + 1] if k + 1 < len(dates) else T
        held0 = {t: store.get(t) for t in pf.positions}
        days = sorted({x["date"] for b in held0.values() for x in b
                       if T < x["date"] < nxt})
        for d in days:
            if not pf.positions:
                break
            held = {t: store.get(t) for t in pf.positions}
            pxd = PF.mark_to_market(pf, held, d)
            if a.dividends == "on":
                PF.credit_dividends(pf, held, d, divmark)
            daily_equity.append((d, pf.value(pxd)))
            hit = {}
            for tk, pos in list(pf.positions.items()):
                r, _ = SL.decide(pos, SL.MarketState(), rules, d)
                if r is not SL.SellReason.HOLD:
                    hit[tk] = 0.0
                    reasons[r.name] += 1
            if hit:
                PF.execute(pf, {**pf.weights(pxd), **hit},
                           {t: store.get(t) for t in pf.positions}, d, costs,
                           min_trade_frac=a.min_trade_frac,
                           force=set(hit))

    # ------------------------------------------------------------------ 結果
    if not equity:
        print("**1回も生成できなかった。** 訓練の観測が足りない。")
        return 0
    year_tax(9999) if a.tax == "on" else None
    px = PF.mark_to_market(pf, {t: store.get(t) for t in pf.positions},
                           dates[-1])
    final = pf.value(px)
    yrs = ((dt.date.fromisoformat(equity[-1][0])
            - dt.date.fromisoformat(equity[0][0])).days / 365.25) or 1.0
    cagr = (final / a.capital) ** (1.0 / yrs) - 1.0

    print("-" * 78)
    print("結果")
    print("-" * 78)
    print("  生成回数            %d" % n_gen)
    print("  期間                %.1f 年" % yrs)
    print("  **最終評価額        %s 円**（元本 %s）"
          % ("{:,.0f}".format(final), "{:,.0f}".format(a.capital)))
    print("  **年率（CAGR）      %+.2f%%**%s"
          % (100 * cagr, "（税引後）" if a.tax == "on" else ""))
    print("  約定回数            %d" % len(pf.fills))
    if len(pf.fills) < n_gen:
        print("  " + "!" * 60)
        print("  **約定が生成回数(%d)より少ない。システムがほぼ動いていない。**" % n_gen)
        print("  " + "!" * 60)
    print("  **払ったコスト合計  %.0f 円（元本の %.1f%%）**"
          % (PF.total_costs(pf), 100 * PF.total_costs(pf) / a.capital))
    div_total = sum(v for _, v in pf.divs)
    if a.dividends == "on":
        print("  受け取った配当      %s 円" % "{:,.0f}".format(div_total))
    if a.tax == "on":
        print("  **払った税          %s 円**" % "{:,.0f}".format(tax_paid))

    # 最大ドローダウン: 月末と**日次**の両方（V3。月末だけでは浅く見える）
    def mdd_of(seq):
        peak, m, at = 0.0, 0.0, ""
        for d, v in seq:
            peak = max(peak, v)
            if peak > 0 and (v / peak - 1.0) < m:
                m, at = v / peak - 1.0, d
        return m, at
    mdd_m, at_m = mdd_of(equity)
    mdd_d, at_d = mdd_of(sorted(set(equity) | set(daily_equity)))
    print("  最大DD（月末）      %.1f%%（%s）" % (100 * mdd_m, at_m))
    print("  **最大DD（日次）    %.1f%%**（%s）" % (100 * mdd_d, at_d))

    if invested:
        tgt = sorted(x[1] for x in invested)
        rlz = sorted(x[3] for x in invested)
        nn = sorted(x[4] for x in invested)
        print("  投資比率 目標中央値 %.0f%% / **実現中央値 %.0f%%**"
              "（実現最小 %.0f%%）"
              % (100 * tgt[len(tgt) // 2], 100 * rlz[len(rlz) // 2],
                 100 * rlz[0]))
        print("  保有銘柄数 中央値 %d（最小 %d / 最大 %d）"
              % (nn[len(nn) // 2], nn[0], nn[-1]))
    if note_count:
        print("  執行の通知: %s"
              % " / ".join("%s %d" % (k, v) for k, v in note_count.items()))
    if zombie_exits:
        print("  " + "!" * 60)
        print("  **価格系列が尽きた保有を %d 回、最終値で強制手仕舞いした。**"
              % zombie_exits)
        print("  廃止銘柄の対価は最終値と限らない。この数字は不確かである。")
        print("  " + "!" * 60)
    if a.dump:
        pathlib.Path(a.dump).write_text(
            json.dumps([{"date": d, "value": v} for d, v in equity]
                       + [{"date": dates[-1], "value": final}]),
            encoding="utf-8")
        print("  → 評価額の推移を %s に書き出した" % a.dump)

    print()
    print("  売りの内訳:")
    if reasons:
        for r, n in reasons.most_common():
            print("    %-22s %5d" % (r, n))
    else:
        print("    **一度も発動していない**（ルールが None のため）")
    if store.misses:
        print("  価格が取れなかった銘柄 %d（**黙って無視していない**）" % store.misses)

    print()
    print("  " + "!" * 60)
    print("  **これは検証ではない**（docs/05 §1.3, §4.5）。")
    print("  生存者バイアス・カタログのルックアヘッド・**同一パネル上の**")
    print("  **構造選択**の3つの上振れが残る（docs/12 §0）。")
    print("  **同一データ・1条件差の相対比較にのみ使う。**")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
