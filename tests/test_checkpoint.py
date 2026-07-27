"""Tests for checkpoint filename parsing and discovery (new + legacy formats)."""
from __future__ import annotations

import pytest

from block_stacker.training.checkpoint import (
    _OLD_TS,
    _parse_checkpoint_name,
    find_latest_checkpoint,
)


# ---------------------------------------------------------------- _parse_checkpoint_name

class TestParseCheckpointName:
    def test_new_format(self):
        assert _parse_checkpoint_name("sac_20260627-143022_3990_steps.zip") == ("20260627-143022", 3990)

    def test_new_format_leading_zeros(self):
        assert _parse_checkpoint_name("sac_20260101-000000_800_steps.zip") == ("20260101-000000", 800)

    def test_old_format_gets_sentinel_ts(self):
        ts, steps = _parse_checkpoint_name("sac_3990_steps.zip")  # type: ignore[misc]
        assert ts == _OLD_TS
        assert steps == 3990

    def test_old_format_small_steps(self):
        ts, steps = _parse_checkpoint_name("sac_798_steps.zip")  # type: ignore[misc]
        assert ts == _OLD_TS
        assert steps == 798

    def test_unrecognised_returns_none(self):
        assert _parse_checkpoint_name("model.zip") is None
        assert _parse_checkpoint_name("sac_bad_steps.zip") is None
        assert _parse_checkpoint_name("sac_steps.zip") is None
        assert _parse_checkpoint_name("") is None


# ---------------------------------------------------------------- find_latest_checkpoint

class TestFindLatestCheckpoint:
    def test_empty_dirs_returns_none(self, tmp_path):
        assert find_latest_checkpoint(tmp_path) is None

    def test_missing_subdirs_returns_none(self, tmp_path):
        assert find_latest_checkpoint(tmp_path) is None

    def test_single_new_format(self, tmp_path):
        (tmp_path / "fresh").mkdir()
        (tmp_path / "fresh" / "sac_20260627-143022_3990_steps.zip").touch()
        result = find_latest_checkpoint(tmp_path)
        assert result is not None
        assert result.name == "sac_20260627-143022_3990_steps.zip"

    def test_single_old_format(self, tmp_path):
        (tmp_path / "fresh").mkdir()
        (tmp_path / "fresh" / "sac_3990_steps.zip").touch()
        result = find_latest_checkpoint(tmp_path)
        assert result is not None
        assert result.name == "sac_3990_steps.zip"

    def test_highest_step_within_same_run(self, tmp_path):
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "sac_20260627-143022_800_steps.zip").touch()
        (fresh / "sac_20260627-143022_3990_steps.zip").touch()
        result = find_latest_checkpoint(tmp_path)
        assert result is not None
        assert result.name == "sac_20260627-143022_3990_steps.zip"

    def test_newer_run_beats_older_run_even_with_lower_steps(self, tmp_path):
        """Spec: latest = newest run_ts, highest steps within that run."""
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "sac_20260101-120000_3990_steps.zip").touch()  # old run, high steps
        (fresh / "sac_20260627-150000_800_steps.zip").touch()   # new run, low steps
        result = find_latest_checkpoint(tmp_path)
        assert result is not None
        assert result.name == "sac_20260627-150000_800_steps.zip"

    def test_new_format_beats_old_format(self, tmp_path):
        """Any new-format file beats any old-format file (sentinel ts '0...')."""
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "sac_99999_steps.zip").touch()             # old format, enormous steps
        (fresh / "sac_20260101-000000_100_steps.zip").touch()  # new format, tiny steps
        result = find_latest_checkpoint(tmp_path)
        assert result is not None
        assert result.name == "sac_20260101-000000_100_steps.zip"

    def test_searches_both_fresh_and_played(self, tmp_path):
        (tmp_path / "fresh").mkdir()
        (tmp_path / "played").mkdir()
        (tmp_path / "fresh" / "sac_20260101-120000_3990_steps.zip").touch()
        (tmp_path / "played" / "sac_20260627-150000_3990_steps.zip").touch()
        result = find_latest_checkpoint(tmp_path)
        assert result is not None
        assert "played" in str(result)
        assert "20260627" in result.name

    def test_ignores_unrecognised_filenames(self, tmp_path):
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "other_model.zip").touch()
        (fresh / "sac_20260627-143022_3990_steps.zip").touch()
        result = find_latest_checkpoint(tmp_path)
        assert result is not None
        assert result.name == "sac_20260627-143022_3990_steps.zip"
