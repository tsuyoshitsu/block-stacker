"""Configuration loading from YAML files.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    YAML 設定ファイルを frozen dataclass に load する。設計書 §7 のファイル
    分割 (world.yaml, physics.yaml, reward.yaml, training.yaml) に対応。

設計上のポイント:
    - frozen dataclass で immutable 化 → 多プロセス並列訓練 (SubprocVecEnv) で
      安全に共有できる。
    - get(...) でデフォルト値を持つフィールドは将来の YAML 拡張に互換。
    - default_configs_dir() は CONFIGS_DIR 環境変数を尊重 → Docker や
      EC2 デプロイ時にパス差し替え可。

レビューで見る観点:
    - 新しい設定キーを追加する時は (1) dataclass field 追加、(2) from_yaml で
      load、(3) YAML サンプル更新の 3 箇所同時編集が必要。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


@dataclass(frozen=True)
class ShapeSpec:
    name: str
    # 対応形状:
    #   "box"              => dims = [width_x, height_y, depth_z]
    #   "cylinder"         => dims = [radius, height]
    #   "triangular_prism" => dims = [leg_length, prism_length]
    #                         （直角二等辺三角柱、axis 沿い X、断面 YZ）
    type: str
    dims: list[float]
    density: float       # kg/m^3
    color: list[float]   # RGBA


@dataclass(frozen=True)
class InitialScatterConfig:
    exclude_radius_from_center: float  # avoid spawning here (tower predicted area)
    min_inter_block_distance: float    # rejection-sample to maintain this distance
    random_yaw: bool                   # randomize yaw on spawn


@dataclass(frozen=True)
class WorldConfig:
    x_range: tuple[float, float]
    y_range: tuple[float, float]
    z_max: float
    ground_size: tuple[float, float]
    ground_friction: float
    ground_restitution: float
    boundary_type: str
    boundary_height: float
    boundary_restitution: float
    shapes: dict[str, ShapeSpec]
    inventory: dict[str, int]
    initial_scatter: InitialScatterConfig

    @classmethod
    def from_yaml(cls, path: Path) -> WorldConfig:
        data = _load_yaml(path)
        shapes = {
            name: ShapeSpec(
                name=name,
                type=spec["type"],
                dims=list(spec["dims"]),
                density=float(spec["density"]),
                color=list(spec["color"]),
            )
            for name, spec in data["shapes"].items()
        }
        boundary = data["boundary"]
        ground = data["ground"]
        scatter_raw = data.get("initial_scatter", {})
        scatter = InitialScatterConfig(
            exclude_radius_from_center=float(scatter_raw.get("exclude_radius_from_center", 0.0)),
            min_inter_block_distance=float(scatter_raw.get("min_inter_block_distance", 0.07)),
            random_yaw=bool(scatter_raw.get("random_yaw", True)),
        )
        return cls(
            x_range=tuple(data["work_area"]["x_range"]),
            y_range=tuple(data["work_area"]["y_range"]),
            z_max=float(data["work_area"]["z_max"]),
            ground_size=tuple(ground["size"]),
            ground_friction=float(ground["friction"]),
            ground_restitution=float(ground["restitution"]),
            boundary_type=str(boundary["type"]),
            boundary_height=float(boundary.get("height", 1.0)),
            boundary_restitution=float(boundary["restitution"]),
            shapes=shapes,
            inventory=dict(data["inventory"]),
            initial_scatter=scatter,
        )


@dataclass(frozen=True)
class PhysicsConfig:
    # simulation
    internal_rate_hz: int
    solver_iterations: int
    use_split_impulse: bool
    # gravity
    gravity: tuple[float, float, float]
    # friction
    friction_block_block: float
    friction_block_ground: float
    friction_block_wall: float
    rolling_friction: float
    spinning_friction: float
    # restitution
    restitution_block: float
    restitution_ground: float
    restitution_wall: float
    # damping
    damping_linear: float
    damping_angular: float
    # contact
    contact_stiffness: float
    contact_damping: float
    # sleep
    sleep_lin_vel: float
    sleep_ang_vel: float
    sleep_stable_frames: int
    # collapse
    collapse_dispersion_ratio: float
    collapse_cooldown: float
    # carrier
    carrier_type: str
    carrier_max_force: float
    carrier_trajectory_speed: float
    carrier_approach_offset: float

    @classmethod
    def from_yaml(cls, path: Path) -> PhysicsConfig:
        data = _load_yaml(path)
        sim = data["simulation"]
        friction = data["friction"]
        rest = data["restitution"]
        damping = data["damping"]
        contact = data["contact"]
        sleep = data["sleep_detection"]
        collapse = data.get("collapse_detection", {})
        carrier = data["carrier_constraint"]
        return cls(
            internal_rate_hz=int(sim["internal_rate_hz"]),
            solver_iterations=int(sim["solver_iterations"]),
            use_split_impulse=bool(sim["use_split_impulse"]),
            gravity=tuple(data["gravity"]),
            friction_block_block=float(friction["block_to_block"]),
            friction_block_ground=float(friction["block_to_ground"]),
            friction_block_wall=float(friction["block_to_wall"]),
            rolling_friction=float(friction["rolling_friction"]),
            spinning_friction=float(friction["spinning_friction"]),
            restitution_block=float(rest["block"]),
            restitution_ground=float(rest["ground"]),
            restitution_wall=float(rest["wall"]),
            damping_linear=float(damping["linear"]),
            damping_angular=float(damping["angular"]),
            contact_stiffness=float(contact["stiffness"]),
            contact_damping=float(contact["damping"]),
            sleep_lin_vel=float(sleep["linear_velocity_threshold"]),
            sleep_ang_vel=float(sleep["angular_velocity_threshold"]),
            sleep_stable_frames=int(sleep["stable_frames_required"]),
            collapse_dispersion_ratio=float(collapse.get("tower_dispersion_ratio", 0.5)),
            collapse_cooldown=float(collapse.get("cooldown_after_collapse", 2.0)),
            carrier_type=str(carrier["type"]),
            carrier_max_force=float(carrier["max_force"]),
            carrier_trajectory_speed=float(carrier["trajectory_speed"]),
            carrier_approach_offset=float(carrier["approach_height_offset"]),
        )


@dataclass(frozen=True)
class RewardConfig:
    place_success: float
    height_record: float
    collapse: float
    time_penalty: float
    timeout_penalty: float
    collapse_height_threshold: float    # default H_high if stage doesn't override
    reset_height_threshold: float       # default H_low  if stage doesn't override

    @classmethod
    def from_yaml(cls, path: Path) -> RewardConfig:
        data = _load_yaml(path)
        return cls(
            place_success=float(data["place_success"]),
            height_record=float(data["height_record"]),
            collapse=float(data["collapse"]),
            time_penalty=float(data["time_penalty"]),
            timeout_penalty=float(data.get("timeout_penalty", -1.0)),
            collapse_height_threshold=float(data["collapse_height_threshold"]),
            reset_height_threshold=float(data["reset_height_threshold"]),
        )


def default_configs_dir() -> Path:
    """Return the configs directory, respecting CONFIGS_DIR env override.

    Falls back to <repo_root>/configs based on this file's location.
    """
    import os

    env = os.environ.get("CONFIGS_DIR")
    if env:
        return Path(env)
    # src/block_stacker/config.py -> ../../../configs
    return Path(__file__).resolve().parents[2] / "configs"
