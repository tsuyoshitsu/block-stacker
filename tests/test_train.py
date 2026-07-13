"""Unit tests for training/train.py (no env spawning)."""
from __future__ import annotations

import pytest

from block_stacker.training.train import _build_parser, _compute_save_freq


# ----------------------------------------------------------------- _compute_save_freq


class TestComputeSaveFreq:
    def test_basic(self) -> None:
        # 50000 steps / 8 envs = 6250 calls
        assert _compute_save_freq(50000, 8) == 6250

    def test_single_env(self) -> None:
        assert _compute_save_freq(50000, 1) == 50000

    def test_zero_envs_clamps_to_one(self) -> None:
        # n_envs=0 is pathological; should not divide by zero
        assert _compute_save_freq(50000, 0) == 50000

    def test_small_interval(self) -> None:
        assert _compute_save_freq(1000, 4) == 250

    def test_minimum_one(self) -> None:
        # very small interval + many envs → clamp to 1
        assert _compute_save_freq(1, 100) == 1


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

    def test_resume_default_false(self) -> None:
        args = self._parse()
        assert args.resume is False
