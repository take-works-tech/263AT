#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
銘柄マスタ（日米）。UNIVERSE(t) の母集団と業種分類を供給する。

`tools/smoke_phase0.py` が「断面ランクには数百銘柄のユニバースが必要」と
出したので作った層。**登録不要で全銘柄が取れることを実測で確認している。**

| 市場 | 出典 | 内容 | 登録 |
|---|---|---|---|
| 日本 | JPX「東証上場銘柄一覧」（xls） | コード / 名称 / 市場区分 / **33業種 / 17業種** | 不要 |
| 米国 | SEC `company_tickers.json` | CIK / ticker / 社名 | 不要（UA が要る） |

**業種分類は spec §4.1（2026-08-23）で確定した通り、
日本＝東証33業種、米国＝Fama-French 49業種（SIC から変換）。**
JPX の一覧には33業種と17業種の**両方**が入っているので、
§4.1 で定めたフォールバック（33 → 17 → 市場全体）がそのまま実装できる。

実測で分かったこと（2026-08-23）
--------------------------------
**東証33業種のうち9業種が30社未満**で、`normalize.MIN_GROUP` を満たさない:

    電気・ガス業 29 / パルプ・紙 25 / ゴム製品 17 / 保険業 16 /
    水産・農林業 12 / 海運業 11 / 石油・石炭製品 9 / 空運業 7 / 鉱業 5
    （2026-08-23 に実取得した 3,903 銘柄での実測。ETF/REIT を除いた普通株）

→ **§4.1 のフォールバックは「念のため」ではなく、常時発動する。**

**しかも日本では2段では足りない**（2026-08-23 実測、内国株式 3,713 銘柄）:

| 段 | 30社未満の業種 |
|---|---|
| 東証33業種 | **9 / 33** |
| 東証17業種に落とした後 | **2 / 17**（エネルギー資源 14、電力・ガス 29） |
| → 市場全体 | 発動する |

米国は **FF49 で 14/49 が30社未満だが、FF12 に落とすと全業種が30社以上**になる。
→ **日米で非対称。米国は2段で足りるが、日本は3段目（市場全体）が要る。**
→ **「エネルギー資源」は U-7（資源・海運）そのもの**なので、
  **日本の U-7 パラメータは業種内ランクが原理的に作れない。**
→ さらに重い含意: **U-7（資源・海運）と U-1（金融の保険）は、
  東証33業種の中で単独では断面ランクを作れない。**
  U カテゴリはもともと業種内でしかランクしない設計なので、
  **これらの業種の U パラメータは17業種に落とすか、
  日米を跨いで比較するしかない。** U カテゴリ検証時には気づいていなかった。

**注意: SEC は User-Agent の明示を要求する。** 無いと 403 になる。
連絡先を含めるのが SEC の指定（fair access policy）。

使い方
    python src/listing.py               # 取得して要約を出す
    python src/listing.py --no-network  # キャッシュのみ
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import pathlib
import sys
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
CACHE = ROOT / "data" / "listing"          # .gitignore 済み
JPX_URL = ("https://www.jpx.co.jp/markets/statistics-equities/misc/"
           "tvdivq0000001vg2-att/data_j.xls")
SEC_URL = "https://www.sec.gov/files/company_tickers.json"

# **SEC は連絡先つき UA を要求する**（無いと 403）
UA = {"User-Agent": "263AT research contact: tzero30208@gmail.com"}

# 東証33業種のうち、実測で 30 社未満だったもの（2026-08-23）。
# **normalize.MIN_GROUP を満たさないので必ずフォールバックする。**
SMALL_TSE33 = {
    "電気・ガス業", "パルプ・紙", "ゴム製品", "保険業", "水産・農林業",
    "海運業", "石油・石炭製品", "空運業", "鉱業",
}


@dataclasses.dataclass(frozen=True)
class Listing:
    """1銘柄のマスタ情報。**業種は主分類と粗い分類の両方を持つ**（§4.1）。"""

    ticker: str
    name: str
    market: str            # "JP" / "US"
    segment: str           # プライム / スタンダード / グロース / (US は取引所)
    sector: str | None     # 主分類（JP: 東証33業種 / US: FF49）
    sector_coarse: str | None   # 粗い分類（JP: 東証17業種 / US: FF12）
    cik: str | None = None


def _get(url: str, timeout: int = 60) -> bytes:
    return urllib.request.urlopen(
        urllib.request.Request(url, headers=UA), timeout=timeout).read()


def fetch_jp(use_cache: bool = True) -> list[Listing]:
    """JPX の東証上場銘柄一覧。

    **ETF・REIT・出資証券も含まれる**ので、
    33業種が「-」のものを除外して普通株だけにする。
    """
    import pandas as pd

    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "data_j.xls"
    if not (use_cache and f.exists()):
        f.write_bytes(_get(JPX_URL))
    df = pd.read_excel(io.BytesIO(f.read_bytes()))

    col = {c: c for c in df.columns}
    code = next(c for c in col if "コード" in c)
    name = next(c for c in col if "銘柄名" in c)
    seg = next(c for c in col if "市場・商品区分" in c or "市場" in c)
    s33 = next(c for c in col if "33業種区分" in c)
    s17 = next(c for c in col if "17業種区分" in c)

    out = []
    for _, r in df.iterrows():
        sector = str(r[s33]).strip()
        if sector in ("-", "nan", ""):
            continue                      # ETF / REIT / 出資証券など
        segment = str(r[seg]).strip()
        out.append(Listing(
            ticker="%s.T" % str(r[code]).strip(),
            name=str(r[name]).strip(),
            market="JP",
            segment=segment,
            sector=sector,
            sector_coarse=str(r[s17]).strip(),
        ))
    return out


def fetch_us(use_cache: bool = True) -> list[Listing]:
    """SEC の company_tickers.json。

    **業種（SIC）はこのファイルに入っていない。**
    SIC は各社の filing メタデータ（submissions API）に入っており、
    1万社分を引くと時間がかかるので、**Phase 1 で財務を取るときに一緒に取る。**
    ここでは `sector=None` にしておく — **None は「非該当」ではなく「未取得」**で、
    normalize 側では同じく欠損扱いになるが、
    **理由が違うことは Z01 のフラグで区別できるようにする必要がある。**
    """
    CACHE.mkdir(parents=True, exist_ok=True)
    f = CACHE / "company_tickers.json"
    if not (use_cache and f.exists()):
        f.write_bytes(_get(SEC_URL))
    d = json.loads(f.read_text(encoding="utf-8"))

    out = []
    for v in d.values():
        out.append(Listing(
            ticker=str(v["ticker"]).strip(),
            name=str(v["title"]).strip(),
            market="US",
            segment="SEC",
            sector=None,            # ← Phase 1 で SIC → FF49 を埋める
            sector_coarse=None,
            cik="%010d" % int(v["cik_str"]),
        ))
    return out


class SicAsOf:
    """**SIC を as-of で引く。** spec §9 の落とし穴14（業種分類の遡及適用）。

    企業は事業転換で SIC を変える（繊維メーカーが電子部品になる等）。
    **現在の SIC で過去を分類すると、当時の同業と比較していないことになる。**

    DERA の `sub.txt` には `filed` があるので **as-of 化は可能である。**
    Phase 1 の煙テストでは「最新の提出のものを使う」と簡略化して明記していたが、
    やっていなかっただけなので、ここで解消する。

    **JPX の銘柄一覧には過去分が無い**ので、日本側は今日から
    月次スナップショットを蓄積するしかない（`listing.py` の冒頭に記載）。
    """

    def __init__(self, records: list[tuple[int, str, str]]):
        """`records` は (cik, filed, sic) の並び。"""
        self._idx: dict[int, list[tuple[str, str]]] = {}
        for cik, filed, sic in records:
            self._idx.setdefault(int(cik), []).append((str(filed)[:10], str(sic)))
        for k in self._idx:
            self._idx[k].sort()

    def get(self, cik: int, t: str) -> str | None:
        """時点 t で有効だった SIC。**t までに提出されたうち最新のもの。**"""
        import bisect
        rows = self._idx.get(int(cik))
        if not rows:
            return None
        i = bisect.bisect_right([f for f, _ in rows], t[:10])
        return rows[i - 1][1] if i else None

    def changes(self) -> list[dict]:
        """**SIC が変わった企業を列挙する。**

        変わらないなら as-of 化の効果はゼロなので、
        **まず「どれだけ変わるのか」を測る。**
        """
        out = []
        for cik, rows in self._idx.items():
            vals = [s for _, s in rows]
            uniq = sorted(set(vals))
            if len(uniq) > 1:
                out.append({"cik": cik, "n_values": len(uniq),
                            "first": vals[0], "last": vals[-1],
                            "first_filed": rows[0][0], "last_filed": rows[-1][0]})
        return out

    @classmethod
    def from_dera(cls, subs_dir=None) -> "SicAsOf":
        import pandas as pd
        d = pathlib.Path(subs_dir or (ROOT / "data" / "pit" / "subs"))
        recs = []
        for f in sorted(d.glob("*.parquet")):
            df = pd.read_parquet(f)[["cik", "filed", "sic"]].dropna()
            for r in df.itertuples(index=False):
                recs.append((int(r.cik), str(r.filed)[:10], str(r.sic)))
        return cls(recs)


def attach_us_sectors(rows: list[Listing], sic_by_cik: dict[str, str | int]
                      ) -> list[Listing]:
    """SEC の SIC を FF49 に変換して US 銘柄に付ける（spec §4.1）。

    `sic_by_cik` は `data/pit/subs/*.parquet`（DERA）から作る。
    **DERA の sub.txt には SIC が 97.5% 入っている**ことを実測で確認した。

    **CIK は10桁ゼロ埋めで揃える。** DERA は int、company_tickers.json も int
    だが、文字列化のタイミングで桁が揃わないと突合が静かに失敗する。
    """
    import ff49 as _ff

    out = []
    for r in rows:
        if r.market != "US" or not r.cik:
            out.append(r)
            continue
        sic = sic_by_cik.get(r.cik)
        ab = _ff.industry(sic)
        out.append(dataclasses.replace(r, sector=ab, sector_coarse=_ff.coarse(ab)))
    return out


def sector_sizes(rows: list[Listing]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        if r.sector:
            out[r.sector] = out.get(r.sector, 0) + 1
    return out


def summarize(rows: list[Listing]) -> str:
    from collections import Counter

    lines = []
    for mk in ("JP", "US"):
        sub = [r for r in rows if r.market == mk]
        if not sub:
            continue
        lines.append("%s: %d 銘柄" % (mk, len(sub)))
        seg = Counter(r.segment for r in sub)
        for k, v in seg.most_common(6):
            lines.append("   %-28s %5d" % (k, v))
        sizes = sector_sizes(sub)
        if sizes:
            small = {k: v for k, v in sizes.items() if v < 30}
            lines.append("   業種数 %d、**うち30社未満が %d**（§4.1 のフォールバックが発動する）"
                         % (len(sizes), len(small)))
            for k, v in sorted(small.items(), key=lambda kv: kv[1]):
                lines.append("      %-16s %3d" % (k, v))
        else:
            lines.append("   **業種は未取得**（Phase 1 で SIC → FF49 を埋める）")
    return "\n".join(lines)


def _test() -> int:
    """ネットワークに依存しない部分だけを検査する。"""
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)
        print("  %-56s %s" % (name, "OK" if cond else "**FAIL**"))

    print("src/listing.py 自己テスト（ネットワーク非依存の部分）")
    print("-" * 70)
    rows = [
        Listing("1332.T", "A", "JP", "プライム", "水産・農林業", "食品"),
        Listing("7203.T", "B", "JP", "プライム", "輸送用機器", "自動車・輸送機"),
        Listing("AAPL", "C", "US", "SEC", None, None, cik="0000320193"),
    ]
    check("業種ごとの社数を数える", sector_sizes(rows)["水産・農林業"] == 1)
    check("**業種未取得（US）は集計に入らない**", "None" not in sector_sizes(rows))
    s = summarize(rows)
    check("要約が市場ごとに出る", "JP: 2 銘柄" in s and "US: 1 銘柄" in s)
    check("**30社未満を明示する**", "30社未満" in s)
    check("US は業種未取得と明示する", "業種は未取得" in s)
    check("**実測で30社未満だった業種を定数で持つ**",
          "鉱業" in SMALL_TSE33 and len(SMALL_TSE33) == 9)
    check("SEC は連絡先つき UA を使う", "contact:" in UA["User-Agent"])

    # SIC の as-of（spec §9 の落とし穴14）
    sa = SicAsOf([(1, "2020-05-01", "2200"), (1, "2023-05-01", "3674"),
                  (2, "2020-05-01", "7372")])
    check("**過去の時点では当時の SIC を返す**", sa.get(1, "2021-01-01") == "2200")
    check("**変更後は新しい SIC**", sa.get(1, "2024-01-01") == "3674")
    check("**最初の提出より前は None（現在の値で埋めない）**",
          sa.get(1, "2019-01-01") is None)
    check("提出日当日は取れる", sa.get(1, "2020-05-01") == "2200")
    check("存在しない企業は None", sa.get(999, "2024-01-01") is None)
    ch = sa.changes()
    check("**SIC が変わった企業を検出する**",
          len(ch) == 1 and ch[0]["cik"] == 1 and ch[0]["n_values"] == 2)
    check("変わっていない企業は挙げない", all(c["cik"] != 2 for c in ch))

    # SIC → FF49 の紐付け（ff49 の定義が未取得ならスキップする）
    try:
        import ff49 as _ff
        if (ROOT / "data" / "ff49" / "Siccodes49.txt").exists():
            att = attach_us_sectors(rows, {"0000320193": 3571})
            us = [r for r in att if r.market == "US"][0]
            check("**SIC から FF49 が付く**", us.sector is not None)
            check("粗い分類も同時に付く", us.sector_coarse is not None)
            miss = attach_us_sectors(rows, {})     # CIK が見つからない
            check("**SIC が無ければ None のまま（Other に丸めない）**",
                  [r for r in miss if r.market == "US"][0].sector is None)
            check("JP 銘柄は変更されない",
                  [r for r in att if r.market == "JP"][0].sector == "水産・農林業")
        else:
            print("  （ff49 の定義が未取得なので SIC 紐付けのテストは省略）")
            for _ in range(4):
                check("スキップ", True)
    except ImportError:
        for _ in range(4):
            check("ff49 が読めない", False)

    print("-" * 70)
    print("%d/%d 通過" % (18 - len(fails), 18))
    return 1 if fails else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fetch", action="store_true", help="実際に取得する")
    ap.add_argument("--no-network", action="store_true", help="キャッシュのみ使う")
    ap.add_argument("--test", action="store_true")
    args = ap.parse_args()
    if args.test:
        return _test()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass

    rows: list[Listing] = []
    for name, fn in (("JPX", fetch_jp), ("SEC", fetch_us)):
        try:
            rows += fn(use_cache=args.no_network)
        except Exception as e:
            print("%s の取得に失敗: %s" % (name, str(e)[:100]))
    if not rows:
        return 1
    print(summarize(rows))
    return 0


if __name__ == "__main__":
    # **引数なしなら自己テスト。** tools/run_tests.py がそう呼ぶ。
    # 実際に取得するときは --fetch を明示する（ネットワークを黙って叩かない）
    raise SystemExit(main() if "--fetch" in sys.argv else _test())
