"""Tests for the streaming layer: protocol round-trips and sleep tracker."""
from __future__ import annotations

from pathlib import Path

import pybullet as p
import pytest

from block_stacker.config import PhysicsConfig, WorldConfig
from block_stacker.sim.blocks import create_block
from block_stacker.sim.world import setup_world
from block_stacker.streaming.protocol import (
    MsgType,
    pack_collapse_event,
    pack_heartbeat,
    pack_initial_state,
    pack_snapshot,
    pack_sleep_event,
    pack_wake_event,
    pack_world_config,
    parse_message,
)
from block_stacker.streaming.sleep_tracker import SleepTracker

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS_DIR = REPO_ROOT / "configs"


@pytest.fixture
def world_cfg() -> WorldConfig:
    return WorldConfig.from_yaml(CONFIGS_DIR / "world.yaml")


@pytest.fixture
def physics_cfg() -> PhysicsConfig:
    return PhysicsConfig.from_yaml(CONFIGS_DIR / "physics.yaml")


# ---------------------------------------------------------------- Protocol RT


def test_world_config_roundtrip() -> None:
    payload = {"work_area": {"x_range": [-1, 1]}, "blocks": [{"id": 7}]}
    blob = pack_world_config(payload)
    assert blob[0] == MsgType.WORLD_CONFIG
    mtype, decoded = parse_message(blob)
    assert mtype == MsgType.WORLD_CONFIG
    assert decoded["config"] == payload


def test_initial_state_roundtrip() -> None:
    rows = [
        (3, True, (0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 1.0)),
        (4, False, (-0.1, -0.2, 0.05, 0.0, 0.0, 0.707, 0.707)),
    ]
    blob = pack_initial_state(1.234, rows)
    mtype, decoded = parse_message(blob)
    assert mtype == MsgType.INITIAL_STATE
    assert decoded["timestamp"] == pytest.approx(1.234, rel=1e-6)
    assert len(decoded["blocks"]) == 2
    assert decoded["blocks"][0]["id"] == 3
    assert decoded["blocks"][0]["awake"] is True
    assert decoded["blocks"][1]["awake"] is False
    for orig, dec in zip(rows, decoded["blocks"]):
        for a, b in zip(orig[2], dec["pose"]):
            assert a == pytest.approx(b, abs=1e-5)


def test_snapshot_roundtrip() -> None:
    rows = [
        (1, (0.5, 0.5, 0.05, 0.0, 0.0, 0.0, 1.0)),
        (2, (-0.5, 0.0, 0.10, 0.0, 0.0, 0.707, 0.707)),
    ]
    blob = pack_snapshot(99.99, 7, rows)
    mtype, decoded = parse_message(blob)
    assert mtype == MsgType.SNAPSHOT
    assert decoded["seq"] == 7
    assert decoded["timestamp"] == pytest.approx(99.99, rel=1e-6)
    assert len(decoded["blocks"]) == 2
    assert decoded["blocks"][1]["id"] == 2


def test_event_roundtrips() -> None:
    blob = pack_sleep_event(1.0, 42, (0.0, 0.0, 0.025, 0.0, 0.0, 0.0, 1.0))
    m, d = parse_message(blob)
    assert m == MsgType.SLEEP_EVENT
    assert d["id"] == 42

    blob = pack_wake_event(2.0, 43)
    m, d = parse_message(blob)
    assert m == MsgType.WAKE_EVENT
    assert d["id"] == 43

    blob = pack_heartbeat(3.0)
    m, d = parse_message(blob)
    assert m == MsgType.HEARTBEAT
    assert d["timestamp"] == pytest.approx(3.0, rel=1e-6)

    blob = pack_collapse_event(4.0)
    m, d = parse_message(blob)
    assert m == MsgType.COLLAPSE_EVENT


def test_unknown_message_type_raises() -> None:
    with pytest.raises(ValueError):
        parse_message(bytes([0xFF, 0, 0]))


def test_empty_message_raises() -> None:
    with pytest.raises(ValueError):
        parse_message(b"")


# ---------------------------------------------------------------- Sleep tracker


def test_sleep_tracker_settles_to_asleep(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig
) -> None:
    """A block dropped from 5cm should produce a sleep event after settling."""
    world = setup_world(world_cfg, physics_cfg, gui=False)
    try:
        cube = world_cfg.shapes["cube"]
        block = create_block(cube, (0.0, 0.0, 0.05), physics=physics_cfg)
        tracker = SleepTracker(
            lin_threshold=physics_cfg.sleep_lin_vel,
            ang_threshold=physics_cfg.sleep_ang_vel,
            stable_frames=physics_cfg.sleep_stable_frames,
        )
        tracker.register([block.body_id], awake=True)

        sleep_seen = False
        for _ in range(2 * physics_cfg.internal_rate_hz):  # up to 2 simulated seconds
            world.step()
            sleeps, wakes = tracker.step([block.body_id])
            if sleeps:
                sleep_seen = True
                assert sleeps[0][0] == block.body_id
                # final pose should be near ground
                assert sleeps[0][1][2] < 0.05
                break
        assert sleep_seen, "block never reported asleep"
        assert not tracker.is_awake(block.body_id)
    finally:
        world.disconnect()


def test_sleep_tracker_wake_after_perturb(
    world_cfg: WorldConfig, physics_cfg: PhysicsConfig
) -> None:
    """A pushed asleep block should produce a wake event."""
    world = setup_world(world_cfg, physics_cfg, gui=False)
    try:
        cube = world_cfg.shapes["cube"]
        block = create_block(cube, (0.0, 0.0, 0.025), physics=physics_cfg)
        tracker = SleepTracker(
            lin_threshold=physics_cfg.sleep_lin_vel,
            ang_threshold=physics_cfg.sleep_ang_vel,
            stable_frames=physics_cfg.sleep_stable_frames,
        )
        tracker.register([block.body_id], awake=True)
        # Settle.
        for _ in range(2 * physics_cfg.internal_rate_hz):
            world.step()
            tracker.step([block.body_id])
            if not tracker.is_awake(block.body_id):
                break
        assert not tracker.is_awake(block.body_id), "did not enter sleep first"

        # Push it.
        p.applyExternalForce(block.body_id, -1, [0, 0, 10], [0, 0, 0], p.WORLD_FRAME)
        woke = False
        for _ in range(30):
            world.step()
            _sleeps, wakes = tracker.step([block.body_id])
            if wakes:
                woke = True
                assert wakes[0] == block.body_id
                break
        assert woke, "tracker did not emit wake event after perturbation"
    finally:
        world.disconnect()
