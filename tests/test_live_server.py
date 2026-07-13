"""Smoke tests for serving/live_server.py (Steps A–C, no long-running processes)."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import torch

from block_stacker.serving.live_server import (
    LiveCallback,
    WeightSyncer,
    _apply_live_resume,
    _resolve_model,
)


# ----------------------------------------------------------------- helpers


class _FakePolicy:
    def __init__(self, value: float = 1.0) -> None:
        self._sd: dict = {"w": torch.tensor([value, value])}

    def state_dict(self) -> dict:
        return self._sd

    def load_state_dict(self, sd: dict) -> None:
        self._sd = sd


class _FakeModel:
    def __init__(self, value: float = 1.0) -> None:
        self.policy = _FakePolicy(value)
        self.num_timesteps = 0


# ----------------------------------------------------------------- _resolve_model


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


# ----------------------------------------------------------------- WeightSyncer


class TestWeightSyncer:
    def test_pull_returns_false_when_empty(self) -> None:
        syncer = WeightSyncer()
        assert syncer.pull(_FakeModel()) is False

    def test_push_then_pull_transfers_weights(self) -> None:
        syncer = WeightSyncer()
        push_model = _FakeModel(value=3.0)
        pull_model = _FakeModel(value=0.0)

        syncer.push(push_model)
        assert syncer.sync_count == 1
        assert syncer.pull(pull_model) is True
        assert torch.allclose(pull_model.policy._sd["w"], torch.tensor([3.0, 3.0]))

    def test_pull_consumes_pending(self) -> None:
        syncer = WeightSyncer()
        syncer.push(_FakeModel())
        pull_model = _FakeModel()
        syncer.pull(pull_model)
        assert syncer.pull(pull_model) is False  # already consumed

    def test_push_clones_tensor(self) -> None:
        syncer = WeightSyncer()
        push_model = _FakeModel(value=1.0)
        syncer.push(push_model)
        # Mutate source after push — pull should see the clone, not the mutation
        push_model.policy._sd["w"] = torch.tensor([99.0, 99.0])
        pull_model = _FakeModel(value=0.0)
        syncer.pull(pull_model)
        assert torch.allclose(pull_model.policy._sd["w"], torch.tensor([1.0, 1.0]))

    def test_thread_safety_no_crash(self) -> None:
        syncer = WeightSyncer()
        push_model = _FakeModel(value=2.0)
        pull_model = _FakeModel(value=0.0)

        def _push_loop() -> None:
            for _ in range(100):
                syncer.push(push_model)

        t = threading.Thread(target=_push_loop)
        t.start()
        for _ in range(100):
            syncer.pull(pull_model)
        t.join()
        # Verifies no crash / deadlock


# ----------------------------------------------------------------- LiveCallback


class TestLiveCallback:
    def _make_cb(
        self,
        sync_every: int = 10,
        *,
        stopped: bool = False,
    ) -> tuple[LiveCallback, threading.Event, WeightSyncer]:
        event = threading.Event()
        if stopped:
            event.set()
        syncer = WeightSyncer()
        cb = LiveCallback(event, syncer, sync_every)
        cb.model = _FakeModel()  # type: ignore[assignment]
        cb.n_calls = 0
        return cb, event, syncer

    def test_returns_true_while_not_stopped(self) -> None:
        cb, _, _ = self._make_cb()
        assert cb._on_step() is True

    def test_returns_false_when_event_set(self) -> None:
        cb, event, _ = self._make_cb()
        event.set()
        assert cb._on_step() is False

    def test_does_not_sync_before_interval(self) -> None:
        cb, _, syncer = self._make_cb(sync_every=5)
        for _ in range(4):
            cb._on_step()
        assert syncer.sync_count == 0

    def test_syncs_at_interval(self) -> None:
        cb, _, syncer = self._make_cb(sync_every=3)
        for _ in range(3):
            cb._on_step()
        assert syncer.sync_count == 1

    def test_syncs_on_training_end(self) -> None:
        cb, _, syncer = self._make_cb(sync_every=100)
        cb._on_training_end()
        assert syncer.sync_count == 1

    def test_sync_resets_counter(self) -> None:
        cb, _, syncer = self._make_cb(sync_every=3)
        for _ in range(6):
            cb._on_step()
        assert syncer.sync_count == 2


# ----------------------------------------------------------------- _apply_live_resume


class TestApplyLiveResume:
    def _fake_model(self, is_weighted: bool = False) -> MagicMock:
        model = MagicMock()
        if is_weighted:
            from block_stacker.policy.weighted_replay_buffer import WeightedReplayBuffer
            buf = MagicMock(spec=WeightedReplayBuffer)
            buf.global_step = 0
            buf.decay_rate = 0.9999
        else:
            buf = MagicMock()
        model.replay_buffer = buf
        return model

    def test_no_buf_file_no_crash(self, tmp_path: Path) -> None:
        model = self._fake_model()
        _apply_live_resume(model, tmp_path, {})
        model.load_replay_buffer.assert_not_called()

    def test_loads_buf_when_file_exists(self, tmp_path: Path) -> None:
        buf_path = tmp_path / "replay_buffer.pkl"
        buf_path.write_bytes(b"fake")
        model = self._fake_model(is_weighted=False)
        _apply_live_resume(model, tmp_path, {})
        model.load_replay_buffer.assert_called_once_with(str(buf_path))

    def test_applies_time_decay_to_weighted_buffer(self, tmp_path: Path) -> None:
        (tmp_path / "replay_buffer.pkl").write_bytes(b"fake")
        state = {"timestamp": "2020-01-01T00:00:00", "num_timesteps": 1000}
        (tmp_path / "resume_state.json").write_text(json.dumps(state), encoding="utf-8")
        model = self._fake_model(is_weighted=True)
        # steps_per_day=5000, elapsed_days would be > 0 since 2020
        _apply_live_resume(model, tmp_path, {"steps_per_day": 5000})
        assert model.replay_buffer.global_step > 0

    def test_no_decay_when_no_resume_state(self, tmp_path: Path) -> None:
        (tmp_path / "replay_buffer.pkl").write_bytes(b"fake")
        model = self._fake_model(is_weighted=True)
        # No resume_state.json → elapsed=0 → global_step unchanged
        _apply_live_resume(model, tmp_path, {"steps_per_day": 5000})
        assert model.replay_buffer.global_step == 0

    def test_elapsed_steps_override(self, tmp_path: Path) -> None:
        (tmp_path / "replay_buffer.pkl").write_bytes(b"fake")
        model = self._fake_model(is_weighted=True)
        _apply_live_resume(model, tmp_path, {"elapsed_steps": 99})
        assert model.replay_buffer.global_step == 99
