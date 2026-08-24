#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**証券の種別を判定する。** 普通株でないものをユニバースから外す。

なぜ要るか — **実際の発注内容を作って初めて分かった**
------------------------------------------------------
2026-05-31 の生成の上位4銘柄は、こうだった。

    ATLCL 12.0%   Atlanticus Holdings **6.125% Senior Notes due 2026**
    ATLCZ 11.3%   Atlanticus Holdings **9.25% Senior Notes due 2029**
    AIZN   9.9%   Assurant **5.25% Subordinated Notes due 2061**
    ATLCP  7.8%   Atlanticus Holdings **7.625% Series B Preferred Stock**

**上位3つは株式ですらない。社債である。**
そして4つ合わせて **41%**、うち3つは**同一発行体（CIK 1464343）**である。

なぜ社債が上位に来たか — **サイジングが増幅していた**
------------------------------------------------------
2つの誤りが噛み合っていた。

  1. **財務パラメータは CIK で引く。** 社債も優先株も普通株と CIK を共有するので、
     **発行体の財務諸表がそのまま社債に付く。** SUE もバリューも、
     普通株の数字が社債の「スコア」になっていた。

  2. **Kelly サイジングはボラティリティで割る。**
     社債は額面 $25 の近くで動くので、ボラがそもそも小さい。

     **実測（2026-05-31 まで252日、年率）:**

         社債・優先株 6本   中央値 **7.5%**（ATLCL 5.5 / ATLCZ 5.2 / BAC-PB 5.0）
         選ばれた普通株 8本  中央値 **45.0%**（SNEX 41.7 / AYTU 64.0）
         同一発行体の普通株  中央値 23.5%（ATLC は **51.1%**）

     → **同じスコアなら社債に 6.0 倍の比率が付く。**
     Atlanticus 1社で見れば ATLC 51.1% 対 ATLCL 5.5% で **9.3 倍**である。

**「スコアが高いから上に来た」のではない。「ボラが低いから上に来た」。**
設計方針は「9割が負けても1割が何十倍」である。
**額面 $25 に張り付く社債は、その正反対の性質を持つ。**

どれだけ混ざっていたか
----------------------
9,631 ティッカー中、**1,874（19.5%）が発行体の主銘柄ではない。**
現在の上場一覧と突合できた 6,940 のうち、**969 が普通株ではなかった。**

    UNIT 298 / WARRANT 268 / NOTE 178 / PREFERRED 132 / ETF 76 / RIGHT 17

さらに CIK 単位で見ると、**ETF・ETN が丸ごと入っていた。**

    cik  927971（Bank of Montreal）→ BNKU, FNGU, NRGD ... **32本すべてレバレッジ ETN**
    cik 1415311（ProShares）      → UVXY, BOIL, KOLD ... **16本すべてレバレッジ ETF**
    cik 1026214（Freddie Mac）    → FMCC + **優先株 24 シリーズ**

判定の3つの規則
---------------
**単独では穴があるので3つ重ねる。**

  規則1 **上場一覧の証券名で判定する**（最も確か）
        Nasdaq Trader の SymDir は無料・登録不要で、
        `Security Name` に "Common Stock" / "Senior Notes" / "Warrant" と書いてある。

  規則2 **ティッカーの形で判定する**（上場廃止銘柄にも効く）
        `-P<英字>` 優先株 / `-UN` ユニット / `-WT` 新株予約権 / `-RI` 権利
        **`-A` `-B` `-C` は複数議決権クラスの普通株なので残す**（BRK-A, BF-B）。

  規則3 **1発行体につき1銘柄**（規則1・2で残ったものの中から）
        時点 t の売買代金が最大のものを主銘柄とする。
        **これは PIT である** — 流動性は t の時点で測る。

PIT とバイアスの扱い — **ここを間違えると生存者バイアスを増やす**
----------------------------------------------------------------
上場一覧は**現在の**スナップショットである。だから

    **「一覧に載っていない」＝「除外」にしてはならない。**

上場廃止銘柄は載っていないので、それを除外すると
**生存している銘柄だけが残り、生存者バイアスが今より悪化する。**

    → **一覧は「除外リスト」としてのみ使う。**
       積極的に「普通株でない」と判定できたものだけ外す。
       載っていないものは規則2・3に回す。

証券種別そのものは**時間で変わらない静的な属性**なので、
今日の一覧で 2015年を判定してもルックアヘッドにはならない。
**「上場しているか」は時間で変わるが、それは判定していない。**

**実測して確かめた**（2026-08-25）。パネルに出た 4,262 銘柄について、
価格が 2026-06 以降まで続いているかで現存を判定した。

    除外した 440本  現存 **99.1%**
    残した 3,822本  現存 **100.0%**

**差は 0.9pp（4銘柄）で、実質的に動いていない。**

ただしこの測定は、**もっと大きな問題を映している。**
残した側が 100.0% なのは、価格データが SEC の**現在の**銘柄一覧から
取られているため、**上場廃止銘柄がそもそも1本も入っていない**からである。
（docs/09 の「2012年の残存率 32.6%」と同じことを別の角度から見ている。）

→ 証券種別の除外は生存者バイアスを**悪化させないが、直しもしない。**

自己テスト
    python src/security_type.py
"""
from __future__ import annotations

import enum
import json
import pathlib
import re
import sys


class Kind(enum.Enum):
    """証券の種別。**COMMON 以外はユニバースに入れない。**"""

    COMMON = "普通株"
    PREFERRED = "優先株・預託株式"
    NOTE = "社債・優先出資証券"
    UNIT = "ユニット（株式+新株予約権の抱き合わせ）"
    WARRANT = "新株予約権"
    RIGHT = "権利"
    FUND = "ETF・ETN・投資信託"
    UNKNOWN = "判定できない"


#: 証券名の判定。**上から順に当てる。順序に意味がある。**
#: 例: ETN は名前に "Notes" を含むので、NOTE より先に見る必要がある。
NAME_RULES: tuple[tuple[Kind, str], ...] = (
    (Kind.FUND, r"\bET[FN]s?\b|\bexchange[- ]traded\b|\bleveraged\b"
                r"|\bindex\b.{0,40}\b(fund|note|trust)"),
    (Kind.WARRANT, r"\bwarrants?\b"),
    (Kind.UNIT, r"\bunits?\b"),
    (Kind.RIGHT, r"\brights?\b"),
    # **ADR を優先株より先に見る。** どちらも "Depositary Shares" と書かれる。
    #   ADR      "American Depositary Shares, each representing two common shares"
    #   優先株   "Depositary Sh repstg 1/1000th Perp **Pfd** Ser E"
    # **"Pfd" という略記があるので、"preferred" だけでは足りない。**
    (Kind.COMMON, r"\bamerican depositary\b"),
    (Kind.PREFERRED, r"\bpreferred\b|\bpreference shar|\bpfd\b"
                     r"|\bdepositary sh"),
    (Kind.NOTE, r"\bnotes?\b|\bdebenture|\bbonds?\b|capital securities"),
    (Kind.COMMON, r"\bcommon stock\b|\bordinary share|\bcommon share"
                  r"|\bclass [a-z]\b"),
)

#: ティッカーの形の判定。**`-` の後ろだけを見る。**
#: **1文字（A/B/C…）は複数議決権クラスなので普通株として残す。**
SHAPE_RULES: tuple[tuple[Kind, str], ...] = (
    (Kind.PREFERRED, r"^P[A-Z]$|^PR[A-Z]?$"),
    (Kind.UNIT, r"^U$|^UN$"),
    (Kind.WARRANT, r"^W$|^WT$|^WS[A-Z]?$"),
    (Kind.RIGHT, r"^R$|^RI$|^RT$"),
)

#: 規則3で主銘柄を選ぶときの最小売買代金。
MIN_ADV_FOR_PRIMARY = 0.0


def classify_name(name: str | None) -> Kind:
    """**証券名から種別を判定する。**（規則1）

    判定できないときは `UNKNOWN` を返す。
    **COMMON に丸めない。** 丸めると社債が普通株として通る。
    """
    if not name:
        return Kind.UNKNOWN
    for kind, pat in NAME_RULES:
        if re.search(pat, name, re.I):
            return kind
    return Kind.UNKNOWN


def classify_shape(ticker: str) -> Kind:
    """**ティッカーの形から判定する。**（規則2）

    `-` を含まない、または `-` の後ろが1文字の英字なら `UNKNOWN`。
    **`BRK-B` を優先株にしてはいけない。**
    """
    if not ticker or "-" not in ticker:
        return Kind.UNKNOWN
    suf = ticker.rsplit("-", 1)[1].upper()
    if len(suf) == 1 and suf.isalpha() and suf not in ("U", "W", "R"):
        return Kind.UNKNOWN          # **複数議決権クラス。残す**
    for kind, pat in SHAPE_RULES:
        if re.match(pat, suf):
            return kind
    return Kind.UNKNOWN


def classify(ticker: str, name: str | None = None) -> Kind:
    """規則1 → 規則2 の順で判定する。

    **証券名がある方を優先する。** 形の判定は当て推量だが、
    証券名は取引所が付けたものである。
    """
    k = classify_name(name)
    if k is not Kind.UNKNOWN:
        return k
    return classify_shape(ticker)


def is_excluded(kind: Kind) -> bool:
    """**ユニバースから外すか。**

    `UNKNOWN` は**外さない。** 判定できないものを外すと、
    上場一覧に載っていない上場廃止銘柄が丸ごと消え、
    **生存者バイアスが今より悪化する**（冒頭の注記）。
    """
    return kind not in (Kind.COMMON, Kind.UNKNOWN)


def primary_by_issuer(members: list[dict]) -> str | None:
    """**1発行体につき1銘柄を選ぶ。**（規則3）

    `members` は `{"ticker": str, "kind": Kind, "adv": float|None}`。
    同じ CIK に属するものだけを渡す。

    選び方
      1. **除外種別でないもの**だけを候補にする
      2. その中で **時点 t の売買代金が最大**のものを返す
      3. 売買代金が誰も無ければ **None**（主銘柄を決めない）

    なぜ売買代金か
      **普通株は、同じ発行体の優先株や社債より必ず活発に取引される。**
      銘柄コードの長さや辞書順で選ぶより、実際の取引を見る方が確かで、
      **上場廃止銘柄にも効く。**
    """
    cand = [m for m in members if not is_excluded(m.get("kind", Kind.UNKNOWN))]
    if not cand:
        return None
    with_adv = [m for m in cand
                if m.get("adv") is not None and m["adv"] > MIN_ADV_FOR_PRIMARY]
    if not with_adv:
        # **勝手に1本選ばない。** 根拠が無いまま選ぶと、
        # 社債を主銘柄にしてしまう場合がある
        return None
    return max(with_adv, key=lambda m: m["adv"])["ticker"]


def load(path: str | pathlib.Path) -> dict[str, Kind]:
    """`tools/build_sectypes.py` が作った索引を読む。"""
    p = pathlib.Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {k: Kind[v] for k, v in (raw.get("kinds") or {}).items()}


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-68s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/security_type.py 自己テスト")
    print("-" * 82)

    # --- 規則1: 実際に上位に来た4本 -------------------------------------
    check("**ATLCL は社債**（実際に12.0%を占めていた）",
          classify_name("Atlanticus Holdings Corporation - "
                        "6.125% Senior Notes due 2026") is Kind.NOTE)
    check("**AIZN は社債**（9.9%を占めていた）",
          classify_name("Assurant, Inc. 5.25% Subordinated Notes due 2061")
          is Kind.NOTE)
    check("**ATLCP は優先株**（7.8%）",
          classify_name("Atlanticus Holdings Corporation - 7.625% Series B "
                        "Cumulative Perpetual Preferred Stock")
          is Kind.PREFERRED)
    check("**ATLC は普通株**（本来こちらを買うべきだった）",
          classify_name("Atlanticus Holdings Corporation - Common Stock")
          is Kind.COMMON)

    check("預託株式は優先株として扱う",
          classify_name("Arch Capital Group Ltd. - Depositary Shares, each "
                        "Representing 1/1,000th") is Kind.PREFERRED)
    check("新株予約権", classify_name("Able View Global Inc. - Warrant")
          is Kind.WARRANT)
    check("ユニット", classify_name("Armada Acquisition Corp. III - Units")
          is Kind.UNIT)
    check("権利", classify_name("Apogee Acquisition Corp - Rights")
          is Kind.RIGHT)
    check("ETF", classify_name("ARK 21Shares Bitcoin ETF") is Kind.FUND)
    check("**ETN は名前に Notes を含むが社債ではない**（順序が効く）",
          classify_name("MicroSectors FANG+ Index 3X Leveraged ETN")
          is Kind.FUND)
    check("**ADR は普通株**（優先株と同じ Depositary Shares と書かれる）",
          classify_name("ATA Creativity Global - American Depositary Shares, "
                        "each representing two common shares") is Kind.COMMON)
    check("**Pfd と略された優先株も捕まえる**（preferred と書かれない）",
          classify_name("Bank of America Corporation Depositary Sh repstg "
                        "1/1000th Perp Pfd Ser E") is Kind.PREFERRED)
    check("**空の名前は UNKNOWN**（COMMON に丸めない）",
          classify_name(None) is Kind.UNKNOWN and classify_name("")
          is Kind.UNKNOWN)

    # --- 規則2: ティッカーの形 -------------------------------------------
    check("**BAC-PB は優先株**", classify_shape("BAC-PB") is Kind.PREFERRED)
    check("**BRK-B は普通株として残す**（複数議決権クラス）",
          classify_shape("BRK-B") is Kind.UNKNOWN)
    check("BF-A も残す", classify_shape("BF-A") is Kind.UNKNOWN)
    check("AAC-UN はユニット", classify_shape("AAC-UN") is Kind.UNIT)
    check("-WT は新株予約権", classify_shape("XYZ-WT") is Kind.WARRANT)
    check("-RI は権利", classify_shape("XYZ-RI") is Kind.RIGHT)
    check("`-` が無ければ判定しない", classify_shape("AAPL") is Kind.UNKNOWN)

    # --- 合成 -------------------------------------------------------------
    check("**証券名が形より優先する**",
          classify("BRK-B", "Berkshire Hathaway Inc. Class B Common Stock")
          is Kind.COMMON)
    check("名前が無ければ形で判定",
          classify("BAC-PB", None) is Kind.PREFERRED)

    # --- 除外の方針 --------------------------------------------------------
    check("**UNKNOWN は外さない**（外すと生存者バイアスが悪化する）",
          is_excluded(Kind.UNKNOWN) is False)
    check("COMMON は外さない", is_excluded(Kind.COMMON) is False)
    check("社債・優先株・ETF は外す",
          all(is_excluded(k) for k in (Kind.NOTE, Kind.PREFERRED, Kind.FUND,
                                       Kind.UNIT, Kind.WARRANT, Kind.RIGHT)))

    # --- 規則3: 1発行体1銘柄 ----------------------------------------------
    atl = [{"ticker": "ATLC", "kind": Kind.COMMON, "adv": 5e8},
           {"ticker": "ATLCL", "kind": Kind.NOTE, "adv": 9e9},
           {"ticker": "ATLCZ", "kind": Kind.NOTE, "adv": 9e9},
           {"ticker": "ATLCP", "kind": Kind.PREFERRED, "adv": 9e9}]
    check("**売買代金が社債の方が大きくても、普通株を選ぶ**",
          primary_by_issuer(atl) == "ATLC")

    dual = [{"ticker": "GOOG", "kind": Kind.UNKNOWN, "adv": 1e9},
            {"ticker": "GOOGL", "kind": Kind.UNKNOWN, "adv": 2e9}]
    check("**複数議決権クラスは流動性が高い方を1本だけ残す**",
          primary_by_issuer(dual) == "GOOGL")

    check("**候補が全部除外種別なら None**",
          primary_by_issuer(atl[1:]) is None)
    check("**売買代金が無ければ None**（当て推量で選ばない）",
          primary_by_issuer([{"ticker": "X", "kind": Kind.COMMON,
                              "adv": None}]) is None)
    check("1本だけなら それを返す",
          primary_by_issuer([{"ticker": "X", "kind": Kind.COMMON,
                              "adv": 1.0}]) == "X")

    print("-" * 82)
    declared = 30
    if len(ran) != declared:
        fails.append("本数が宣言と違う")
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    raise SystemExit(_test())
