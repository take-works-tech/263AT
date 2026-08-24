#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
UNIVERSE(t) の構築（docs/02_definition_spec.md §6）。

**必ず「その時点で」判定する。**
現在のユニバースで過去を検証すると生存者バイアス（§8-2）が入り、
成績が劇的に良く見える。これは 263AT で最も起こしやすく、
かつ最も気づきにくい誤りなので、**判定を1箇所に閉じ込める。**

§6 の条件
---------
    上場している(t)                     # 上場廃止銘柄も「過去のその時点では」含む
    上場後経過月数 >= 6                  # N24 は別に減点する（ここでは除外だけ）
    平均売買代金(20d) >= 600万円          # J01
    出来高ゼロ日数(60d) == 0             # J10
    円換算時価総額 >= 30億円              # rho で可変（§1.6）
    整理銘柄・監理銘柄でない              # N23
    継続企業の前提に関する注記がない       # D13
    直近の監査意見が無限定適正            # E22

rho（リスク許容度ダイヤル、§1.6）
--------------------------------
**閾値を rho で動かせるようにしてある。**
rho を上げると小型・低流動性まで踏み込み、下げると安全側に寄る。
263AT の「1/10 が全部を賄う」設計では、**rho を下げすぎると
そもそも狙う銘柄がユニバースに入らない**ので、
**J01 の 600万円は下限であって推奨値ではない**（§1.6 の議論）。

**除外の理由を必ず記録する。**
「なぜこの銘柄が入っていないのか」が後から追えないと、
ユニバース定義のバグに気づけない。

自己テスト
    python src/universe.py
"""
from __future__ import annotations

import dataclasses
import enum


class Exclusion(enum.Enum):
    """除外理由。**集計してユニバースの絞られ方を監視する。**"""

    NOT_LISTED = "上場していない"
    TOO_YOUNG = "上場後6ヶ月未満（N24 とは別に、値付けが不安定なため）"
    ILLIQUID = "平均売買代金が下限未満（J01）"
    ZERO_VOLUME = "出来高ゼロ日がある（J10）"
    TOO_SMALL = "円換算時価総額が下限未満"
    SUPERVISED = "整理・監理銘柄（N23）"
    GOING_CONCERN = "継続企業の前提に関する注記（D13）"
    AUDIT_OPINION = "監査意見が無限定適正でない（E22）"
    LOW_PRICE = "株価が低すぎる（板の刻みに対して比が大きく、往復コストが実現不能）"
    MISSING_DATA = "判定に必要なデータが欠損"


@dataclasses.dataclass(frozen=True)
class Thresholds:
    """§6 の閾値。**rho（§1.6）で動かす。**

    rho = 1.0 が既定。**大きいほど攻める**（下限を下げる）。
    """

    min_months_listed: int = 6
    min_adv_jpy: float = 6_000_000.0        # J01: 600万円
    max_zero_volume_days: int = 0           # J10
    min_mcap_jpy: float = 3_000_000_000.0   # 30億円

    # **最低株価。市場ごとに現地通貨で持つ。**
    #
    # 円換算した単一の閾値では機能しない。日本の 200円 の銘柄は普通だが、
    # 米国の $1.3（同額）は仕手株の領域である。**同じ金額でも意味が違う。**
    #
    # なぜ要るか（2026-08-23、実データで踏んだ）:
    #   2020-10-31 の断面で AITX の90日リターンが **+10,729%** だった。
    #   データの誤りではない。$0.0007 -> $0.08 の実際の値動きである。
    #   しかし**この利益は取れない。** 板の刻み $0.0001 に対して
    #   株価が $0.0007 なので、**買って売るだけで 14% 以上が消える。**
    #   ADV のゲートは通ってしまう（株数が膨大なので売買代金は立つ）。
    #
    #   結果として下位分位の平均が +250% に化け、
    #   **上位-下位の差が -206.9% という、長期保有ではあり得ない値**が出た。
    #   下位10%の 16 回だけで合計 -618.7%（全体は +232.0%）。
    #   **測定の2割が、取れない利益で決まっていた。**
    #
    # 本来は「板の刻み ÷ 株価」で測るのが筋である（往復コストそのもの）。
    # 刻みの規則を市場ごとに持っていないので、**株価を代理に使う。** OQ に残す。
    #
    # **$1 という値を株価帯別に検証した（2026-08-24、5年後の倍率）。**
    #
    # **一度目の測定は間違っていた。** 遡及調整後の株価で帯を作っており、
    # 「調整後が低い」＝「その後大きく分割した」＝「大きく上がった」
    # という**未来のリターンで銘柄を選んでいた**（prices.unadjust_factor）。
    # そのときは「$1-2 が全指標で最良（中央値1.91）」と出ていた。
    #
    # **その時点で実際に付いていた株価で測り直すと、結論は逆転した。**
    #
    #   株価帯      中央値  平均  5倍+   10倍+  半減
    #   $1未満      0.15  0.76   2.45%  1.23%  **69.0%**
    #   $1-2        0.38  1.76   7.59%  2.91%  **54.4%**
    #   $2-5        0.71  1.66   5.66%  1.99%  40.5%
    #   $5-20       1.13  1.63   4.58%  0.89%  22.0%
    #   $20-100     1.29  1.56   1.81%  0.16%  **9.2%**
    #
    # **低位株は5年で中央値が 0.15-0.38 倍になる。** 7割が半減する。
    # 10倍株の出現率は確かに高いが（2.91% 対 0.16%）、
    # **20銘柄持っても 0.58本しか引けず、10.9本が半減する。**
    # 平均 1.76 は 10倍株が引き上げているだけで、**引ける保証がない。**
    #
    # → **$1 は据え置く。** ただし理由は一度目と違う。
    #   一度目は「$1-2 が良いから境界として妥当」だったが、
    #   正しい理由は「**$1未満は中央値 0.15 で買ってはいけない**」である。
    #
    # **平均だけを見て判断すると誤る。** 一度目も二度目も、
    # 平均は低位株帯の方が高かった。**中央値と半減率を見て初めて分かる。**
    min_price_local: dict = dataclasses.field(
        default_factory=lambda: {"US": 1.0, "JP": 100.0})

    # **上場期間のゲートを明示的に無効化できるようにする。**
    #
    # 実データで踏んだ（2026-08-23）: 上場後経過月数を
    # 「取得できた価格のバー数 ÷ 21」で概算していたところ、
    # **2年分しか価格を落としていないので、2024年11月時点では
    # 全銘柄が「上場3.3ヶ月」に見えて全滅した**（181/181 が TOO_YOUNG）。
    #
    # **データの取得範囲を、銘柄の属性と取り違えていた。**
    #
    # 上場日が本当に取れないなら、**偽の近似で埋めるのではなく
    # ゲートを明示的に切る。** そうすれば除外内訳に現れないので、
    # 「このゲートは効いていない」ことが読む人に分かる。
    require_age: bool = True

    @classmethod
    def for_rho(cls, rho: float) -> "Thresholds":
        """rho で流動性・サイズの下限を動かす。

        **上場期間・整理銘柄・継続企業の前提・監査意見は動かさない。**
        これらは「リスク許容度」の問題ではなく、
        **踏んではいけない地雷**だから（§1.6 と D/E カテゴリの検証）。
        """
        if not 0.2 <= rho <= 3.0:
            raise ValueError("rho は 0.2〜3.0 の範囲で指定する（実際に意味のある範囲）")
        return cls(
            min_months_listed=cls.min_months_listed,
            min_adv_jpy=cls.min_adv_jpy / rho,
            max_zero_volume_days=cls.max_zero_volume_days,
            min_mcap_jpy=cls.min_mcap_jpy / rho,
            require_age=cls.require_age,
            # **最低株価は rho で動かさない。**
            # 低位株を買うかどうかは「リスク許容度」の問題ではない。
            # 板の刻みに対して株価が低いと、**利益そのものが実現しない。**
            # 攻めても取れないものは、攻めても取れない。
        )


@dataclasses.dataclass(frozen=True)
class Candidate:
    """ある時点 t における1銘柄の状態。**すべて available_at <= t のもの。**"""

    ticker: str
    listed: bool
    months_listed: float | None
    adv_jpy: float | None            # 円換算済み（§5）
    zero_volume_days: int | None
    mcap_jpy: float | None           # 円換算済み
    supervised: bool                 # 整理・監理
    going_concern_note: bool         # 継続企業の前提に関する注記
    audit_clean: bool | None         # 無限定適正なら True。None は未取得
    price_local: float | None = None   # **現地通貨の株価**（円換算しない）
    market: str = "US"


def judge(c: Candidate, th: Thresholds) -> list[Exclusion]:
    """**除外理由を全部返す。** 最初の1つで打ち切らない。

    打ち切ると「流動性で落ちた」としか分からず、
    その銘柄が同時に監査意見でも落ちていたことを見逃す。
    ユニバースの絞られ方を監視するには全部要る。
    """
    out: list[Exclusion] = []
    if not c.listed:
        out.append(Exclusion.NOT_LISTED)
    if th.require_age:
        if c.months_listed is None:
            out.append(Exclusion.MISSING_DATA)
        elif c.months_listed < th.min_months_listed:
            out.append(Exclusion.TOO_YOUNG)
    if c.adv_jpy is None:
        out.append(Exclusion.MISSING_DATA)
    elif c.adv_jpy < th.min_adv_jpy:
        out.append(Exclusion.ILLIQUID)
    if c.zero_volume_days is None:
        out.append(Exclusion.MISSING_DATA)
    elif c.zero_volume_days > th.max_zero_volume_days:
        out.append(Exclusion.ZERO_VOLUME)
    if c.mcap_jpy is None:
        out.append(Exclusion.MISSING_DATA)
    elif c.mcap_jpy < th.min_mcap_jpy:
        out.append(Exclusion.TOO_SMALL)
    # **最低株価。** 円換算せず、市場ごとの現地通貨で比べる
    lim = th.min_price_local.get(c.market)
    if lim is not None:
        if c.price_local is None:
            out.append(Exclusion.MISSING_DATA)
        elif c.price_local < lim:
            out.append(Exclusion.LOW_PRICE)
    if c.supervised:
        out.append(Exclusion.SUPERVISED)
    if c.going_concern_note:
        out.append(Exclusion.GOING_CONCERN)
    # **監査意見が未取得（None）を「適正」に丸めない。**
    # 丸めると、データが無い銘柄が自動的に通ってしまう。
    if c.audit_clean is not True:
        out.append(Exclusion.AUDIT_OPINION if c.audit_clean is False
                   else Exclusion.MISSING_DATA)
    # 同じ理由が2回入ることがある（MISSING_DATA）ので潰す
    seen, uniq = set(), []
    for e in out:
        if e not in seen:
            seen.add(e)
            uniq.append(e)
    return uniq


def build(candidates: list[Candidate], rho: float = 1.0
          ) -> tuple[list[str], dict[Exclusion, int]]:
    """UNIVERSE(t) を返す。第2要素は除外理由の内訳。

    **内訳を返すのが本質。** ユニバースが急に縮んだとき、
    何が原因かが分からないと対処できない。
    """
    th = Thresholds.for_rho(rho)
    keep, reasons = [], {}
    for c in candidates:
        ex = judge(c, th)
        if not ex:
            keep.append(c.ticker)
        for e in ex:
            reasons[e] = reasons.get(e, 0) + 1
    return keep, reasons


def report(candidates: list[Candidate], rho: float = 1.0) -> str:
    """人が読む形の内訳。運用時に毎日出す想定。"""
    keep, reasons = build(candidates, rho)
    n = len(candidates)
    lines = ["UNIVERSE(t)  rho=%.2f  %d / %d 銘柄が残った" % (rho, len(keep), n)]
    if n:
        lines.append("-" * 60)
        for e, cnt in sorted(reasons.items(), key=lambda kv: -kv[1]):
            lines.append("  %-5d %s" % (cnt, e.value))
    return "\n".join(lines)


# ---------------------------------------------------------------- self-test
def _ok(t="OK", **kw) -> Candidate:
    base = dict(ticker=t, listed=True, months_listed=24.0, adv_jpy=50_000_000.0,
                zero_volume_days=0, mcap_jpy=50_000_000_000.0, supervised=False,
                going_concern_note=False, audit_clean=True,
                price_local=20.0, market="US")
    base.update(kw)
    return Candidate(**base)


def _test() -> int:
    fails = []
    ran = []

    def check(name, cond):

        ran.append(name)
        if not cond:
            fails.append(name)
        print("  %-56s %s" % (name, "OK" if cond else "**FAIL**"))

    print("src/universe.py 自己テスト")
    print("-" * 70)
    th = Thresholds()

    check("すべて満たす銘柄は通る", judge(_ok(), th) == [])
    check("上場していない銘柄は落ちる",
          Exclusion.NOT_LISTED in judge(_ok(listed=False), th))
    check("上場5ヶ月は落ちる", Exclusion.TOO_YOUNG in judge(_ok(months_listed=5.0), th))
    check("上場6ヶ月ちょうどは通る", judge(_ok(months_listed=6.0), th) == [])
    check("売買代金 599万円は落ちる（J01）",
          Exclusion.ILLIQUID in judge(_ok(adv_jpy=5_990_000.0), th))
    check("出来高ゼロ日が1日でもあれば落ちる（J10）",
          Exclusion.ZERO_VOLUME in judge(_ok(zero_volume_days=1), th))
    check("時価総額 29億円は落ちる",
          Exclusion.TOO_SMALL in judge(_ok(mcap_jpy=2_900_000_000.0), th))
    check("整理・監理銘柄は落ちる（N23）",
          Exclusion.SUPERVISED in judge(_ok(supervised=True), th))
    check("継続企業の前提の注記があれば落ちる（D13）",
          Exclusion.GOING_CONCERN in judge(_ok(going_concern_note=True), th))

    # --- 最低株価（2026-08-23 に実データで踏んだ） -------------------------
    check("**米国のサブペニー株を落とす**",
          Exclusion.LOW_PRICE in judge(
              _ok(price_local=0.0007, market="US"), th))
    check("米国の $1 以上は通す",
          Exclusion.LOW_PRICE not in judge(
              _ok(price_local=1.5, market="US"), th))
    check("**日本の 200円 は通す（同額の $1.3 とは意味が違う）**",
          Exclusion.LOW_PRICE not in judge(
              _ok(price_local=200.0, market="JP"), th))
    check("**日本でも 50円 は落とす**",
          Exclusion.LOW_PRICE in judge(
              _ok(price_local=50.0, market="JP"), th))
    check("**株価が未取得なら通さない（欠損を「高い」に丸めない）**",
          Exclusion.MISSING_DATA in judge(
              _ok(price_local=None, market="US"), th))
    check("**rho を上げても最低株価は下がらない**",
          Thresholds.for_rho(3.0).min_price_local["US"]
          == Thresholds.for_rho(0.5).min_price_local["US"])
    check("流動性の下限は rho で下がる",
          Thresholds.for_rho(3.0).min_adv_jpy
          < Thresholds.for_rho(0.5).min_adv_jpy)
    check("監査意見が不適正なら落ちる（E22）",
          Exclusion.AUDIT_OPINION in judge(_ok(audit_clean=False), th))
    check("**監査意見が未取得（None）を『適正』に丸めない**",
          Exclusion.MISSING_DATA in judge(_ok(audit_clean=None), th))
    # **上場期間のゲートは明示的に切れる。**（上場日が取れないデータ源のため）
    no_age = Thresholds(require_age=False)
    check("**require_age=False なら上場期間で落とさない**",
          judge(_ok(months_listed=1.0), no_age) == [])
    check("**上場日が未取得でも落とさない（ゲートを切っているので）**",
          judge(_ok(months_listed=None), no_age) == [])
    check("既定では落とす", Exclusion.TOO_YOUNG in judge(_ok(months_listed=1.0), th))
    check("**他のゲートは切らない**",
          Exclusion.ILLIQUID in judge(_ok(months_listed=1.0, adv_jpy=1.0), no_age))

    check("**除外理由を全部返す（最初の1つで打ち切らない）**",
          len(judge(_ok(listed=False, supervised=True, audit_clean=False), th)) >= 3)

    # rho による可変
    lo = Thresholds.for_rho(0.5)
    hi = Thresholds.for_rho(2.0)
    check("rho を上げると流動性の下限が下がる（攻める）",
          hi.min_adv_jpy < th.min_adv_jpy < lo.min_adv_jpy)
    check("rho を上げると時価総額の下限も下がる",
          hi.min_mcap_jpy < th.min_mcap_jpy < lo.min_mcap_jpy)
    check("**rho では上場期間の下限を動かさない**",
          hi.min_months_listed == lo.min_months_listed == th.min_months_listed)
    try:
        Thresholds.for_rho(10.0)
        check("rho の範囲外は拒否する", False)
    except ValueError:
        check("rho の範囲外は拒否する", True)

    small = _ok("SMALL", adv_jpy=4_000_000.0, mcap_jpy=2_000_000_000.0)
    check("rho=2.0 なら小型株が入る", judge(small, hi) == [])
    check("rho=1.0 では入らない", judge(small, th) != [])

    keep, reasons = build([_ok("A"), _ok("B", supervised=True), _ok("C", listed=False)])
    check("build がユニバースと内訳を返す",
          keep == ["A"] and reasons[Exclusion.SUPERVISED] == 1)
    check("report が読める形を出す", "UNIVERSE(t)" in report([_ok("A")]))

    print("-" * 70)
    declared = 31
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
