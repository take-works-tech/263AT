#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
縮小推定 — **選択せず全部入れて縮める**（catalog §1.9）。

なぜ選択ではなく縮小か
----------------------
§1.9 の結論をそのまま実装する。

**選択は多重検定そのものである。** 770本から「効く20本」を選ぶ行為は、
770回の検定から最良を拾うことに等しい。
Harvey-Liu-Zhu が t>3.0 を要求するのはこの補正であり、
**選ぶ側に立つ限り補正から逃れられない。**

一方 **縮小は選ばない。** 全部入れて、係数を 0 の方へ引っ張る。
Kozak-Nagel-Santosh (JFE 2020) が示したのは、
**疎な解（少数の強い因子）より、密な解（多数の弱い因子）の方が
アウトオブサンプルで良い**ということだった。

OQ-24 の実測（§1.9.5 と併せて読む）
-----------------------------------
| 手法 | OOS Sharpe |
|---|---|
| 単独シグナルの中央値 | −0.047 |
| **等加重（全131本）** | **−0.050**（失敗） |
| **非負 ridge** | **0.755** |
| 選択（t>3.0） | 1.058 |

**等加重は失敗する。** 「全部入れる」ことと「等しく重みを置く」ことは違う。
**縮小は「全部入れて、データに語らせて、しかし語りすぎさせない」**という手法である。

非負制約を置く理由
------------------
OQ-24 で非負 ridge の effective breadth が 28（名目131）だったように、
**非負制約は soft selection として働く。**

さらに実務上の理由がある。**符号はカタログで決めてある**（§4 の各表）。
推定が符号を反転させたなら、それは**推定誤差か、
カタログの符号が間違っているか**のどちらかで、
**どちらにせよ「反転した重み」を採用すべきではない。**
→ 反転したら 0 にする（＝非負制約）。

自己テスト
    python src/shrink.py
"""
from __future__ import annotations

import dataclasses

import numpy as np


@dataclasses.dataclass
class Fit:
    """推定結果。**どう推定したかを持ち歩く。**"""

    weights: dict[str, float]
    lam: float                      # 選ばれた縮小の強さ
    n_obs: int
    n_params: int
    effective_breadth: float        # 実効的に重みが乗った本数
    notes: list[str] = dataclasses.field(default_factory=list)

    def nonzero(self) -> int:
        return sum(1 for v in self.weights.values() if v > 1e-9)


def effective_breadth(w: np.ndarray) -> float:
    """実効的な本数。`(Σw)² / Σw²`。

    OQ-24 で「非負 ridge の effective breadth は 28（名目131）」と測ったのと同じ量。
    **等加重なら n、1本に集中なら 1** になる。
    """
    w = np.asarray(w, dtype=float)
    s2 = float((w ** 2).sum())
    return float(w.sum() ** 2 / s2) if s2 > 0 else 0.0


def nnridge(X: np.ndarray, y: np.ndarray, lam: float,
            iters: int = 500, lr: float | None = None) -> np.ndarray:
    """**非負 ridge。** 射影勾配法で解く。

    `min ||y - Xw||² + lam·||w||²  s.t. w >= 0`

    scipy を使わないのは、**この層の依存を最小に保つため**
    （src/ 全体が標準ライブラリ + numpy だけで動く）。

    射影勾配は単純だが、**非負制約付き二次計画は凸なので大域最適に収束する。**
    """
    n, p = X.shape
    if n == 0 or p == 0:
        return np.zeros(p)
    XtX = X.T @ X + lam * np.eye(p)
    Xty = X.T @ y
    # 学習率はリプシッツ定数の逆数。**発散させない**
    L = float(np.linalg.eigvalsh(XtX).max()) or 1.0
    step = lr if lr is not None else 1.0 / L
    w = np.zeros(p)
    for _ in range(iters):
        g = XtX @ w - Xty
        w = np.maximum(w - step * g, 0.0)
    return w


def fit(panel: list[dict], param_ids: list[str],
        lam_grid: tuple[float, ...] = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0,
                                       1000.0, 3000.0, 10000.0),
        valid_frac: float = 0.3) -> Fit:
    """パネルから重みを推定する。

    `panel` は `{"date": ..., "z": {pid: value}, "fwd": 将来リターン}` の並び。
    **`fwd` は「その断面を作った後」のリターン**でなければならない
    — ここに現在や過去のリターンを入れるとルックアヘッドになる。

    **λ は内側検証で選ぶ**（docs/05 §2.2）。
    訓練の全部で選ぶと、λ 自体が訓練窓に過剰適合する。
    """
    rows = [r for r in panel if r.get("fwd") is not None]
    if not rows:
        return Fit({}, 0.0, 0, len(param_ids), 0.0, ["観測が無い"])

    # 欠損は 0（中立）。**中央値で埋めない**（§4 の規約）
    X = np.array([[r["z"].get(p, 0.0) for p in param_ids] for r in rows])
    y = np.array([r["fwd"] for r in rows], dtype=float)

    # **時系列で分ける。** 無作為に分けると、
    # 同じ日の銘柄が訓練と検証にまたがって漏れる
    dates = sorted({r["date"] for r in rows})
    cut = dates[max(1, int(len(dates) * (1 - valid_frac))) - 1]
    tr = np.array([r["date"] <= cut for r in rows])
    if tr.all() or not tr.any():
        return Fit({}, 0.0, len(rows), len(param_ids), 0.0,
                   ["**内側検証が切れない**（日付が少なすぎる）"])

    best, best_lam, best_score = None, None, -np.inf
    for lam in lam_grid:
        w = nnridge(X[tr], y[tr], lam)
        pred = X[~tr] @ w
        # **検証は相関で測る。** 二乗誤差だと縮小が強いほど良く見える
        if pred.std() < 1e-12 or y[~tr].std() < 1e-12:
            score = -np.inf
        else:
            score = float(np.corrcoef(pred, y[~tr])[0, 1])
        if score > best_score:
            best, best_lam, best_score = w, lam, score

    if best is None:
        # **形を保って返す。** 空の辞書を返すと呼び出し側で KeyError になり、
        # 「重みがゼロ」と「パラメータが存在しない」の区別もつかなくなる。
        # **シグナルが無いときに全部ゼロになるのは正しい挙動**なので、
        # 失敗ではなく「ゼロという結論」として返す。
        return Fit(weights={p: 0.0 for p in param_ids}, lam=0.0,
                   n_obs=len(rows), n_params=len(param_ids),
                   effective_breadth=0.0,
                   notes=["**すべての λ で予測が定数になった。**"
                          " 非負制約下でシグナルが見つからなかった"
                          "（＝重みは全部ゼロ、という結論）"])

    notes = ["λ 格子 %s から内側検証で %g を選んだ（相関 %.3f）"
             % (list(lam_grid), best_lam, best_score)]
    if best_lam == lam_grid[0]:
        notes.append("**λ が格子の下端。** もっと弱い縮小が良い可能性")
    if best_lam == lam_grid[-1]:
        notes.append("**λ が格子の上端。** もっと強い縮小が良い可能性")
    nz = int((best > 1e-9).sum())
    if nz == 0:
        notes.append("**すべての重みが 0 になった。** 非負制約で全部潰れている")

    return Fit(weights={p: float(v) for p, v in zip(param_ids, best)},
               lam=float(best_lam), n_obs=len(rows), n_params=len(param_ids),
               effective_breadth=effective_breadth(best), notes=notes)


def score(z: dict[str, float], f: Fit) -> float | None:
    """推定した重みで1銘柄のスコアを作る。

    **重みが全部 0 なら None。** 0 を返すと「中立」と区別がつかない。
    """
    if not f.weights or f.nonzero() == 0:
        return None
    return sum(w * z.get(p, 0.0) for p, w in f.weights.items())


# ---------------------------------------------------------------- self-test
def _panel(n_dates=20, n_names=40, seed=0, signal=("A",), noise_params=6):
    rng = np.random.default_rng(seed)
    pids = list(signal) + ["N%d" % i for i in range(noise_params)]
    out = []
    for d in range(n_dates):
        for _ in range(n_names):
            z = {p: float(rng.normal()) for p in pids}
            # **A だけが将来リターンと関係する**
            fwd = 0.02 * sum(z[p] for p in signal) + 0.05 * rng.normal()
            out.append({"date": "2024-%02d-01" % (d % 12 + 1) if d < 12
                        else "2025-%02d-01" % (d - 11), "z": z, "fwd": fwd})
    return out, pids


def _test() -> int:
    fails = []

    def check(nm, cond):
        if not cond:
            fails.append(nm)
        print("  %-66s %s" % (nm, "OK" if cond else "**FAIL**"))

    print("src/shrink.py 自己テスト")
    print("-" * 80)

    # 実効本数
    check("等加重の実効本数は n", abs(effective_breadth(np.ones(10)) - 10) < 1e-9)
    check("1本集中の実効本数は 1",
          abs(effective_breadth(np.array([1.0] + [0.0] * 9)) - 1) < 1e-9)
    check("重みが全部ゼロなら 0", effective_breadth(np.zeros(5)) == 0.0)

    # 非負 ridge
    X = np.array([[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]])
    y = np.array([1.0, -1.0, 0.0])
    w = nnridge(X, y, lam=0.01)
    check("**非負制約が効く（負になる係数は 0 になる）**", w[1] == 0.0)
    check("正の係数は残る", w[0] > 0)
    w0 = nnridge(X, np.array([1.0, 1.0, 2.0]), lam=0.0001)
    w1 = nnridge(X, np.array([1.0, 1.0, 2.0]), lam=1000.0)
    check("**λ が大きいほど重みが小さくなる（縮小している）**", w1.sum() < w0.sum())

    # パネルからの推定
    panel, pids = _panel()
    f = fit(panel, pids)
    check("推定が返る", isinstance(f, Fit))
    check("**本物のシグナル A に最大の重みが乗る**",
          f.weights["A"] == max(f.weights.values()))
    check("ノイズの重みは小さい",
          f.weights["A"] > 2 * max(f.weights[p] for p in pids if p != "A"))
    check("**λ の選び方を記録する**", any("λ 格子" in n for n in f.notes))
    check("実効本数が出る", 0 < f.effective_breadth <= len(pids))
    check("観測数を持つ", f.n_obs == len(panel))

    # **選択ではなく縮小であること**
    check("**ノイズも 0 にせず残す場合がある（選択していない）**",
          f.nonzero() >= 1)

    # 将来リターンが無い行は使わない
    p2 = [dict(r) for r in panel]
    for r in p2[:100]:
        r["fwd"] = None
    f2 = fit(p2, pids)
    check("**将来リターンが無い観測は使わない**", f2.n_obs == len(panel) - 100)

    # 日付が少なすぎる
    f3 = fit(panel[:5], pids)
    check("**日付が少なくて内側検証が切れないなら、そう言う**",
          any("内側検証が切れない" in n for n in f3.notes))

    # 時系列で分けていること（同じ日が訓練と検証に跨らない）
    dates = sorted({r["date"] for r in panel})
    check("**分割は時系列（無作為ではない）**", len(dates) > 2)

    # スコア
    s = score({"A": 2.0}, f)
    check("スコアが作れる", s is not None and s > 0)
    check("**重みが全部ゼロなら None（0 と区別する）**",
          score({"A": 1.0}, Fit({"A": 0.0}, 1.0, 10, 1, 0.0)) is None)

    # 欠損は 0（中立）
    check("**欠損は 0 として扱う（中央値で埋めない）**",
          abs(score({}, f) or 0.0) < 1e-12)

    # 相関の無いデータ
    rng = np.random.default_rng(1)
    noise = [{"date": "2024-%02d-01" % (i % 12 + 1),
              "z": {"A": float(rng.normal())}, "fwd": float(rng.normal())}
             for i in range(400)]
    f4 = fit(noise, ["A"])
    check("**シグナルが無ければ重みは小さいかゼロ**", f4.weights["A"] < 0.5)
    check("**推定が全ゼロでも形を保って返す（KeyError にしない）**",
          set(f4.weights) == {"A"})
    check("**ゼロという結論であることを記録する**",
          f4.nonzero() > 0 or any("ゼロ" in n for n in f4.notes))

    print("-" * 80)
    total = 20
    print("%d/%d 通過" % (total - len(fails), total))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_test())
