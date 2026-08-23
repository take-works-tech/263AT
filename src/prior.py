#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
スコアに使う本数を、**自分の成績を見ずに決める。**

なぜこれが要るか
----------------
独立観測は **58個**しかない（15年 ÷ 90日の重なり）。
そこに 35本のパラメータを当てると、**多重検定で何かは必ず光る。**
そして「光ったものを採る」と、それは検証ではなく**選択**になる（§1.9）。

Kozak-Nagel-Santosh (JFE 2020) の答えは「選択ではなく縮小」だが、
**縮小も、候補集合を成績で決めていたら同じことである。**

→ **候補集合は、自分のデータを一切見ずに決める。**

採用の規則（これだけ）
----------------------
    **他者による再現の t が 3.0 以上**（Harvey-Liu-Zhu の基準）

「他者による再現」は Open Source Asset Pricing の再現値を使う。
**原論文の t ではない。** ここは実際に間違えた場所である（2回）:

    I26 Frazzini-Pedersen ベータ  原論文 t=7.1 → **再現 t=1.19**
    I29 3ファクター残差歪度        原論文 t=4.3 → **再現 t=2.87**

カタログの備考に「OSAP `BetaFP` t=7.1」と書いてあるのは**原論文の値**で、
再現値ではない。これは §1.9.8 で一度訂正したはずの誤りを、
**params_fx.py を書くときにもう一度やった。**
再現値では**どちらも基準に届かない。**

ゲートはスコアに入れない
------------------------
J01（平均売買代金）と J10（ゼロ出来高日）は**ユニバースのゲート**である。
ゲートを通した後の断面では、
**J10 は全員 0 になるので、断面に情報が無い**（実測でも重みは 166回中 0回）。
ゲートで使ったものをスコアでもう一度使うのは二重計上である。

再現 t を持たないことは弱さの証拠ではない
----------------------------------------
§1.9.8 の通り、日本固有（K32/K33、K17）・LLM 派生・保有状態には
**OSAP に対応が存在しないだけ**で、263AT の優位性はむしろそちら側にある。

**この規則は「今このデータで測る対象」を決めるものであって、
「価値のあるパラメータ」を決めるものではない。**
前向き記録（forward_log）が貯まれば、そちらは別の証拠で判断する。

自己テスト
    python src/prior.py
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 採用の閾値。**Harvey-Liu-Zhu が要求する値をそのまま使う。**
MIN_REPLICATED_T = 3.0

# 凍結した採用集合（2026-08-24 に registry から導出）。
# **成績を見て足したり引いたりしてはいけない。**
# 自己テストが registry から再導出して、ずれたら落ちる。
ADOPTED = (
    "A03", "A04", "A06", "B02", "B06", "B22",
    "E01", "E03", "E29", "F24",
    "G01", "G02", "G04", "G10", "G32",
    "G38", "G39", "G40", "G41", "G42", "G43", "G44", "G45",
    "I04", "I08", "I27",
    "J22", "J25",
)

# 実装済みだが見送ったもの。**理由を残す**（消すと再発する）
DEFERRED = {
    "G03": "再現なし（3-1モメンタム。G01/G02 と重なる）",
    "G16": "再現なし（時系列モメンタム。符号だけなので情報が薄い）",
    "H05": "再現なし（1週間リバーサル）",
    "I01": "再現 t=2.96。**基準に僅かに届かない。** 下げない",
    "I26": "再現 t=1.19。**原論文の 7.1 と取り違えていた**",
    "I29": "再現 t=2.87。**原論文の 4.3 と取り違えていた**",
}

# ゲートとして使うのでスコアには入れないもの
AS_GATE = {
    "J01": "平均売買代金。ユニバースのゲート（min_adv_jpy）",
    "J10": "ゼロ出来高日。ゲート後は全員 0 で断面に情報が無い",
}


def derive() -> tuple[list[str], list[str], list[str]]:
    """registry から規則を当てて導出する。**凍結集合の答え合わせ用。**"""
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "vr", str(ROOT / "tools" / "validate_registry.py"))
    vr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vr)
    _, entries = vr.load()

    sys.path.insert(0, str(ROOT / "src"))
    import params_us as PU      # type: ignore
    import params_px as PX      # type: ignore
    import params_fx as FX      # type: ignore
    impl = set(PU.REGISTRY) | set(PX.PARAMS) | set(FX.PARAMS)

    adopted, deferred, gates = [], [], []
    for e in entries:
        if e["id"] not in impl:
            continue
        is_gate = (e.get("buy_class") == "gate"
                   or e.get("sell_class") == "gate")
        if is_gate:
            gates.append(e["id"])
            continue
        r = e.get("replication") or {}
        t = r.get("t_replicated")
        if t is not None and abs(float(t)) >= MIN_REPLICATED_T:
            adopted.append(e["id"])
        else:
            deferred.append(e["id"])
    return sorted(adopted), sorted(deferred), sorted(gates)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/prior.py 自己テスト")
    print("-" * 80)

    check("**閾値は Harvey-Liu-Zhu の 3.0**", MIN_REPLICATED_T == 3.0)
    check("採用集合が凍結されている（tuple）", isinstance(ADOPTED, tuple))
    check("採用と見送りが重ならない",
          not (set(ADOPTED) & set(DEFERRED)))
    check("採用とゲートが重ならない", not (set(ADOPTED) & set(AS_GATE)))
    check("**見送りには理由が付いている**",
          all(v.strip() for v in DEFERRED.values()))

    try:
        adopted, deferred, gates = derive()
    except Exception as e:
        check("**registry から再導出できる**", False)
        print("     %s" % str(e)[:120])
        adopted = deferred = gates = None

    if adopted is not None:
        # **凍結した集合が、規則の再適用と一致すること。**
        # ずれたら「成績を見て触った」か「registry が変わった」のどちらか。
        # どちらも黙って通してはいけない。
        check("**凍結集合が registry からの再導出と一致する**",
              sorted(ADOPTED) == adopted)
        if sorted(ADOPTED) != adopted:
            print("     凍結にあって導出に無い: %s"
                  % sorted(set(ADOPTED) - set(adopted)))
            print("     導出にあって凍結に無い: %s"
                  % sorted(set(adopted) - set(ADOPTED)))
        check("見送りの一覧も一致する", sorted(DEFERRED) == deferred)
        check("ゲートの一覧も一致する", sorted(AS_GATE) == gates)
        check("**採用は 28 本**", len(adopted) == 28)

    # 取り違えの記録が残っていること（**消すと再発する**）
    check("**I26 の取り違えを記録している**", "1.19" in DEFERRED.get("I26", ""))
    check("**I29 の取り違えを記録している**", "2.87" in DEFERRED.get("I29", ""))
    check("I01 は僅差でも下げない", "2.96" in DEFERRED.get("I01", ""))

    print("-" * 80)
    declared = 12
    if len(ran) != declared:
        fails.append("**検査の本数が宣言と違う（宣言 %d / 実際 %d）**"
                     % (declared, len(ran)))
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
