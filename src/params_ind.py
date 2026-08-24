#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**業種レベルのパラメータ。** 業種内では一定の値になる。

なぜ別の層にするか
------------------
カタログには**業種内で一定になるパラメータが12本**ある。

    A41 同業最安値スプレッド / C26 平均回帰調整済み成長率
    G36 業種モメンタム / **L01 同業大型株のリターン（再現 t=9.93）**
    L02 同業大型株の決算サプライズ / L09 業種内の情報伝播順位
    L10 日米間のリードラグ / Q09 業種内相対順位
    Q10 同業他社の相対パフォーマンス
    V13/V14/V15 業種集中度（売上/資産/純資産 HHI）

**これらは現状の業種内正規化を通すと、全員 z=0 になって消える。**
実測で確認した（業種A全員1.0・業種B全員2.0 でも、両方 z=0）。

**L01 は再現 t=9.93 で L カテゴリ最強**であり、
優先度分析でも「J-Quants 登録で解放される筆頭」と名指しされていた。
**実装しても、正規化の母集団が業種内のままでは効かない。**

→ **業種レベルのパラメータは、市場全体で順位を付ける。**
  `normalize()` の変更は不要で、**呼び出し側が group を揃えるだけ**である。

**業種を跨ぐことの危険**
------------------------
業種内正規化には理由があった。**業種を跨ぐと業種ダミーが混入する。**

第1回の事前登録で「数を数えたつもりで規模を測っていた」という
失敗をした。**業種横断では「業種を当てているだけ」になりうる。**
同じ構造の罠である。

→ **業種ダミーだけで説明できないかを、必ず併記して確認する**
  （`tools/measure_ind.py` が業種ダミーとの分離を出す）。

**実装が意図した現象を捉えているかの確認**（2026-08-24、393,421観測）
--------------------------------------------------------------
| 測定 | 値 |
|---|---|
| (a) 全体での L01 の上位−下位 | **+1.95%** |
| (b) 大型株のみ（売買代金 上位半分） | +1.64% |
| **(b) 小型株のみ（下位半分）** | **+2.23%** |
| **(c) 業種モメンタム単独**（G04 の業種平均） | **−0.67%** |

**業種ダミーとは2点で区別できた。**

1. **業種モメンタム単独は −0.67% で符号が逆。**
   L01 が「良い業種にいるか」を測っているだけなら同じ向きになるはずで、
   ならなかった。**L01 は業種を選んでいない。**

2. **小型株でより強い**（+2.23% 対 +1.64%）。
   Hou (2007) のリードラグが予測する形そのもの。
   大型株は「自分の直近リターン」を見ているだけなので効きが弱く、
   **小型株は大型株の動きに遅れて追随するので強く効く。**

**なぜ L01 と業種モメンタムが逆向きになるか。**
両者の違いは「業種の上位10社」か「業種全体」かだけである。
**業種全体には小型株が多数含まれ、そちらは短期反転（G04 の符号 −）が
支配的**になる。**大型株だけを見ると逆に先行指標になる。**
→ **「時価総額上位10社に絞る」という設計判断が、そのまま結果を分けている。**

**この測定は採用の根拠ではない。**
採用は `prior.py` の規則（**他者の再現 t >= 3.0**、L01 は 9.93）による。
ここで確認したのは**実装が意図した現象を捉えているか**だけである。
自分のデータの t は計算していない（観測が重なっており、
**業種内で一定なので実質「31業種 × 独立期間21個」**しかない）。

自己テスト
    python src/params_ind.py
"""
from __future__ import annotations

import pathlib
import statistics as st
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from params_us import Value  # type: ignore  # noqa: E402

# **この層のパラメータは市場全体で順位を付ける。**
# 業種内では一定なので、業種内で順位を付けると全員 z=0 になる。
SCOPE = "market"

# 「大型株」とみなす上位何銘柄か。
# **業種の規模で割合を変えない。** 割合にすると、
# 銘柄数の少ない業種で「大型株」が1〜2銘柄になり、個別銘柄のノイズになる。
TOP_K = 10
MIN_MEMBERS = 15      # これ未満の業種では作らない（大型株が定義できない）

PARAMS = ("L01",)

# 事前登録は不要。**他者による再現の t が 3.0 以上**という
# 既存の採用規則（src/prior.py）をそのまま満たす。
REPLICATED_T = {"L01": 9.93}


def l01(members: list[dict]) -> float | None:
    """**同業大型株の直近リターン。**

    `members` は同一業種の銘柄で、それぞれ
      {"mcap": 時価総額, "ret": 直近1ヶ月リターン}
    を持つ。

    時価総額の上位 `TOP_K` 銘柄の直近リターンの**平均**を返す。

    経済的な根拠（Hou 2007 の業種内リードラグ）:
    **同じ業種の大型株が先に動き、小型株が後から追う。**
    情報が大型株に先に織り込まれ、小型株には遅れて伝わる。

    **時価総額加重にしない。** 加重すると最大の1銘柄でほぼ決まり、
    「大型株が動いた」ではなく「その1社が動いた」を測ることになる。
    """
    ok = [m for m in members
          if m.get("mcap") is not None and m.get("ret") is not None]
    if len(ok) < MIN_MEMBERS:
        return None
    ok.sort(key=lambda m: -m["mcap"])
    top = ok[:TOP_K]
    if len(top) < 3:
        return None
    return st.fmean([m["ret"] for m in top])


def compute(by_sector: dict[str, list[dict]]) -> dict[str, dict[str, Value]]:
    """業種ごとに1つ値を作る。**戻りは {業種: {pid: Value}}。**

    呼び出し側が、同じ業種の全銘柄に同じ値を配る。
    """
    out: dict[str, dict[str, Value]] = {}
    for sec, members in by_sector.items():
        v = l01(members)
        out[sec] = {
            "L01": (Value("L01", float(v)) if v is not None
                    else Value("L01", None,
                               "業種の銘柄数が %d 未満、または大型株が3銘柄未満"
                               % MIN_MEMBERS))
        }
    return out


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    def near(a, b, tol=1e-9):
        return a is not None and abs(a - b) < tol

    print("src/params_ind.py 自己テスト")
    print("-" * 80)

    # 大型10社が +10%、残り10社が −10%
    mem = ([{"mcap": 1e10 - i, "ret": 0.10} for i in range(10)]
           + [{"mcap": 1e8 - i, "ret": -0.10} for i in range(10)])
    check("**上位10社のリターンの平均を返す**", near(l01(mem), 0.10))

    # 順序を入れ替えても同じ（時価総額で選ぶので）
    check("並び順に依存しない", near(l01(list(reversed(mem))), 0.10))

    # **時価総額加重ではない**ことの確認
    mem2 = [{"mcap": 1e12, "ret": 1.0}] + \
           [{"mcap": 1e9 - i, "ret": 0.0} for i in range(19)]
    check("**最大の1社に支配されない（加重平均ではない）**",
          near(l01(mem2), 1.0 / 10))

    check("**銘柄数が少ない業種では作らない**",
          l01([{"mcap": 1e9, "ret": 0.1}] * 10) is None)
    check("欠損を含む銘柄は数に入れない",
          l01([{"mcap": None, "ret": 0.1}] * 20) is None)
    check("片方だけ欠けても除外",
          l01([{"mcap": 1e9, "ret": None}] * 20) is None)

    # compute の契約
    r = compute({"Autos": mem, "Tiny": [{"mcap": 1e9, "ret": 0.1}] * 3})
    check("業種ごとに値を返す", set(r) == {"Autos", "Tiny"})
    check("**作れた業種は値を持つ**", r["Autos"]["L01"].value is not None)
    check("**作れない業種は理由を持つ**",
          r["Tiny"]["L01"].value is None and r["Tiny"]["L01"].reason)

    # 設計上の宣言
    check("**この層は市場全体で正規化する**", SCOPE == "market")
    check("大型株は上位10銘柄（割合ではない）", TOP_K == 10)
    check("**再現 t が 3.0 以上（既存の採用規則を満たす）**",
          REPLICATED_T["L01"] >= 3.0)
    check("パラメータ一覧に L01 がある", "L01" in PARAMS)

    print("-" * 80)
    declared = 13
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
