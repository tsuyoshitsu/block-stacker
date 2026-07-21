"""Stage 進行（オートカリキュラム）の補助。

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    SAC 学習を Stage 1 → 2 → … と自動で進めるための部品。

ステージ進行:
    **固定ステップ数**で進む。各ステージは configs/training.yaml の
    `curriculum.stages[].steps`（CLI の --stage-steps で上書き可）だけ走り、
    成績によらず必ず次のステージへ進む。**卒業判定は行わない。**

    かつては「散布0（all_placed）で即卒業」「成功率 threshold 超えで卒業」の
    2 条件で進行していたが、前者が誤検出するため撤去した（詳細は
    docs/design_change_record.md）。横に広い連結構造でも all_placed が成立し、
    高さ 0.100m（目標 0.240m）のまま Stage 1→4 が数千ステップで飛んでいた。

指標（学習を止めず記録のみ。次回のステップ数配分を決める材料）:
    - curriculum/success_rate     直近 window の「目標高さ到達」率（info["is_success"]）
    - curriculum/all_placed_rate  直近 window の「全ブロックが1つの連結構造」率
    - curriculum/all_placed_total そのステージでの通算回数
    ※ all_placed は高さを含まない指標。「高く積めた」ではなく
      「作品が1つにまとまった」を意味する（低く横に広い構造でも成立する）。

設計上のポイント:
    - 観測空間は全ステージ共通（max_blocks=8 等で固定）なので、同じ model を
      model.set_env() で各ステージに付け替えるだけでよい（NN もバッファも引き継ぐ）。

関連:
    - configs/training.yaml: curriculum.stages[].steps / curriculum.graduation.ratio
    - env/env.py: info["is_success"] / info["all_placed"] の算出
    - training/train.py: ステージ進行ループ本体
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
      - BS_GRADUATION_WINDOW    : 指標の集計対象エピソード数（既定 30）
      - BS_GRADUATION_THRESHOLD : **未使用**（卒業判定を撤去したため残置。読み替えのみ）
      - BS_GRADUATION_RATIO     : 目標高さ ＝ 在庫満積み高さ × ratio（既定 0.6）

    注: 卒業判定は廃止したので threshold は進行を左右しない。window と ratio のみ有効
    （window = 指標の移動平均幅、ratio = info["is_success"] の判定に使う目標高さ）。
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
    """ステージ指標の記録専用コールバック。**learn() を決して止めない。**

    卒業判定を撤去したので、進行はステージ予算（固定ステップ数）だけで決まる。
    このコールバックは「次回どれだけステップを割り当てるか」を判断するための
    材料を TensorBoard に残すのが役目。

    記録する指標:
        curriculum/stage              現在のステージ番号
        curriculum/episodes_seen      このステージで完了したエピソード数
        curriculum/success_rate       直近 window の「目標高さ到達」率
        curriculum/all_placed_rate    直近 window の「全ブロックが1つの連結構造」率
        curriculum/all_placed_total   このステージでの all_placed 通算回数
        curriculum/all_placed_height  達成時のタワー高さ（直近 window の平均）
        curriculum/tower_height_mean  直近 window のエピソード最高到達高さの平均

    **all_placed だけでは「本物の塔」か「低く広がった構造」かを判別できない**。
    連結成分は横に広がっても成立するため（8 個のレンガ積み・高さ 0.100m でも成立する
    ことを実測確認済み）、必ず all_placed_height と併読すること。
    例: Stage 1 なら 0.400m 付近なら本物の 8 段、0.100m 付近ならレンガ積み。
    """

    def __init__(self, stage_id: int | None, window: int = 30) -> None:
        super().__init__(verbose=0)
        self.stage_id = stage_id
        self.successes: deque[float] = deque(maxlen=window)
        self.all_placed_flags: deque[float] = deque(maxlen=window)
        self.all_placed_heights: deque[float] = deque(maxlen=window)
        self.tower_heights: deque[float] = deque(maxlen=window)
        self.episodes_seen = 0
        self.all_placed_total = 0

    @property
    def success_rate(self) -> float:
        if not self.successes:
            return 0.0
        return sum(self.successes) / len(self.successes)

    @property
    def all_placed_rate(self) -> float:
        if not self.all_placed_flags:
            return 0.0
        return sum(self.all_placed_flags) / len(self.all_placed_flags)

    @property
    def all_placed_height(self) -> float:
        """散布0 を達成したエピソードの、達成時タワー高さの平均（未達成なら 0）。"""
        if not self.all_placed_heights:
            return 0.0
        return sum(self.all_placed_heights) / len(self.all_placed_heights)

    @property
    def tower_height_mean(self) -> float:
        if not self.tower_heights:
            return 0.0
        return sum(self.tower_heights) / len(self.tower_heights)

    def _record(self, dones: Any, infos: Any) -> None:
        """done になった env の指標を集計（純粋ロジック、テスト用に分離）。"""
        for done, info in zip(dones, infos, strict=False):
            if not done:
                continue
            self.episodes_seen += 1
            self.successes.append(1.0 if info.get("is_success", False) else 0.0)
            self.tower_heights.append(float(info.get("tower_best_height", 0.0)))
            placed = bool(info.get("all_placed", False))
            self.all_placed_flags.append(1.0 if placed else 0.0)
            if placed:
                # エピソード内で複数回達成しうるので count 分を通算に足す。
                self.all_placed_total += int(info.get("all_placed_count", 1))
                self.all_placed_heights.append(float(info.get("all_placed_height", 0.0)))

    def _on_step(self) -> bool:
        dones = self.locals.get("dones")
        infos = self.locals.get("infos")
        if dones is not None and infos is not None:
            self._record(dones, infos)
        logger = getattr(self, "logger", None)
        if logger is not None:
            if self.stage_id is not None:
                logger.record("curriculum/stage", self.stage_id)
            if self.episodes_seen > 0:
                logger.record("curriculum/success_rate", self.success_rate)
                logger.record("curriculum/episodes_seen", self.episodes_seen)
                logger.record("curriculum/all_placed_rate", self.all_placed_rate)
                logger.record("curriculum/all_placed_total", self.all_placed_total)
                logger.record("curriculum/tower_height_mean", self.tower_height_mean)
                if self.all_placed_heights:
                    logger.record("curriculum/all_placed_height", self.all_placed_height)
        return True  # 記録のみ: 決して learn() を止めない

