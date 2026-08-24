#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
**今日の生成結果を記録する。** これが唯一、交絡のない証拠になる。

なぜこれが最重要か
------------------
現在の全結論には**2つの上振れ交絡**が残っている。

  1. **生存者バイアス** — 2012年の残存率 32.6%。廃止銘柄は最初から入らない
  2. **カタログ自体のルックアヘッド** — パラメータの選定と符号を
     2024年までの OSAP を見て決めた。**2012-2026 は設計にとって全部 in-sample**

**どちらも過去データでは除去できない。**
除去できるのは「**今日決めて、将来を待つ**」ことだけである。

    今日 T に、上位N銘柄を記録する
      → **T の時点で未来は存在しない**
      → 5年後、その記録と実際のリターンを突き合わせる
      → **交絡ゼロの証拠が1個できる**

1日遅らせれば、検証の開始も1日遅れる。
**コードより先に、記録を始めるべき部品である。**

記録するもの — **後から監査できるだけ全部**
--------------------------------------------
| 何を | なぜ |
|---|---|
| `asof` / `generated_at` | いつ決めたか。**未来を見ていないことの根拠** |
| `git_commit` | **どのコードが出した答えか** |
| `prior_sha` | 採用パラメータ集合の指紋。**後から変えたら分かる** |
| `weights` | 重み。**再現できなければ検証にならない** |
| `picks` | 上位N銘柄と**スコアと順位** |
| `universe_size` / `train_obs` | どれだけの母集団から選んだか |
| `prev` | **直前の記録のハッシュ**（連鎖） |

**`realized` は記録時に必ず null。** 値が入っていたらルックアヘッドである。

書き換えを防ぐ
--------------
`knowledge.py` と同じく**追記のみ・ハッシュ連鎖**。
**過去の記録を1件でも直すと `verify()` が落ちる。**

規律を人に任せない。**5年後の自分が「あの銘柄は入れていたはず」と
書き換えられる状態では、証拠にならない。**

使い方
    .venv/Scripts/python.exe tools/record_generation.py            # 記録する
    .venv/Scripts/python.exe tools/record_generation.py --verify   # 連鎖の検証
    .venv/Scripts/python.exe tools/record_generation.py --evaluate # 実現値と突合
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import pathlib
import subprocess
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
import prices as PR           # noqa: E402
import prior as PRIOR         # noqa: E402
import shrink as SH           # noqa: E402

LOG = ROOT / "data" / "generations.jsonl"
GENESIS = "0" * 64


def _sha(obj: dict) -> str:
    s = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                   separators=(",", ":"))
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(ROOT),
                              capture_output=True, text=True,
                              timeout=15).stdout.strip()[:12]
    except Exception:
        return "unknown"


def read_all() -> list[dict]:
    if not LOG.exists():
        return []
    return [json.loads(x) for x in
            LOG.read_text(encoding="utf-8").splitlines() if x.strip()]


def verify() -> tuple[bool, str]:
    rows = read_all()
    prev = GENESIS
    for i, r in enumerate(rows):
        body = {k: v for k, v in r.items() if k != "sha"}
        if _sha(body) != r["sha"]:
            return False, "%d 件目（%s）の本文が書き換えられている" % (i + 1,
                                                                     r["asof"])
        if r["prev"] != prev:
            return False, "%d 件目（%s）で連鎖が切れている" % (i + 1, r["asof"])
        prev = r["sha"]
    return True, "%d 件、連鎖は健全" % len(rows)


def load_panel(branch: str, horizon: int) -> dict[str, list[dict]]:
    d = ROOT / "data" / "panel" / branch
    out = {}
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


def cmd_record(a) -> int:
    panel = load_panel(a.panel, a.horizon)
    if not panel:
        print("パネルが無い")
        return 1
    dates = sorted(panel)
    T = a.asof or dates[-1]
    if T not in panel:
        print("**その日付の断面が無い**: %s（最新は %s）" % (T, dates[-1]))
        return 1

    flat = [r for t in dates for r in panel[t]]
    names = sorted({k for r in flat for k in r["z"]} & set(PRIOR.ADOPTED))
    train = [r for r in flat if usable_at(r, T, a.horizon)]
    if len(train) < 2000:
        print("**訓練の観測が足りない**: %d" % len(train))
        return 1

    fit = SH.fit(train, names)
    scored = []
    for r in panel[T]:
        s = SH.score(r["z"], fit)
        if s is not None:
            scored.append((s, r["ticker"], r.get("sector")))
    if len(scored) < 100:
        print("**断面が薄すぎる**: %d 銘柄" % len(scored))
        return 1
    scored.sort(key=lambda x: -x[0])

    prev_rows = read_all()
    if prev_rows and T <= prev_rows[-1]["asof"]:
        # **時間が巻き戻る記録を許さない。** 後から過去に差し込めてしまう
        print("**直前の記録（%s）より前の日付は記録しない**: %s"
              % (prev_rows[-1]["asof"], T))
        return 1

    body = {
        "asof": T,
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "git_commit": git_commit(),
        # **採用集合の指紋。** 後から変えたら分かる
        "prior_sha": hashlib.sha256(
            ",".join(sorted(PRIOR.ADOPTED)).encode()).hexdigest()[:16],
        "params": names,
        "horizon_days": a.horizon,
        "panel": a.panel,
        "lambda": fit.lam,
        "effective_breadth": round(fit.effective_breadth, 3),
        "weights": {k: round(v, 6) for k, v in sorted(fit.weights.items())
                    if v > 1e-9},
        "universe_size": len(scored),
        "train_obs": len(train),
        "picks": [{"rank": i + 1, "ticker": tk, "sector": sec,
                   "score": round(s, 6)}
                  for i, (s, tk, sec) in enumerate(scored[:a.top_n])],
        # **記録時は必ず null。** 値が入っていたらルックアヘッド
        "realized": None,
        "prev": prev_rows[-1]["sha"] if prev_rows else GENESIS,
    }
    rec = dict(body)
    rec["sha"] = _sha(body)

    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print("=" * 78)
    print("**生成を記録した** — これが交絡のない証拠になる")
    print("=" * 78)
    print("  基準日          %s" % T)
    print("  コード          %s" % rec["git_commit"])
    print("  採用集合の指紋   %s" % rec["prior_sha"])
    print("  パラメータ      %d 本 / 実効 %.1f 本（λ=%g）"
          % (len(names), fit.effective_breadth, fit.lam))
    print("  ユニバース      %d 銘柄 / 訓練 %d 観測"
          % (len(scored), len(train)))
    print("  記録の指紋      %s" % rec["sha"][:16])
    print()
    print("  **上位%d銘柄**" % min(a.top_n, len(scored)))
    for p in rec["picks"][:15]:
        print("    %2d. %-8s %-10s score %+.4f"
              % (p["rank"], p["ticker"], (p["sector"] or "")[:10], p["score"]))
    if len(rec["picks"]) > 15:
        print("    …（残り %d 銘柄）" % (len(rec["picks"]) - 15))
    print()
    print("  重み（上位）:")
    for k, v in sorted(rec["weights"].items(), key=lambda x: -x[1])[:8]:
        print("    %-5s %.4f" % (k, v))
    print()
    print("  **realized は null。** %d日後（%s 以降）に評価できる。"
          % (a.horizon, (dt.date.fromisoformat(T)
                         + dt.timedelta(days=a.horizon)).isoformat()))
    ok, msg = verify()
    print("  連鎖の検証: %s（%s）" % ("OK" if ok else "**壊れている**", msg))
    return 0


def cmd_evaluate(a) -> int:
    """記録と実際のリターンを突き合わせる。**記録は書き換えない。**"""
    rows = read_all()
    if not rows:
        print("記録がまだ無い")
        return 0
    print("=" * 78)
    print("前向き記録の評価（**記録そのものは書き換えない**）")
    print("=" * 78)
    today = dt.date.today().isoformat()
    n_done = 0
    for r in rows:
        end = (dt.date.fromisoformat(r["asof"])
               + dt.timedelta(days=r["horizon_days"])).isoformat()
        if end > today:
            print("  %s  **まだ評価できない**（%s 以降）" % (r["asof"], end))
            continue
        rets = []
        for p in r["picks"]:
            s = PR.load([p["ticker"]]).get(p["ticker"])
            if not s:
                continue
            b = BR.adjust(s.bars)
            a1 = [x for x in b if x["date"] > r["asof"]]
            a2 = [x for x in b if x["date"] > end]
            if a1 and a2 and a1[0]["open"] > 0:
                rets.append(a2[0]["open"] / a1[0]["open"] - 1.0)
        if not rets:
            print("  %s  価格が取れない" % r["asof"])
            continue
        n_done += 1
        import statistics as st
        print("  %s  上位%d銘柄の平均 **%+.2f%%**（%d 銘柄で計測）"
              % (r["asof"], len(r["picks"]), 100 * st.fmean(rets), len(rets)))
    print()
    if n_done == 0:
        print("  **評価できる記録はまだ無い。** これは正常である —")
        print("  前向き記録は、**待つことでしか証拠にならない。**")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--panel", default="gate")
    ap.add_argument("--horizon", type=int, default=250)
    ap.add_argument("--top-n", type=int, default=30)
    ap.add_argument("--asof", default="")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--evaluate", action="store_true")
    a = ap.parse_args()
    if a.verify:
        ok, msg = verify()
        print("%s: %s" % ("OK" if ok else "**壊れている**", msg))
        return 0 if ok else 1
    if a.evaluate:
        return cmd_evaluate(a)
    return cmd_record(a)


if __name__ == "__main__":
    raise SystemExit(main())
