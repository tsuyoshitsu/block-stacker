"""Tests for lambda/handler.py (EC2 start/stop scheduler Lambda).

ASG 撤去（desired_capacity 0/1 → EC2 start/stop）に伴い追加。

boto3 / jpholiday は **Lambda デプロイパッケージ側の依存**でローカル .venv には
入っていない（lambda/requirements.txt 参照）。テストをスキップさせると回帰検知が
効かないので、sys.modules にスタブを差してから handler をロードする。
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HANDLER_PATH = REPO_ROOT / "lambda" / "handler.py"


class FakeEC2:
    """start_instances / stop_instances の呼び出しを記録するだけのスタブ。"""

    def __init__(self) -> None:
        self.started: list[list[str]] = []
        self.stopped: list[list[str]] = []

    def start_instances(self, InstanceIds):  # noqa: N803 (boto3 の API 名に合わせる)
        self.started.append(list(InstanceIds))

    def stop_instances(self, InstanceIds):  # noqa: N803
        self.stopped.append(list(InstanceIds))


@pytest.fixture
def handler(monkeypatch):
    """boto3 / jpholiday をスタブして handler.py をロードする。"""
    fake_ec2 = FakeEC2()

    boto3_stub = types.ModuleType("boto3")
    boto3_stub.client = lambda name, *a, **kw: fake_ec2  # type: ignore[attr-defined]

    holiday_stub = types.ModuleType("jpholiday")
    holiday_stub.is_holiday = lambda d: False            # type: ignore[attr-defined]
    holiday_stub.is_holiday_name = lambda d: None        # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "boto3", boto3_stub)
    monkeypatch.setitem(sys.modules, "jpholiday", holiday_stub)

    spec = importlib.util.spec_from_file_location("bs_lambda_handler", HANDLER_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "bs_lambda_handler", mod)
    spec.loader.exec_module(mod)

    mod._test_ec2 = fake_ec2  # type: ignore[attr-defined]
    return mod


class TestResolveInstanceIds:
    def test_payload_takes_precedence(self, handler, monkeypatch):
        monkeypatch.setenv("INSTANCE_IDS", '["i-env"]')
        assert handler._resolve_instance_ids({"instance_ids": ["i-payload"]}) == ["i-payload"]

    def test_falls_back_to_env(self, handler, monkeypatch):
        monkeypatch.setenv("INSTANCE_IDS", '["i-a", "i-b"]')
        assert handler._resolve_instance_ids({}) == ["i-a", "i-b"]

    def test_empty_when_neither_set(self, handler, monkeypatch):
        monkeypatch.delenv("INSTANCE_IDS", raising=False)
        assert handler._resolve_instance_ids({}) == []


class TestScaleUp:
    def test_starts_payload_instances(self, handler):
        out = handler.scale_up({"instance_ids": ["i-1", "i-2"]}, None)
        assert handler._test_ec2.started == [["i-1", "i-2"]]
        assert out["started"] == ["i-1", "i-2"]

    def test_skips_on_japanese_holiday(self, handler, monkeypatch):
        # 祝日 skip は ASG 時代から維持している要件。
        monkeypatch.setattr(handler.jpholiday, "is_holiday", lambda d: True)
        monkeypatch.setattr(handler.jpholiday, "is_holiday_name", lambda d: "元日")
        out = handler.scale_up({"instance_ids": ["i-1"]}, None)
        assert out["skipped"] is True
        assert handler._test_ec2.started == []      # 起動しない

    def test_no_ids_is_noop(self, handler, monkeypatch):
        monkeypatch.delenv("INSTANCE_IDS", raising=False)
        out = handler.scale_up({}, None)
        assert out["started"] == []
        assert handler._test_ec2.started == []


class TestScaleDown:
    def test_stops_payload_instances(self, handler):
        out = handler.scale_down({"instance_ids": ["i-1", "i-2"]}, None)
        assert handler._test_ec2.stopped == [["i-1", "i-2"]]
        assert out["stopped"] == ["i-1", "i-2"]

    def test_stops_even_on_holiday(self, handler, monkeypatch):
        # stop は祝日でも実行する（起動しっぱなしを防ぐため）。
        monkeypatch.setattr(handler.jpholiday, "is_holiday", lambda d: True)
        out = handler.scale_down({"instance_ids": ["i-1"]}, None)
        assert out["stopped"] == ["i-1"]
        assert handler._test_ec2.stopped == [["i-1"]]

    def test_no_ids_is_noop(self, handler, monkeypatch):
        monkeypatch.delenv("INSTANCE_IDS", raising=False)
        out = handler.scale_down({}, None)
        assert out["stopped"] == []
        assert handler._test_ec2.stopped == []


def test_uses_ec2_api_not_autoscaling() -> None:
    """回帰: ASG API に戻っていないこと（撤去済み）。"""
    src = HANDLER_PATH.read_text(encoding="utf-8")
    assert "update_auto_scaling_group" not in src
    assert "AutoScalingGroupName" not in src
    assert 'boto3.client("ec2")' in src
