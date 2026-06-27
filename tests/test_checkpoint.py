"""Tests for checkpoint filename parsing and discovery (new + legacy formats)."""
from __future__ import annotations

import pytest

from block_stacker.mvp2.checkpoint import (
    _OLD_TS,
    _parse_checkpoint_name,
    find_latest_checkpoint,
    list_checkpoints_sorted,
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


# ---------------------------------------------------------------- list_checkpoints_sorted

class TestListCheckpointsSorted:
    def test_empty_subdir(self, tmp_path):
        (tmp_path / "fresh").mkdir()
        assert list_checkpoints_sorted(tmp_path, "fresh") == []

    def test_missing_subdir(self, tmp_path):
        assert list_checkpoints_sorted(tmp_path, "fresh") == []

    def test_ascending_within_same_run(self, tmp_path):
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "sac_20260627-143022_1600_steps.zip").touch()
        (fresh / "sac_20260627-143022_800_steps.zip").touch()
        (fresh / "sac_20260627-143022_3990_steps.zip").touch()
        result = list_checkpoints_sorted(tmp_path, "fresh")
        steps = [r[1] for r in result]
        assert steps == [800, 1600, 3990]

    def test_older_run_before_newer_run(self, tmp_path):
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "sac_20260627-150000_400_steps.zip").touch()   # newer run
        (fresh / "sac_20260101-120000_3990_steps.zip").touch()  # older run, higher steps
        result = list_checkpoints_sorted(tmp_path, "fresh")
        # Old run sorts first (smaller ts)
        assert result[0][0] == "20260101-120000"
        assert result[1][0] == "20260627-150000"

    def test_old_format_sorts_first(self, tmp_path):
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "sac_20260101-000000_800_steps.zip").touch()
        (fresh / "sac_3990_steps.zip").touch()  # old format → sentinel ts
        result = list_checkpoints_sorted(tmp_path, "fresh")
        # Old format gets '00000000-000000' → sorts before new format
        assert result[0][2].name == "sac_3990_steps.zip"
        assert result[1][2].name == "sac_20260101-000000_800_steps.zip"

    def test_returns_run_ts_steps_path_tuple(self, tmp_path):
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "sac_20260627-143022_3990_steps.zip").touch()
        result = list_checkpoints_sorted(tmp_path, "fresh")
        assert len(result) == 1
        ts, steps, path = result[0]
        assert ts == "20260627-143022"
        assert steps == 3990
        assert path.name == "sac_20260627-143022_3990_steps.zip"
