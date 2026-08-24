#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**どの名前がどの企業か**を解く。外部データを使うための前提。

なぜ最も難しいか
----------------
`src/extern.py` の疎通で出た（2026-08-24）:

    論文 Moderna: 2015年末まで **3,214 件**

Moderna は2010年創業。ありえない。
**`moderna` はスペイン語・イタリア語で「現代の」という一般語**で、
所属文字列の検索がそれを拾っていた。

**機関ID（ROR）で引き直すと 47 件**になった。妥当な数である。

だが ROR にすると**次の問題が出る。**

    Nvidia (United States)   ror=03jdj4y14  works=5606
    Nvidia (United Kingdom)  ror=02kr42612  works=5041
    NVIDIA (Italy)           ror=04z2hsy54  works=0

**同じ企業が国ごとに別の ID を持つ。** 1つだけ使うと半分になる。
Vertex も US/UK/Canada の3つに分かれていた。

→ **候補を集めて、同じ企業のものを束ねる。**

照合は汚染されない
------------------
「この会社は有望か」という判断は、モデルの重みに未来が焼き付いていて
後知恵と区別できない。

**「この所属文字列はこの企業か」は違う。**
答えは文字列と設立年と国の中にあり、**将来のリターンとは無関係**である。
だから**過去に遡って解いてよい。**

ただし**解いた結果は記録する**（`knowledge.py` に fact として追記）。
一度解けた対応を毎回引き直すと、
**同じ入力に違う答えが出たときに気づけない。**

自己テスト
    python src/entity.py            # ネットワーク非依存
    python src/entity.py --probe NVDA MRNA VRTX
"""
from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request

UA = {"User-Agent": "263AT/1.0 (tzero30208@gmail.com)"}
MAILTO = "tzero30208@gmail.com"

# 社名の末尾に付く語。**照合の前に落とす。**
# これを残すと "NVIDIA CORP" と "Nvidia" が別物に見える。
SUFFIXES = (
    "corporation", "corp", "incorporated", "inc", "company", "co",
    "limited", "ltd", "plc", "llc", "lp", "holdings", "holding",
    "group", "the", "sa", "nv", "ag", "se", "kk", "kabushiki", "kaisha",
    "class a", "class b", "common stock", "ordinary shares",
)

# **一般語と衝突する社名。** 生の文字列検索では使えない。
# ここに載っているものは、**機関ID での照合を必須にする。**
AMBIGUOUS = {
    "moderna", "apple", "amazon", "square", "block", "target", "gap",
    "shell", "total", "unity", "match", "sky", "arm", "box", "chase",
    "visa", "next", "now", "open", "wave", "peak", "summit", "atlas",
}


def normalize(name: str) -> str:
    """社名を照合しやすい形にする。**末尾の法人格を落とす。**

    SEC のマスタは "VERTEX PHARMACEUTICALS INC / MA" のように
    **州コードを `/` の後ろに付ける。** これも落とす。
    """
    s = (name or "").lower()
    s = re.sub(r"\s*/\s*[a-z]{2}\s*$", " ", s)      # 末尾の州コード
    s = re.sub(r"[^\w\s&-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    changed = True
    while changed:
        changed = False
        for suf in SUFFIXES:
            if s.endswith(" " + suf) or s == suf:
                s = s[: len(s) - len(suf)].strip()
                changed = True
    return s


def is_ambiguous(name: str) -> bool:
    """**生の文字列検索で使ってはいけない社名か。**"""
    return normalize(name) in AMBIGUOUS


def similarity(a: str, b: str) -> float:
    """0〜1。**完全一致・前方一致・語の重なり**の順で見る。

    編集距離を使わないのは、**社名では語の入れ替えより
    語の有無の方が意味を持つ**から
    （"Vertex Pharmaceuticals" と "Vertex Energy" は別会社）。
    """
    x, y = normalize(a), normalize(b)
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    xs, ys = set(x.split()), set(y.split())
    inter = xs & ys
    if not inter:
        return 0.0
    j = len(inter) / len(xs | ys)
    # 片方が他方を完全に含むなら底上げする（"nvidia" ⊂ "nvidia research"）
    if xs <= ys or ys <= xs:
        j = max(j, 0.75)
    return j


def _get(url: str, timeout: int = 45, retries: int = 5) -> dict:
    """**429（レート制限）は待って再試行する。**

    最初の一括取得で **3,734件中 3,663件が 429** になり、
    論文データがほぼ全滅した（2026-08-24）。
    しかも**失敗した記録をキャッシュ済みとして保存していた**ので、
    再実行しても飛ばされる状態だった。

    **待てば通るものを、失敗として確定させてはいけない。**
    """
    import time as _t
    import urllib.error as _e
    delay = 1.0
    for k in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except _e.HTTPError as ex:
            if ex.code not in (429, 503) or k == retries - 1:
                raise
            _t.sleep(delay)
            delay = min(delay * 2, 30.0)
    raise RuntimeError("再試行を使い切った")


def openalex_candidates(name: str, per_page: int = 20) -> list[dict]:
    """OpenAlex の機関を検索して候補を返す。**企業だけに絞る。**"""
    # **正規化してから検索する。**
    # OpenAlex の search は全語 AND なので、"NVIDIA CORP" だと
    # 機関名に "corp" が無くて **0件になる**（実際にそうなった）。
    term = normalize(name) or name
    q = urllib.parse.urlencode({"search": term, "per-page": str(per_page),
                                "mailto": MAILTO})
    d = _get("https://api.openalex.org/institutions?" + q)
    out = []
    for r in d.get("results") or []:
        if r.get("type") != "company":
            # **大学や図書館を拾わない。** Moderna の事故はこれで防げる
            continue
        out.append({
            "ror": r.get("ror"), "id": r.get("id"),
            "name": r.get("display_name"),
            "country": r.get("country_code"),
            "works": r.get("works_count", 0),
            "similarity": similarity(name, r.get("display_name") or ""),
        })
    out.sort(key=lambda x: (-x["similarity"], -x["works"]))
    return out


def resolve_openalex(legal_name: str, min_sim: float = 0.75,
                     max_orgs: int = 6) -> dict:
    """**同じ企業の機関IDをまとめて返す。**

    国ごとに別 ID になっているので、**似ている候補を全部束ねる。**
    束ねなければ Nvidia は US だけで半分になる。

    `min_sim` を下げると別会社を巻き込む
    （"Vertex Pharmaceuticals" と "Vertex Energy"）。
    **緩めるより、取り逃す方を選ぶ。**
    """
    cands = openalex_candidates(legal_name)
    keep = [c for c in cands if c["similarity"] >= min_sim][:max_orgs]
    return {
        "query": legal_name,
        "rors": [c["ror"] for c in keep if c.get("ror")],
        "matched": keep,
        "rejected": [c for c in cands if c["similarity"] < min_sim][:5],
        "note": ("**候補なし**" if not keep else
                 "%d 件を同一企業として束ねた（国別に分かれているため）"
                 % len(keep)),
    }


def papers_asof(rors: list[str], asof: str) -> int:
    """**束ねた機関IDで**、その日までの論文数を数える。

    `cited_by_count` は使わない（今日までの累積なので）。
    """
    if not rors:
        return 0
    ids = "|".join(rors)
    q = urllib.parse.urlencode({
        "filter": "institutions.ror:%s,to_publication_date:%s" % (ids, asof),
        "per-page": "1", "mailto": MAILTO})
    d = _get("https://api.openalex.org/works?" + q)
    return (d.get("meta") or {}).get("count", 0)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/entity.py 自己テスト（ネットワーク非依存）")
    print("-" * 80)

    check("**末尾の法人格を落とす**", normalize("NVIDIA CORP") == "nvidia")
    check("複数の末尾も落とす",
          normalize("Vertex Pharmaceuticals Incorporated")
          == "vertex pharmaceuticals")
    check("記号と大小を潰す", normalize("Alphabet Inc.") == "alphabet")
    check("**株式の種類も落とす**",
          normalize("Berkshire Hathaway Inc Class B") == "berkshire hathaway")
    check("空文字でも落ちない", normalize("") == "")

    check("**同じ社名は 1.0**", similarity("NVIDIA CORP", "Nvidia") == 1.0)
    check("**別会社を高く出さない**",
          similarity("Vertex Pharmaceuticals", "Vertex Energy") < 0.75)
    check("包含関係は底上げする",
          similarity("Nvidia", "Nvidia Research") >= 0.75)
    check("共通語が無ければ 0", similarity("Apple", "Microsoft") == 0.0)

    check("**一般語と衝突する社名を知っている**", is_ambiguous("Moderna"))
    check("法人格付きでも判定できる", is_ambiguous("Moderna Inc"))
    check("衝突しない社名は False", not is_ambiguous("Vertex Pharmaceuticals"))
    check("**事故を起こした社名が入っている**", "moderna" in AMBIGUOUS)

    # **SEC のマスタは州コードを付ける。** これを落とさないと検索が 0件になる
    check("**末尾の州コードを落とす**",
          normalize("VERTEX PHARMACEUTICALS INC / MA")
          == "vertex pharmaceuticals")
    check("州コードに見える普通の語は残す",
          normalize("Tyler Technologies") == "tyler technologies")

    print("-" * 80)
    declared = 15
    if len(ran) != declared:
        fails.append("本数が宣言と違う")
        print("  **検査の本数が宣言と違う: 宣言 %d / 実際 %d**"
              % (declared, len(ran)))
    print("%d/%d 通過" % (len(ran) - len(fails), len(ran)))
    return 1 if fails else 0


def _probe(tickers: list[str]) -> int:
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    import listing as LS      # type: ignore
    by = {r.ticker: r for r in LS.fetch_us(use_cache=True)}
    print("=" * 78)
    print("照合の確認（**束ねる前と後で件数がどう変わるか**）")
    print("=" * 78)
    for tk in tickers:
        m = by.get(tk)
        if not m:
            print("\n%-6s マスタに無い" % tk)
            continue
        legal = getattr(m, "name", None) or tk
        r = resolve_openalex(legal)
        print("\n■ %-6s %s" % (tk, legal))
        for c in r["matched"]:
            print("   採用 %-34s %-4s 類似 %.2f  論文 %d"
                  % (c["name"][:34], c["country"], c["similarity"], c["works"]))
        for c in r["rejected"]:
            print("   **除外** %-30s 類似 %.2f（別会社の疑い）"
                  % (c["name"][:30], c["similarity"]))
        if r["rors"]:
            a = papers_asof(r["rors"], "2015-12-31")
            b = papers_asof(r["rors"][:1], "2015-12-31")
            print("   2015年末までの論文: **束ねて %d 件** / 先頭だけ %d 件"
                  % (a, b))
            if a > b:
                print("   → **束ねないと %d 件を取り逃していた。**" % (a - b))
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--probe" in sys.argv:
        i = sys.argv.index("--probe")
        raise SystemExit(_probe(sys.argv[i + 1:] or ["NVDA", "MRNA", "VRTX"]))
    raise SystemExit(_test())
