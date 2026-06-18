"""Wire protocol for the streaming server.

All messages are length-implicit (one WebSocket frame = one message). Each
message begins with a single byte `type` that selects the layout:

    0x01 world_config        [u8][u32 json_len][bytes json]
    0x02 initial_state       [u8][f64 ts][u16 N][(u16 id, u8 awake, 7×f32 pose)] × N
    0x03 snapshot            [u8][f64 ts][u32 seq][u8 N][(u16 id, 7×f32 pose)] × N
    0x04 sleep_event         [u8][f64 ts][u16 id][7×f32 final_pose]
    0x05 wake_event          [u8][f64 ts][u16 id]
    0x07 heartbeat           [u8][f64 ts]
    0x08 collapse_event      [u8][f64 ts]

All multi-byte *binary* fields are little-endian. The world_config payload
itself is a UTF-8 JSON document; only its u32 length prefix is binary.

Pose = (px, py, pz, qx, qy, qz, qw).

Note: 0x06 was reserved for `active_block_changed` in earlier design drafts
but was removed when the Godot client dropped the gripper visual; the gap
in the enum is intentional.

----------------------------------------------------------------------
レビューノート（日本語）
----------------------------------------------------------------------
目的:
    Godot クライアントとの WebSocket 通信プロトコル。pack_* で構築、
    parse_message でディスパッチ。

設計上のポイント:
    - 1 WebSocket フレーム = 1 メッセージ。長さ前置は持たない。
    - 先頭 1 byte が type byte。残りは type ごとの固定スキーマ（u16/u32/f32/f64）。
      world_config だけは JSON 可変長で、サイズを u32 prefix で持つ。
    - エンディアン: バイナリは全部 little-endian（"<" prefix）。
      JSON ペイロードは UTF-8 text（バイナリの little-endian 規約とは別概念）。
    - Pose は (px, py, pz, qx, qy, qz, qw) の 7-tuple。f32 で量子化済みではない。
    - 0x06 は欠番（設計途中で削除した active_block_changed の予約番号）。
      Godot クライアント側 ws_client.gd の match 文と整合。

レビューで見る観点:
    - struct.pack のフォーマット文字列と parse_message のオフセット計算が対応するか。
    - 新メッセージ種別を足す時は _PARSERS に dispatcher を追加するだけで済む構造か。
"""
from __future__ import annotations

import json
import struct
from collections.abc import Callable, Iterable
from enum import IntEnum
from typing import Any

import pybullet as p

Pose = tuple[float, float, float, float, float, float, float]  # px,py,pz,qx,qy,qz,qw


class MsgType(IntEnum):
    WORLD_CONFIG = 0x01
    INITIAL_STATE = 0x02
    SNAPSHOT = 0x03
    SLEEP_EVENT = 0x04
    WAKE_EVENT = 0x05
    # 0x06 reserved (was `active_block_changed`; removed per design).
    HEARTBEAT = 0x07
    COLLAPSE_EVENT = 0x08


def pose_from_body(body_id: int) -> Pose:
    """Read a body's pose and return it as the flat 7-tuple the wire uses."""
    pos, quat = p.getBasePositionAndOrientation(body_id)
    return (pos[0], pos[1], pos[2], quat[0], quat[1], quat[2], quat[3])


# ---------------------------------------------------------------- pack helpers


def pack_world_config(config: dict[str, Any]) -> bytes:
    """`config` is sent as JSON (variable structure, only once at connect)."""
    payload = json.dumps(config, separators=(",", ":")).encode("utf-8")
    return struct.pack("<BI", MsgType.WORLD_CONFIG, len(payload)) + payload


def pack_initial_state(
    timestamp: float, blocks: Iterable[tuple[int, bool, Pose]]
) -> bytes:
    """blocks: iterable of (block_id, awake_flag, pose)."""
    rows = list(blocks)
    header = struct.pack("<BdH", MsgType.INITIAL_STATE, timestamp, len(rows))
    body_parts = [
        struct.pack("<HB7f", bid, 1 if awake else 0, *pose)
        for bid, awake, pose in rows
    ]
    return header + b"".join(body_parts)


def pack_snapshot(
    timestamp: float, seq: int, awake_blocks: Iterable[tuple[int, Pose]]
) -> bytes:
    """awake_blocks: iterable of (block_id, pose). num_awake fits in u8 (<=255)."""
    # AWAKE ブロック数は通常 0〜2（運搬中の 1 + たまに崩落で増える）。
    # 設計上の上限が 255 を超える可能性は無いが、念のため assert。
    rows = list(awake_blocks)
    assert len(rows) <= 255, "u8 num_awake overflow"
    header = struct.pack("<BdIB", MsgType.SNAPSHOT, timestamp, seq, len(rows))
    body_parts = [struct.pack("<H7f", bid, *pose) for bid, pose in rows]
    return header + b"".join(body_parts)


def pack_sleep_event(timestamp: float, block_id: int, final_pose: Pose) -> bytes:
    return struct.pack(
        "<BdH7f", MsgType.SLEEP_EVENT, timestamp, block_id, *final_pose
    )


def pack_wake_event(timestamp: float, block_id: int) -> bytes:
    return struct.pack("<BdH", MsgType.WAKE_EVENT, timestamp, block_id)


def pack_heartbeat(timestamp: float) -> bytes:
    return struct.pack("<Bd", MsgType.HEARTBEAT, timestamp)


def pack_collapse_event(timestamp: float) -> bytes:
    return struct.pack("<Bd", MsgType.COLLAPSE_EVENT, timestamp)


# --------------------------------------------------------------- parse helpers


def _parse_world_config(data: bytes) -> dict[str, Any]:
    (json_len,) = struct.unpack_from("<I", data, 1)
    config = json.loads(data[5 : 5 + json_len].decode("utf-8"))
    return {"config": config}


def _parse_initial_state(data: bytes) -> dict[str, Any]:
    timestamp, num_blocks = struct.unpack_from("<dH", data, 1)
    blocks = []
    offset = 1 + 8 + 2
    row_size = 2 + 1 + 7 * 4
    for _ in range(num_blocks):
        bid, awake, *pose = struct.unpack_from("<HB7f", data, offset)
        blocks.append({"id": bid, "awake": bool(awake), "pose": tuple(pose)})
        offset += row_size
    return {"timestamp": timestamp, "blocks": blocks}


def _parse_snapshot(data: bytes) -> dict[str, Any]:
    timestamp, seq, num_awake = struct.unpack_from("<dIB", data, 1)
    blocks = []
    offset = 1 + 8 + 4 + 1
    row_size = 2 + 7 * 4
    for _ in range(num_awake):
        bid, *pose = struct.unpack_from("<H7f", data, offset)
        blocks.append({"id": bid, "pose": tuple(pose)})
        offset += row_size
    return {"timestamp": timestamp, "seq": seq, "blocks": blocks}


def _parse_sleep_event(data: bytes) -> dict[str, Any]:
    ts, bid, *pose = struct.unpack_from("<dH7f", data, 1)
    return {"timestamp": ts, "id": bid, "pose": tuple(pose)}


def _parse_wake_event(data: bytes) -> dict[str, Any]:
    ts, bid = struct.unpack_from("<dH", data, 1)
    return {"timestamp": ts, "id": bid}


def _parse_timestamp_only(data: bytes) -> dict[str, Any]:
    (ts,) = struct.unpack_from("<d", data, 1)
    return {"timestamp": ts}


_PARSERS: dict[MsgType, Callable[[bytes], dict[str, Any]]] = {
    MsgType.WORLD_CONFIG: _parse_world_config,
    MsgType.INITIAL_STATE: _parse_initial_state,
    MsgType.SNAPSHOT: _parse_snapshot,
    MsgType.SLEEP_EVENT: _parse_sleep_event,
    MsgType.WAKE_EVENT: _parse_wake_event,
    MsgType.HEARTBEAT: _parse_timestamp_only,
    MsgType.COLLAPSE_EVENT: _parse_timestamp_only,
}


def parse_message(data: bytes) -> tuple[MsgType, dict[str, Any]]:
    """Decode the first byte and dispatch to the appropriate parser.

    Returns (msg_type, payload_dict). Raises ValueError on bad type.
    """
    if not data:
        raise ValueError("empty message")
    raw_type = data[0]
    try:
        msg_type = MsgType(raw_type)
    except ValueError as e:
        raise ValueError(f"unknown message type byte: 0x{raw_type:02x}") from e
    parser = _PARSERS.get(msg_type)
    if parser is None:
        raise ValueError(f"unhandled msg_type: {msg_type}")
    return msg_type, parser(data)
