"""EventBridge Scheduler が呼び出す scale_up / scale_down Lambda。

scale_up:
    - 当日が日本の祝日でなければ、指定されたインスタンスを start する。
    - 祝日ならスキップ + ログ出力。

scale_down:
    - 祝日判定なしで指定されたインスタンスを stop する。

対象インスタンスの決定:
    - event["instance_ids"] (list[str]) があればそれを使う（払い出された payload 駆動）。
    - 無ければ環境変数 INSTANCE_IDS (JSON 配列文字列) の全リストを使う。
      → 手動 invoke で全インスタンスを一括制御する用。

ASG からの移行について:
    かつては ASG の desired_capacity を 0/1 する実装だったが、min=0/max=1 で
    スケールしておらず実態は「起動/停止スイッチ」だったため、単一 EC2 の
    start/stop に置き換えた（docs/design_change_record.md）。
    stop は terminate と違い EBS が保持されるので、live_server のスナップショットや
    world_state がインスタンス上に残る。

スケジュール構成（70_lambda.ps1 と一致させる。時刻は暫定）:
    bs-learner-start  cron(0 0 1 * ? *)       payload {"instance_ids": ["<learner>"]}
    bs-learner-stop   cron(0 2 1 * ? *)       同上
    bs-live-start     cron(0 1 ? * MON-FRI *) payload {"instance_ids": ["<live>", "<streamer>"]}
    bs-live-stop      cron(0 9 ? * MON-FRI *) 同上
"""
import json
import logging
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import boto3
import jpholiday

LOG = logging.getLogger()
LOG.setLevel(logging.INFO)

EC2 = boto3.client("ec2")
JST = ZoneInfo("Asia/Tokyo")


def _resolve_instance_ids(event) -> list[str]:
    """払い出された payload に instance_ids があればそれを、なければ env var を使う。"""
    if isinstance(event, dict) and event.get("instance_ids"):
        return list(event["instance_ids"])
    raw = os.environ.get("INSTANCE_IDS", "[]")
    return list(json.loads(raw))


def scale_up(event, context):
    today = datetime.now(JST).date()
    if jpholiday.is_holiday(today):
        name = jpholiday.is_holiday_name(today)
        LOG.info("Today (%s) is a Japanese holiday (%s); skipping start",
                 today.isoformat(), name)
        return {"skipped": True, "reason": "holiday", "date": today.isoformat()}

    ids = _resolve_instance_ids(event)
    if not ids:
        LOG.warning("no instance ids resolved; nothing to start")
        return {"started": [], "date": today.isoformat()}

    EC2.start_instances(InstanceIds=ids)
    LOG.info("start: %s", ids)
    return {"started": ids, "date": today.isoformat()}


def scale_down(event, context):
    ids = _resolve_instance_ids(event)
    today = datetime.now(JST).date()
    if not ids:
        LOG.warning("no instance ids resolved; nothing to stop")
        return {"stopped": [], "date": today.isoformat()}

    EC2.stop_instances(InstanceIds=ids)
    LOG.info("stop: %s", ids)
    return {"stopped": ids, "date": today.isoformat()}
