"""Stage 進行（オートカリキュラム）の補助。

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    SAC 学習を Stage 1 → 2 → … と自動で進めるための部品。

卒業条件（OR、どちらかで卒業）:
    1. 散布ブロックゼロ（全ブロックを積み切った）→ **即卒業**（成功率を待たない fast-track）。
       env が info["all_placed"] を出し、それが True になった瞬間に卒業する。
    2. 「目標高さ達成状態」の成功率が直近 window で threshold（既定 0.6）以上 → 卒業。
       env が info["is_success"]（= tower_best_height >= 目標高さ）を出し、
       GraduationCallback が done ごとに rolling window で集計する。
       目標高さ = 在庫を全部縦積みした理論高さ × graduation.ratio。

設計上のポイント:
    - is_success は「目標高さ到達」のみ（散布0 は混ぜず、別シグナル all_placed で即卒業）。
    - graduated=True になると _on_step() が False を返して model.learn() を早期終了
      → train.py のループが次ステージへ進む。
    - 観測空間は全ステージ共通（max_blocks=8 等で固定）なので、同じ model を
      model.set_env() で各ステージに付け替えるだけでよい（NN もバッファも引き継ぐ）。
    - 観測空間は全ステージ共通（max_blocks=8 等で固定）なので、同じ model を
      model.set_env() で各ステージに付け替えるだけでよい（NN もバッファも引き継ぐ）。

関連:
    - configs/training.yaml: curriculum.graduation (window / threshold / ratio)
    - env/env.py: info["is_success"] の算出
    - mvp2/train.py: ステージ進行ループ本体
"""
from __future__ import annotations

import os
from collections import deque
from typing import Any

from stable_baselines3.common.callbacks import BaseCallback

from block_stacker.config import WorldConfig

# graduation 設定を上書きできるコンテナ環境変数（本番は Docker/ECS の env で渡す）。
GRAD_ENV_WINDOW = "BS_GRADUATION_WINDOW"
GRAD_ENV_THRESHOLD = "BS_GRADUATION_THRESHOLD"
GRAD_ENV_RATIO = "BS_GRADUATION_RATIO"


def resolve_graduation(graduation_cfg: dict[str, Any]) -> tuple[int, float, float]:
    """graduation 設定を (window, threshold, ratio) で返す。

    優先順位: コンテナ環境変数 BS_GRADUATION_* > training.yaml の graduation > 既定値。
    本番（Docker/ECS）では `-e BS_GRADUATION_RATIO=0.7` のように env var で上書きできる。
      - BS_GRADUATION_WINDOW    : 成功率を見る直近エピソード数（既定 30）
      - BS_GRADUATION_THRESHOLD : 卒業する成功率（既定 0.6）
      - BS_GRADUATION_RATIO     : 目標高さ ＝ 在庫満積み高さ × ratio（既定 0.6）
    """
    win_env = os.environ.get(GRAD_ENV_WINDOW)
    thr_env = os.environ.get(GRAD_ENV_THRESHOLD)
    ratio_env = os.environ.get(GRAD_ENV_RATIO)
    window = int(win_env) if win_env is not None else int(graduation_cfg.get("window", 30))
    threshold = (
        float(thr_env) if thr_env is not None
        else float(graduation_cfg.get("threshold", 0.6))
    )
    ratio = (
        float(ratio_env) if ratio_env is not None
        else float(graduation_cfg.get("ratio", 0.6))
    )
    return window, threshold, ratio


def stage_inventory(stage: dict[str, Any], world_cfg: WorldConfig) -> dict[str, int]:
    """stage 定義から在庫を取り出す。

    inventory 明示があればそれを、無ければ shapes_allowed で world の在庫を絞る。
    """
    return stage.get("inventory") or {
        name: count
        for name, count in world_cfg.inventory.items()
        if name in set(stage["shapes_allowed"])
    }


class StageMonitorCallback(BaseCallback):
    """継続学習フェーズ用モニター。卒業ロジックなしで curriculum メトリクスを記録する。

    全ステージ早期卒業後の post-loop continuation で GraduationCallback の代わりに使う。
    GraduationCallback は graduation 条件が満たされると False を返して learn() を止めるが、
    continuation では total_timesteps まで走り続ける必要があるため、このクラスを使う。
    """

    def __init__(self, stage_id: int | None, window: int = 30) -> None:
        super().__init__(verbose=0)
        self.stage_id = stage_id
        self.successes: deque[float] = deque(maxlen=window)
        self.episodes_seen = 0

    @property
    def success_rate(self) -> float:
        if not self.successes:
            return 0.0
        return sum(self.successes) / len(self.successes)

    def _on_step(self) -> bool:
        dones = self.locals.get("dones")
        infos = self.locals.get("infos")
        if dones is not None and infos is not None:
            for done, info in zip(dones, infos, strict=False):
                if done:
                    self.episodes_seen += 1
                    self.successes.append(1.0 if info.get("is_success", False) else 0.0)
        logger = getattr(self, "logger", None)
        if logger is not None:
            if self.stage_id is not None:
                logger.record("curriculum/stage", self.stage_id)
            if self.episodes_seen > 0:
                logger.record("curriculum/success_rate", self.success_rate)
                logger.record("curriculum/episodes_seen", self.episodes_seen)
        return True  # 卒業ロジックなし: 決して learn() を止めない


class GraduationCallback(BaseCallback):
    """成功率が threshold 以上になったら卒業（learn を早期終了して次ステージへ）。

    使い方:
        cb = GraduationCallback(window=30, threshold=0.6, verbose=1)
        model.learn(total_timesteps=budget, callback=cb, reset_num_timesteps=False)
        if cb.graduated: 次ステージへ
    """

    def __init__(
        self,
        window: int = 30,
        threshold: float = 0.6,
        stage_id: int | None = None,
        verbose: int = 0,
    ) -> None:
        super().__init__(verbose)
        self.window = int(window)
        self.threshold = float(threshold)
        self.stage_id = stage_id
        self.successes: deque[float] = deque(maxlen=self.window)
        self.graduated = False
        self.episodes_seen = 0

    @property
    def success_rate(self) -> float:
        if not self.successes:
            return 0.0
        return sum(self.successes) / len(self.successes)

    def _record(self, dones: Any, infos: Any) -> None:
        """done になった env の is_success を集計（純粋ロジック、テスト用に分離）。"""
        for done, info in zip(dones, infos, strict=False):
            if not done:
                continue
            self.episodes_seen += 1
            self.successes.append(1.0 if info.get("is_success", False) else 0.0)

    def _should_graduate(self) -> bool:
        return len(self.successes) >= self.window and self.success_rate >= self.threshold

    def _on_step(self) -> bool:
        dones = self.locals.get("dones")
        infos = self.locals.get("infos")

        # ① fast-track: 散布ブロックゼロ（全部積み切った）を達成 → 成功率を待たず即卒業。
        if infos is not None and any(info.get("all_placed", False) for info in infos):
            self.graduated = True
            if self.verbose:
                print("[graduation] all blocks placed (散布0) -> GRADUATE (即卒業)")
            return False

        if dones is not None and infos is not None:
            self._record(dones, infos)
        logger = getattr(self, "logger", None)
        if logger is not None:
            # ステージ番号は毎テーブルに出す（学習中どのステージか一目で分かるように）。
            if self.stage_id is not None:
                logger.record("curriculum/stage", self.stage_id)
            if self.episodes_seen > 0:
                logger.record("curriculum/success_rate", self.success_rate)
                logger.record("curriculum/episodes_seen", self.episodes_seen)

        # 目標高さ到達の成功率が threshold 以上で卒業。
        if self._should_graduate():
            self.graduated = True
            if self.verbose:
                print(
                    f"[graduation] success_rate={self.success_rate:.2f} "
                    f">= {self.threshold:.2f} over last {self.window} eps -> GRADUATE"
                )
            return False  # learn() を止める
        return True
