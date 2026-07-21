"""Unit tests for training/train.py (no env spawning)."""
from __future__ import annotations

import pytest

from block_stacker.training.curriculum import StageMonitorCallback
from block_stacker.training.train import (
    DEFAULT_STAGE_STEPS,
    _build_parser,
    resolve_stage_budgets,
)

# ----------------------------------------------------------------- _build_parser (train)


class TestBuildParserTrain:
    def _parse(self, extra: list[str] | None = None) -> object:
        return _build_parser().parse_args(extra or [])

    def test_default_target_stage_is_4(self) -> None:
        args = self._parse()
        assert args.target_stage == 4

    def test_explicit_target_stage(self) -> None:
        args = self._parse(["--target-stage", "5"])
        assert args.target_stage == 5

    def test_target_stage_large_value(self) -> None:
        args = self._parse(["--target-stage", "9999"])
        assert args.target_stage == 9999

    def test_curriculum_on_by_default(self) -> None:
        args = self._parse()
        assert args.curriculum is True

    def test_no_curriculum_flag(self) -> None:
        args = self._parse(["--no-curriculum"])
        assert args.curriculum is False

    def test_default_start_stage(self) -> None:
        args = self._parse()
        assert args.start_stage == 1

    def test_default_max_stage_none(self) -> None:
        args = self._parse()
        assert args.max_stage is None

    def test_resume_flag_removed(self) -> None:
        # --resume は廃止済み。渡すとパースエラーになる（復活の回帰防止）。
        with pytest.raises(SystemExit):
            self._parse(["--resume"])

    def test_default_stage_steps_none(self) -> None:
        args = self._parse()
        assert args.stage_steps is None

    def test_stage_steps_passthrough(self) -> None:
        args = self._parse(["--stage-steps", "1000,2000"])
        assert args.stage_steps == "1000,2000"


# ------------------------------------------------------------- resolve_stage_budgets


class TestResolveStageBudgets:
    """ステージ予算（固定ステップ制）の解決。卒業判定廃止に伴い進行を決める唯一の要素。"""

    STAGES: list[dict[str, object]] = [
        {"id": 1, "steps": 300000},
        {"id": 2, "steps": 300000},
        {"id": 3, "steps": 350000},
    ]

    def test_none_uses_yaml_steps(self) -> None:
        assert resolve_stage_budgets(None, self.STAGES) == [300000, 300000, 350000]

    def test_none_falls_back_when_steps_missing(self) -> None:
        stages: list[dict[str, object]] = [{"id": 1}, {"id": 2, "steps": 7}]
        assert resolve_stage_budgets(None, stages) == [DEFAULT_STAGE_STEPS, 7]

    def test_single_value_applies_to_all(self) -> None:
        assert resolve_stage_budgets("5000", self.STAGES) == [5000, 5000, 5000]

    def test_per_stage_list(self) -> None:
        assert resolve_stage_budgets("100,200,300", self.STAGES) == [100, 200, 300]

    def test_scientific_notation_accepted(self) -> None:
        assert resolve_stage_budgets("3e5", self.STAGES) == [300000] * 3

    def test_whitespace_tolerated(self) -> None:
        assert resolve_stage_budgets(" 100 , 200 , 300 ", self.STAGES) == [100, 200, 300]

    def test_length_mismatch_is_fatal(self) -> None:
        # 黙って切り詰める/伸ばすと学習量が意図とズレるので、必ず失敗させる。
        with pytest.raises(SystemExit):
            resolve_stage_budgets("100,200", self.STAGES)

    def test_non_numeric_is_fatal(self) -> None:
        with pytest.raises(SystemExit):
            resolve_stage_budgets("100,abc,300", self.STAGES)

    def test_non_positive_is_fatal(self) -> None:
        with pytest.raises(SystemExit):
            resolve_stage_budgets("100,0,300", self.STAGES)

    def test_empty_spec_is_fatal(self) -> None:
        with pytest.raises(SystemExit):
            resolve_stage_budgets("  ", self.STAGES)


# ------------------------------------------------------------- StageMonitorCallback


class TestStageMonitorAllPlaced:
    """all_placed を「卒業ゲート」ではなく「指標」として数えることの検証。

    回帰: 以前は all_placed が True になった瞬間に learn() を止めて即卒業していた。
    横に広い連結構造でも成立するため、高さ 0.100m（目標 0.240m）のまま
    Stage 1→4 が数千ステップで飛んでいた。今は記録するだけで進行に影響しない。
    """

    def _cb(self, window: int = 30) -> StageMonitorCallback:
        return StageMonitorCallback(stage_id=1, window=window)

    def test_counts_only_finished_episodes(self) -> None:
        cb = self._cb()
        # done=False の間は何も数えない（all_placed が立っていても）。
        cb._record([False, False], [{"all_placed": True}, {"is_success": True}])
        assert cb.episodes_seen == 0
        assert cb.all_placed_total == 0

    def test_counts_all_placed_at_episode_end(self) -> None:
        cb = self._cb()
        cb._record([True, True], [{"all_placed": True}, {"all_placed": False}])
        assert cb.episodes_seen == 2
        assert cb.all_placed_total == 1
        assert cb.all_placed_rate == 0.5

    def test_all_placed_independent_of_is_success(self) -> None:
        # 低く横に広い構造 = all_placed だが目標高さ未達、という組み合わせを表現できる。
        cb = self._cb()
        cb._record([True], [{"all_placed": True, "is_success": False}])
        assert cb.all_placed_rate == 1.0
        assert cb.success_rate == 0.0

    def test_rate_uses_rolling_window(self) -> None:
        cb = self._cb(window=2)
        cb._record([True], [{"all_placed": True}])
        cb._record([True], [{"all_placed": True}])
        cb._record([True], [{"all_placed": False}])
        # 直近 2 件は (True, False) → 0.5。通算カウンタは減らない。
        assert cb.all_placed_rate == 0.5
        assert cb.all_placed_total == 2
        assert cb.episodes_seen == 3

    def test_empty_rates_are_zero(self) -> None:
        cb = self._cb()
        assert cb.all_placed_rate == 0.0
        assert cb.success_rate == 0.0

    def test_on_step_never_stops_learning(self) -> None:
        # 卒業判定を撤去した以上、どんな入力でも True（= 学習継続）を返す。
        cb = self._cb()
        cb.locals = {"dones": [True], "infos": [{"all_placed": True, "is_success": True}]}
        assert cb._on_step() is True

    def test_records_all_placed_height(self) -> None:
        # all_placed だけでは本物の塔かレンガ積みか分からないので高さを併記する。
        cb = self._cb()
        cb._record([True], [{"all_placed": True, "all_placed_height": 0.10,
                             "all_placed_count": 1, "tower_best_height": 0.10}])
        assert cb.all_placed_height == 0.10
        assert cb.tower_height_mean == 0.10

    def test_all_placed_count_accumulates(self) -> None:
        # 再配置して継続するため 1 エピソードで複数回成立しうる。
        cb = self._cb()
        cb._record([True], [{"all_placed": True, "all_placed_count": 3}])
        assert cb.all_placed_total == 3
        assert cb.episodes_seen == 1

    def test_height_ignored_when_not_all_placed(self) -> None:
        cb = self._cb()
        cb._record([True], [{"all_placed": False, "tower_best_height": 0.22}])
        assert cb.all_placed_height == 0.0     # 未達成なら 0
        assert cb.tower_height_mean == 0.22    # 到達高さは常に記録
