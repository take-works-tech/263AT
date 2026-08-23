#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
LLM 派生パラメータの「前向き記録」。

なぜ今日始めるのか
------------------
T カテゴリ 45件と X カテゴリ 16件は **過去に遡って生成できない**（catalog §6.4、
docs/03_data_feasibility.md §6.1 で 769件中 187件が該当と実測）。
バックテストができない以上、検証手段は2つしかない。

  (a) 使用モデルの知識カットオフ**以降**のデータだけで検証する
  (b) **今日から出力を記録し続け、将来のリターンと突き合わせる**

(b) は始めるのが1日遅れれば検証開始も1日遅れる。**コードより先に、記録を始めるべき部品。**

記録するもの
------------
LLM の出力そのものだけでなく、**後から再現・監査できる情報を全部**残す。
  - asof_date        その時点で使える情報だけで判断したことを示す基準日
  - model_id         モデルを変えたら較正はやり直し（catalog §8-5）
  - prompt_sha256    プロンプトを変えたら別系列として扱う
  - input_sha256     同じ入力に同じ出力が出るか（決定性の検証）
  - value/evidence/confidence   根拠テキストがないと後で検証できない（§6.3）

使い方
------
  python tools/forward_log.py record --param T01 --market JP --ticker 7203 \\
      --value 0.6 --confidence 0.7 --evidence "..." --model claude-x --prompt-version v1
  python tools/forward_log.py record --batch batch.json
  python tools/forward_log.py status
  python tools/forward_log.py evaluate --horizon 63     # 将来リターンと突合（要価格データ）
  python tools/forward_log.py demo                      # 動作確認用の1件を書く
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import pathlib
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = pathlib.Path(__file__).resolve().parents[1]
LOGDIR = ROOT / "forward_log"
SCHEMA_VERSION = 1

REQUIRED = ["schema_version", "record_id", "ts_utc", "asof_date", "param_id",
            "market", "identifier", "model_id", "prompt_version", "prompt_sha256",
            "input_sha256", "value", "confidence"]


def sha(s):
    return hashlib.sha256(str(s).encode("utf-8")).hexdigest()[:16]


def make_record(param_id, market, identifier, value, confidence, evidence=None,
                model_id=None, prompt_version=None, prompt_text=None, input_text=None,
                input_ref=None, asof_date=None, extra=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "schema_version": SCHEMA_VERSION,
        "record_id": uuid.uuid4().hex[:16],
        "ts_utc": now.isoformat(timespec="seconds"),
        "asof_date": asof_date or now.date().isoformat(),
        "param_id": param_id,
        "market": market,
        "identifier": str(identifier),        # JP=証券コード / US=CIK か ticker
        "model_id": model_id or "unspecified",
        "prompt_version": prompt_version or "v0",
        "prompt_sha256": sha(prompt_text) if prompt_text else None,
        "input_sha256": sha(input_text) if input_text else None,
        "input_ref": input_ref,               # URL / EDINET 書類ID / accession number
        "value": value,
        "confidence": confidence,
        "evidence": evidence,
        "extra": extra or {},
        # 評価時に埋める。記録時点では必ず null（ここに値が入っていたらルックアヘッド）
        "realized": None,
    }


def append(records):
    LOGDIR.mkdir(parents=True, exist_ok=True)
    by_month = {}
    for r in records:
        miss = [k for k in REQUIRED if k not in r or r[k] is None and k != "input_sha256"]
        miss = [k for k in miss if k != "input_sha256"]
        if miss:
            raise ValueError("必須項目が欠けている: %s" % miss)
        by_month.setdefault(r["asof_date"][:7], []).append(r)
    n = 0
    for ym, rs in by_month.items():
        f = LOGDIR / ("%s.jsonl" % ym)
        with f.open("a", encoding="utf-8") as fh:
            for r in rs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")
                n += 1
    return n


def load_all():
    out = []
    if not LOGDIR.exists():
        return out
    for f in sorted(LOGDIR.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def cmd_status():
    recs = load_all()
    print("=" * 66)
    print("LLM 前向き記録の状況")
    print("=" * 66)
    if not recs:
        print("  記録がまだ1件もない。")
        print("  **T カテゴリ45件・X カテゴリ16件はバックテストできない。**")
        print("  記録を始めない限り、これらは永遠に検証できないままになる。")
        print("\n  最初の1件:  python tools/forward_log.py demo")
        return 0
    dates = sorted({r["asof_date"] for r in recs})
    params = sorted({r["param_id"] for r in recs})
    ids = {r["identifier"] for r in recs}
    models = sorted({r["model_id"] for r in recs})
    prompts = sorted({(r["param_id"], r["prompt_version"]) for r in recs})
    d0 = datetime.date.fromisoformat(dates[0])
    span = (datetime.date.today() - d0).days
    print("  レコード数      : %d" % len(recs))
    print("  記録開始        : %s（%d 日経過 / %.1f 年）" % (dates[0], span, span / 365.25))
    print("  最終記録        : %s" % dates[-1])
    print("  対象パラメータ  : %d 種  %s" % (len(params), ", ".join(params[:12])))
    print("  対象銘柄        : %d" % len(ids))
    print("  モデル          : %s" % ", ".join(models))
    print("  プロンプト版    : %d 組" % len(prompts))
    ev = sum(1 for r in recs if r.get("evidence"))
    print("  根拠テキストあり: %d / %d (%.0f%%)  ← 無いものは後から検証できない" % (ev, len(recs), 100 * ev / len(recs)))
    done = sum(1 for r in recs if r.get("realized") is not None)
    print("  実現値と突合済み: %d / %d" % (done, len(recs)))
    print()
    for h, lab in [(63, "3ヶ月"), (126, "6ヶ月"), (252, "1年"), (756, "3年")]:
        ready = sum(1 for d in dates if (datetime.date.today() - datetime.date.fromisoformat(d)).days >= h * 7 / 5)
        print("  %-6s ホライズンで評価可能な記録日: %d / %d" % (lab, ready, len(dates)))
    if span < 180:
        print("\n  ⚠ まだ %d 日分しかない。**最短でも半年、本来は1年以上の蓄積が要る。**" % span)
    return 0


def cmd_demo():
    r = make_record(
        param_id="T01", market="JP", identifier="0000",
        value=0.0, confidence=0.0,
        evidence="動作確認用のダミー。実際の判断には使わない。",
        model_id="demo", prompt_version="v0",
        prompt_text="demo-prompt", input_text="demo-input",
        input_ref="https://example.invalid/demo",
        extra={"note": "forward_log の疎通確認"})
    n = append([r])
    print("  %d 件を記録した → %s" % (n, (LOGDIR / (r['asof_date'][:7] + '.jsonl')).relative_to(ROOT)))
    print("  記録開始日: %s" % r["asof_date"])
    return 0


def cmd_record(a):
    if a.batch:
        payload = json.loads(pathlib.Path(a.batch).read_text(encoding="utf-8"))
        recs = [make_record(**p) for p in payload]
    else:
        if not (a.param and a.identifier is not None and a.value is not None):
            print("  --param / --ticker / --value は必須（または --batch）")
            return 1
        recs = [make_record(param_id=a.param, market=a.market, identifier=a.identifier,
                            value=a.value, confidence=a.confidence, evidence=a.evidence,
                            model_id=a.model, prompt_version=a.prompt_version,
                            prompt_text=a.prompt_text, input_text=a.input_text,
                            input_ref=a.input_ref, asof_date=a.asof)]
    print("  %d 件を記録した" % append(recs))
    return 0


def cmd_evaluate(a):
    """将来リターンと突合する。価格データが揃ってから使う。"""
    recs = load_all()
    ready = []
    for r in recs:
        d = datetime.date.fromisoformat(r["asof_date"])
        if (datetime.date.today() - d).days >= a.horizon * 7 / 5 and r.get("realized") is None:
            ready.append(r)
    print("  ホライズン %d 営業日で評価可能かつ未評価: %d 件" % (a.horizon, len(ready)))
    if not ready:
        print("  まだ評価できる記録がない。時間の経過を待つしかない。")
        return 0
    print("  ⚠ 価格データの接続が未実装（docs/03_data_feasibility.md DF-01）。")
    print("     株価が取れるようになったら、ここで asof_date + horizon のリターンを結合し、")
    print("     realized に書き込んだうえで IC と較正曲線を出す。")
    print("     **realized を記録時に埋めてはいけない。埋まっていたらルックアヘッド。**")
    return 0


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("demo")
    r = sub.add_parser("record")
    r.add_argument("--param"); r.add_argument("--market", default="JP")
    r.add_argument("--ticker", dest="identifier"); r.add_argument("--value", type=float)
    r.add_argument("--confidence", type=float, default=0.5)
    r.add_argument("--evidence"); r.add_argument("--model")
    r.add_argument("--prompt-version", dest="prompt_version")
    r.add_argument("--prompt-text", dest="prompt_text")
    r.add_argument("--input-text", dest="input_text")
    r.add_argument("--input-ref", dest="input_ref")
    r.add_argument("--asof"); r.add_argument("--batch")
    e = sub.add_parser("evaluate"); e.add_argument("--horizon", type=int, default=63)
    a = ap.parse_args()
    if a.cmd == "status":
        return cmd_status()
    if a.cmd == "demo":
        return cmd_demo()
    if a.cmd == "record":
        return cmd_record(a)
    if a.cmd == "evaluate":
        return cmd_evaluate(a)
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
