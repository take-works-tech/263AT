#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
企業ごとの知識を**追記だけで**貯める。ニュース・技術・論文・規制・国の資料。

なぜ要るか
----------
公表された数値だけを使ったシステムは、**実測で S&P500 に年5pp 負けた**
（docs/05 §4.7）。OSAP には同じデータ上の公表アノマリーが200本以上ある。
**最も混雑した情報を使っているのだから当然である。**

差別化があるとすれば、**決算数値になる前の情報**にある。
技術動向・論文・規制・国の調達・競合の動き —
これらは数値化されていないので、機械的なパイプラインでは拾えない。
**LLM が読めば拾える。**

だがそれには、**読んだことを貯めて、次に参照する**仕組みが要る。
毎回ゼロから読み直すのでは、蓄積にならない。

**この層の唯一にして絶対の規律**
--------------------------------
    **未来の知識が過去に染み出してはいけない。**

2015年のニュースを今日読めば、**その後何が起きたかを知っている。**
「この技術は有望だ」という判断は、後知恵と区別できない。
これは注意深さの問題ではなく、**原理的な制約**である。

だから仕組みで防ぐ。

| 規律 | 実装 |
|---|---|
| 過去の記録を書き換えない | **追記のみ**（JSONL、上書き API を持たない） |
| 時点 T では T までの記録だけ見る | `read_asof(t)` が `asof <= t` で切る |
| 書き換えを検出する | **ハッシュ連鎖**。1件でも直すと `verify()` が落ちる |
| 出典の公開日を持つ | `sources[].published`。**基準日より後の出典は拒否** |

**規律を人に任せない。** 人も LLM も、後から
「あの記述を直しておこう」としてしまう。

構造
----
    data/knowledge/{market}/{ticker}/log.jsonl   追記のみ
    1行 = 1件の記録:
      asof        判断の基準日。**この日までの情報だけを使った**という宣言
      written_at  実際に書いた時刻（asof より後になるのが普通）
      kind        fact（事実） / assessment（判断） / question（未解決）
      topic       technology / regulation / competition / demand / people ...
      text        本文
      sources     [{url, published, title}]
      model       どのモデルが書いたか（変えたら較正はやり直し）
      sha         この記録の内容ハッシュ
      prev        直前の記録の sha（連鎖）

**assessment と fact を分ける。**
事実は後から変わらないが、判断は変わる。
判断が変わったら**新しい記録を追記する。古いものは残す。**
「いつ考えを変えたか」自体が、後から検証すべき情報である。

自己テスト
    python src/knowledge.py
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = ROOT / "data" / "knowledge"

KINDS = ("fact", "assessment", "question")
GENESIS = "0" * 64          # 連鎖の起点


class LeakError(ValueError):
    """**未来の情報を過去に書こうとした。**

    データ不在や書式の誤りと区別する。これは**検証を壊す種類の誤り**で、
    黙って直してはいけない。
    """


@dataclasses.dataclass(frozen=True)
class Source:
    url: str
    published: str            # 公開日（ISO）。**asof より後は拒否**
    title: str = ""

    def as_dict(self) -> dict:
        return {"url": self.url, "published": self.published,
                "title": self.title}


def _sha(obj: dict) -> str:
    """内容のハッシュ。**キーの順序に依存しない**（sort_keys）。"""
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _path(market: str, ticker: str) -> pathlib.Path:
    safe = ticker.replace("/", "_").replace("\\", "_")
    return BASE / market / safe / "log.jsonl"


def read_all(market: str, ticker: str) -> list[dict]:
    f = _path(market, ticker)
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


def read_asof(market: str, ticker: str, t: str,
              kinds: tuple[str, ...] | None = None) -> list[dict]:
    """**時点 t で参照してよい記録だけ**を返す。

    `asof <= t` で切る。**これが PIT の境界そのもの。**
    生成系（スコアを作る側）は、必ずこちらを使うこと。
    `read_all` は監査用である。
    """
    rows = [r for r in read_all(market, ticker) if r["asof"] <= t]
    if kinds:
        rows = [r for r in rows if r["kind"] in kinds]
    return rows


def append(market: str, ticker: str, *, asof: str, kind: str, topic: str,
           text: str, sources: list[Source] | None = None,
           model: str = "", prompt_version: str = "",
           now: str | None = None) -> dict:
    """1件追記する。**上書きする関数は用意しない。**

    次を満たさなければ `LeakError` で落ちる。
      - `asof` が今日より後でない
      - **すべての出典の公開日が `asof` 以前**
      - `asof` が直前の記録より前でない（時間が巻き戻らない）
    """
    if kind not in KINDS:
        raise ValueError("kind は %s のいずれか" % (KINDS,))
    today = (now or dt.date.today().isoformat())[:10]
    if asof > today:
        raise LeakError("asof が未来（%s > %s）" % (asof, today))

    srcs = list(sources or [])
    for s in srcs:
        if s.published > asof:
            # **これを許すと、基準日以降のニュースを見て書けてしまう。**
            raise LeakError(
                "出典の公開日が基準日より後: %s（公開 %s > asof %s）"
                % (s.url, s.published, asof))

    prev_rows = read_all(market, ticker)
    if prev_rows and asof < prev_rows[-1]["asof"]:
        # **時間が巻き戻る記録を許すと、後から過去に差し込めてしまう。**
        raise LeakError("asof が直前の記録より前（%s < %s）"
                        % (asof, prev_rows[-1]["asof"]))
    prev = prev_rows[-1]["sha"] if prev_rows else GENESIS

    body = {
        "asof": asof, "written_at": (now or dt.datetime.now().isoformat()),
        "kind": kind, "topic": topic, "text": text,
        "sources": [s.as_dict() for s in srcs],
        "model": model, "prompt_version": prompt_version,
        "prev": prev,
    }
    rec = dict(body)
    rec["sha"] = _sha(body)

    f = _path(market, ticker)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return rec


def verify(market: str, ticker: str) -> tuple[bool, str]:
    """**過去が書き換えられていないか。**

    連鎖を辿って、各記録のハッシュと `prev` の対応を確認する。
    1件でも本文を直せば、そこから先の全部が合わなくなる。
    """
    rows = read_all(market, ticker)
    prev = GENESIS
    for i, r in enumerate(rows):
        body = {k: r[k] for k in r if k != "sha"}
        if _sha(body) != r["sha"]:
            return False, "%d 件目の本文が書き換えられている（%s）" % (i + 1,
                                                                     r["asof"])
        if r["prev"] != prev:
            return False, "%d 件目で連鎖が切れている（%s）" % (i + 1, r["asof"])
        prev = r["sha"]
    return True, "%d 件、連鎖は健全" % len(rows)


def tickers(market: str | None = None) -> list[tuple[str, str]]:
    out = []
    if not BASE.exists():
        return out
    for m in sorted(BASE.iterdir()):
        if not m.is_dir() or (market and m.name != market):
            continue
        for t in sorted(m.iterdir()):
            if (t / "log.jsonl").exists():
                out.append((m.name, t.name))
    return out


def digest(market: str, ticker: str, t: str, max_chars: int = 4000) -> str:
    """**時点 t で LLM に渡す形。** 新しい順に、字数上限まで。

    **判断（assessment）を先に、事実（fact）を後に**並べる。
    字数で切られたときに、まず判断が残る方がよい —
    事実は元の出典を辿れるが、**過去の判断は辿れない。**
    """
    rows = read_asof(market, ticker, t)
    rows.sort(key=lambda r: (r["asof"], r["written_at"]), reverse=True)
    order = {"assessment": 0, "question": 1, "fact": 2}
    rows.sort(key=lambda r: order.get(r["kind"], 9))
    buf, n = [], 0
    for r in rows:
        line = "[%s/%s/%s] %s" % (r["asof"], r["kind"], r["topic"], r["text"])
        if n + len(line) > max_chars:
            buf.append("…（%d 件を字数上限で省略）" % (len(rows) - len(buf)))
            break
        buf.append(line)
        n += len(line)
    return "\n".join(buf)


# ---------------------------------------------------------------- self-test
def _test() -> int:
    import shutil
    import tempfile
    fails, ran = [], []

    def check(nm, cond):
        ran.append(nm)
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/knowledge.py 自己テスト")
    print("-" * 80)

    global BASE
    tmp = pathlib.Path(tempfile.mkdtemp())
    old, BASE = BASE, tmp
    try:
        M, TK = "US", "TEST"
        r1 = append(M, TK, asof="2025-01-31", kind="fact", topic="technology",
                    text="固体電池の量産ラインを発表",
                    sources=[Source("http://x/1", "2025-01-20", "発表")],
                    model="m1", now="2025-02-01T10:00:00")
        r2 = append(M, TK, asof="2025-06-30", kind="assessment",
                    topic="technology", text="量産は2027年より後ろにずれる見込み",
                    model="m1", now="2025-07-01T10:00:00")
        check("追記できる", len(read_all(M, TK)) == 2)
        check("**連鎖が繋がる**", r2["prev"] == r1["sha"])
        ok, msg = verify(M, TK)
        check("**検証が通る**", ok)

        # --- PIT の境界 -----------------------------------------------------
        check("**基準日より後の記録は見えない**",
              len(read_asof(M, TK, "2025-03-31")) == 1)
        check("基準日以降なら見える", len(read_asof(M, TK, "2025-12-31")) == 2)
        check("種類で絞れる",
              len(read_asof(M, TK, "2025-12-31", ("assessment",))) == 1)

        # --- 漏れを止める ---------------------------------------------------
        try:
            append(M, TK, asof="2030-01-01", kind="fact", topic="x",
                   text="y", now="2025-07-02T10:00:00")
            check("**未来の基準日を拒否する**", False)
        except LeakError:
            check("**未来の基準日を拒否する**", True)

        try:
            append(M, TK, asof="2025-07-01", kind="fact", topic="x",
                   text="y", sources=[Source("http://x/2", "2025-08-01")],
                   now="2025-09-01T10:00:00")
            check("**基準日より後に公開された出典を拒否する**", False)
        except LeakError:
            check("**基準日より後に公開された出典を拒否する**", True)

        try:
            append(M, TK, asof="2025-03-01", kind="fact", topic="x",
                   text="後から過去に差し込む", now="2025-09-01T10:00:00")
            check("**時間が巻き戻る記録を拒否する**", False)
        except LeakError:
            check("**時間が巻き戻る記録を拒否する**", True)

        check("拒否された記録は書かれていない", len(read_all(M, TK)) == 2)

        # --- 書き換えの検出 -------------------------------------------------
        f = _path(M, TK)
        rows = read_all(M, TK)
        rows[0]["text"] = "**後から書き換えた本文**"
        f.write_text("\n".join(json.dumps(r, ensure_ascii=False)
                               for r in rows) + "\n", encoding="utf-8")
        ok2, msg2 = verify(M, TK)
        check("**本文を書き換えたら検証が落ちる**", not ok2)
        check("どこで落ちたか分かる", "1 件目" in msg2)

        # --- digest ---------------------------------------------------------
        shutil.rmtree(tmp / M / TK)
        append(M, TK, asof="2025-01-31", kind="fact", topic="a",
               text="事実A", now="2025-02-01T10:00:00")
        append(M, TK, asof="2025-02-28", kind="assessment", topic="b",
               text="判断B", now="2025-03-01T10:00:00")
        dg = digest(M, TK, "2025-12-31")
        check("**判断が事実より先に来る**", dg.index("判断B") < dg.index("事実A"))
        check("基準日で切られる", "判断B" not in digest(M, TK, "2025-01-31"))
        check("字数上限で省略を明示する", "省略" in digest(M, TK, "2025-12-31",
                                                          max_chars=5))

        check("銘柄を列挙できる", (M, TK) in tickers())
        check("市場で絞れる", tickers("JP") == [])

        # 空でも落ちない
        check("記録が無くても空を返す", read_asof(M, "NONE", "2030-01-01") == [])
        ok3, _ = verify(M, "NONE")
        check("記録が無ければ検証は通る", ok3)
    finally:
        BASE = old
        shutil.rmtree(tmp, ignore_errors=True)

    print("-" * 80)
    declared = 19
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
