"""Smoke tests for serving/live_server.py (Step A: no long-running processes)."""
from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from block_stacker.serving.live_server import _resolve_model


class TestResolveModel:
    def test_explicit_path_returned_directly(self, tmp_path: Path) -> None:
        fake = tmp_path / "sac_20260101-000000_1000_steps.zip"
        fake.touch()
        result = _resolve_model(tmp_path, explicit=fake)
        assert result == fake

    def test_latest_checkpoint_found_in_fresh(self, tmp_path: Path) -> None:
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        ckpt = fresh / "sac_20260101-000000_2000_steps.zip"
        ckpt.touch()
        result = _resolve_model(tmp_path, explicit=None)
        assert result == ckpt

    def test_explicit_takes_priority_over_snapshot_dir(self, tmp_path: Path) -> None:
        fresh = tmp_path / "fresh"
        fresh.mkdir()
        (fresh / "sac_20260101-000000_2000_steps.zip").touch()
        explicit = tmp_path / "sac_20260101-000000_9999_steps.zip"
        explicit.touch()
        result = _resolve_model(tmp_path, explicit=explicit)
        assert result == explicit

    def test_raises_system_exit_when_nothing_found(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            _resolve_model(tmp_path, explicit=None)

    def test_raises_system_exit_empty_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "fresh").mkdir()
        (tmp_path / "played").mkdir()
        with pytest.raises(SystemExit):
            _resolve_model(tmp_path, explicit=None)
