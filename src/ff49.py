#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
SIC → Fama-French 49業種 の対応（米国の業種分類）。

spec §4.1（2026-08-23）で「**米国＝Fama-French 49業種（SIC から機械変換）**」と
決めたが、変換表そのものは Phase 1 に先送りしていた。これがその実装。

**定義は一次資料から取る。記憶や再入力に頼らない。**

    https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/Siccodes49.zip

49業種 × 数百の SIC レンジを手で書き写すと、必ずどこかで間違える。
しかも**間違えても動くので気づけない** — 業種内ランクが少しずつずれるだけで、
バックテストは普通に走る。§8 の「静かに壊れる」誤りの典型なので、
**取得したファイルをそのまま解析して使い、ハッシュで固定する。**

なぜ GICS ではなく FF49 か（§4.1 の再掲）
----------------------------------------
GICS は S&P/MSCI のライセンス製品で、**日本株の全銘柄分を無料で得る手段が無い。**
「日米で揃う」という GICS の利点は実現しないので、揃わない前提で設計した。
→ **日本＝東証33業種、米国＝FF49。rank_sector は市場内で閉じる。**

FF49 は学術標準で、OSAP のシグナルも同じ分類で構築されている。
**再現 t（§1.9.8）と同じ土俵に乗る**という副次的な利点がある。

粗い分類（§4.1 のフォールバック先）
----------------------------------
FF49 が 30 社に届かないときは **FF12** に落とす。
FF12 も同じ Ken French のサイトから取れるが、
**FF49 → FF12 の対応は SIC を経由せずに直接持つ方が安全**なので、
`COARSE` に手で書いた（49 → 12 の写像なので目視で検証できる規模）。
**自己テストが「COARSE が49業種すべてを覆う」ことを検査する** —
実際、最初に書いたときに `Comps` という存在しない略号を入れて
`Hardw` / `Softw` を落としており、そのテストが検出した。

実データで確認した意外な対応（**記憶で決めていたら間違えていた**）
------------------------------------------------------------
| SIC | 内容 | FF49 | 注意 |
|---|---|---|---|
| 6798 | REIT | **`Fin`（Trading）** | **`RlEst` ではない。** FF49 の `RlEst` は 6500-6553 の不動産業で、REIT は金融側に入る |
| 7372 | ソフトウェア | `Softw` | `BusSv` ではない。FF49 は Hardw / Softw / Chips を分けて持つ |
| 3674 | 半導体 | `Chips` | 業種名は "Electronic Equipment"（"Semiconductor" ではない） |

→ **U-2（不動産・REIT）の U09-U14 を米国株に適用するときは、
`RlEst` だけでなく `Fin` の一部も見る必要がある。**
日本の東証33業種では「不動産業」に REIT は入らない（REIT は別枠の投資法人）ので、
**日米で U-2 の母集団の作り方が違う。**

使い方
    python src/ff49.py                # 自己テスト
    python src/ff49.py --fetch        # 定義を取得してキャッシュ
    python src/ff49.py --sic 3674     # 引いてみる
"""
from __future__ import annotations

import argparse
import hashlib
import io
import pathlib
import re
import sys
import urllib.request
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "ff49"
URL = ("https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/"
       "Siccodes49.zip")
UA = {"User-Agent": "263AT research contact: tzero30208@gmail.com"}

# FF49 → FF12（粗い分類）。**§4.1 のフォールバック先。**
# 49 → 12 なので目視で検証できる。SIC を経由しないのは、
# 経由すると2つの独立した対応表がずれる可能性があるため。
COARSE = {
    "Agric": "NoDur", "Food": "NoDur", "Soda": "NoDur", "Beer": "NoDur",
    "Smoke": "NoDur", "Toys": "Durbl", "Fun": "Durbl", "Books": "NoDur",
    "Hshld": "NoDur", "Clths": "NoDur", "Hlth": "Hlth", "MedEq": "Hlth",
    "Drugs": "Hlth", "Chems": "Chems", "Rubbr": "Manuf", "Txtls": "NoDur",
    "BldMt": "Manuf", "Cnstr": "Manuf", "Steel": "Manuf", "FabPr": "Manuf",
    "Mach": "Manuf", "ElcEq": "Manuf", "Autos": "Durbl", "Aero": "Manuf",
    "Ships": "Manuf", "Guns": "Manuf", "Gold": "Enrgy", "Mines": "Enrgy",
    "Coal": "Enrgy", "Oil": "Enrgy", "Util": "Utils", "Telcm": "Telcm",
    "PerSv": "Other", "BusSv": "BusEq", "Hardw": "BusEq", "Softw": "BusEq",
    "Chips": "BusEq",
    "LabEq": "BusEq", "Paper": "Manuf", "Boxes": "Manuf", "Trans": "Manuf",
    "Whlsl": "Shops", "Rtail": "Shops", "Meals": "Shops", "Banks": "Money",
    "Insur": "Money", "RlEst": "Money", "Fin": "Money", "Other": "Other",
}

_TABLE: list[tuple[int, int, str]] | None = None      # (lo, hi, abbrev)
_NAMES: dict[str, str] = {}


def _cache_file() -> pathlib.Path:
    return CACHE / "Siccodes49.txt"


def fetch(force: bool = False) -> str:
    """定義を取得してキャッシュする。**内容のハッシュを出す。**

    Ken French のファイルは稀に更新される。
    ハッシュが変わったら**業種分類が変わった**ということなので、
    バックテスト結果の比較可能性が切れる。記録しておく。
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    f = _cache_file()
    if force or not f.exists():
        b = urllib.request.urlopen(
            urllib.request.Request(URL, headers=UA), timeout=60).read()
        z = zipfile.ZipFile(io.BytesIO(b))
        f.write_bytes(z.read(z.namelist()[0]))
    return f.read_text(encoding="latin-1")


def digest() -> str | None:
    f = _cache_file()
    return hashlib.sha256(f.read_bytes()).hexdigest()[:16] if f.exists() else None


def parse(text: str) -> tuple[list[tuple[int, int, str]], dict[str, str]]:
    """Siccodes49.txt を解析する。

    形式:
        ` 1 Agric  Agriculture`             ← 業種の見出し（番号 略号 名称）
        `        0100-0199 説明`            ← SIC のレンジ（インデントあり）
    """
    table: list[tuple[int, int, str]] = []
    names: dict[str, str] = {}
    cur: str | None = None
    head = re.compile(r"^\s*(\d{1,2})\s+([A-Za-z]+)\s+(.*?)\s*$")
    rng = re.compile(r"^\s+(\d{4})-(\d{4})")
    for ln in text.splitlines():
        if not ln.strip():
            continue
        m = rng.match(ln)
        if m and cur:
            table.append((int(m.group(1)), int(m.group(2)), cur))
            continue
        m = head.match(ln)
        if m and not ln.startswith("    "):
            cur = m.group(2)
            names[cur] = m.group(3)
    return table, names


def _ensure() -> None:
    global _TABLE, _NAMES
    if _TABLE is None:
        _TABLE, _NAMES = parse(fetch())


def industry(sic: int | str | None) -> str | None:
    """SIC コードから FF49 の略号を返す。

    **どのレンジにも入らない SIC は "Other"** にする（FF の規約）。
    **None は「未取得」** で、"Other" とは意味が違う —
    normalize 側では同じく欠損になるが、
    **Z01 のフラグで理由を区別できるようにする必要がある。**
    """
    if sic is None or str(sic).strip() in ("", "nan"):
        return None
    try:
        s = int(float(sic))
    except (TypeError, ValueError):
        return None
    _ensure()
    for lo, hi, ab in _TABLE:                # type: ignore[union-attr]
        if lo <= s <= hi:
            return ab
    return "Other"


def coarse(abbrev: str | None) -> str | None:
    """FF49 → FF12。§4.1 のフォールバック先。"""
    return COARSE.get(abbrev) if abbrev else None


def name(abbrev: str | None) -> str | None:
    if not abbrev:
        return None
    _ensure()
    return _NAMES.get(abbrev)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-58s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/ff49.py 自己テスト")
    print("-" * 72)

    if not _cache_file().exists():
        print("  定義が未取得。`python src/ff49.py --fetch` を先に実行する")
        print("  （ネットワークを黙って叩かないため、自己テストでは取得しない）")
        return 0

    _ensure()
    check("49業種すべてが読めた", len(_NAMES) == 49)
    check("SIC レンジが数百件ある", len(_TABLE) > 300)          # type: ignore[arg-type]
    check("**COARSE が49業種すべてを覆う**", set(COARSE) == set(_NAMES))
    check("FF12 は12種類", len(set(COARSE.values())) == 12)

    # 代表的な SIC。**AMD の 3674 は Chips**（DERA から実際に出てきた値）
    check("3674（半導体）→ Chips", industry(3674) == "Chips")
    check("2834（医薬品）→ Drugs", industry(2834) == "Drugs")
    # **記憶ではなく実データで確認した期待値。**
    check("**6798（REIT）→ Fin。RlEst ではない**", industry(6798) == "Fin")
    check("7372（ソフトウェア）→ Softw", industry(7372) == "Softw")
    check("FF49 は Hardw / Softw / Chips を分けて持つ",
          {"Hardw", "Softw", "Chips"} <= set(_NAMES))
    check("2810（工業化学）→ Chems", industry(2810) == "Chems")
    check("5172（石油卸）→ Oil または Whlsl",
          industry(5172) in ("Oil", "Whlsl"))

    check("**どのレンジにも入らない SIC は Other**", industry(9999) == "Other")
    check("**None は None のまま（Other に丸めない）**", industry(None) is None)
    check("空文字も None", industry("") is None)
    check("文字列の SIC も引ける", industry("3674") == "Chips")
    check("float の SIC も引ける", industry(3674.0) == "Chips")

    check("粗い分類に落とせる", coarse("Chips") == "BusEq")
    check("Banks → Money", coarse("Banks") == "Money")
    check("None は None", coarse(None) is None)
    check("業種名が引ける", "Electronic" in (name("Chips") or ""))
    check("**定義ファイルのハッシュを記録できる**", len(digest() or "") == 16)

    # レンジの重なり検査。**重なっていると先に書かれた方が勝つ**ので、
    # 意図しない重なりがあれば分類が定義順に依存してしまう
    overlaps = 0
    tbl = sorted(_TABLE)                                       # type: ignore[arg-type]
    for (a1, b1, n1), (a2, b2, n2) in zip(tbl, tbl[1:]):
        if a2 <= b1 and n1 != n2:
            overlaps += 1
    check("SIC レンジが業種間で重ならない（重なり %d 件）" % overlaps, overlaps == 0)

    print("-" * 72)
    total = 23
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true")
    ap.add_argument("--sic", type=int)
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    if args.fetch:
        fetch(force=True)
        _ensure()
        print("取得した: %d業種 / %d レンジ  sha256[:16]=%s"
              % (len(_NAMES), len(_TABLE), digest()))       # type: ignore[arg-type]
        return 0
    if args.sic is not None:
        ab = industry(args.sic)
        print("SIC %d → %s（%s） 粗い分類 %s" % (args.sic, ab, name(ab), coarse(ab)))
        return 0
    return _test()


if __name__ == "__main__":
    raise SystemExit(main())
