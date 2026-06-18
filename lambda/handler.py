"""EventBridge Scheduler が呼び出す scale_up / scale_down Lambda。

scale_up:
    - 当日が日本の祝日でなければ、指定された ASG の desired_capacity を 1 に。
    - 祝日ならスキップ + ログ出力。

scale_down:
    - 祝日判定なしで指定された ASG の desired_capacity を 0 に。

対象 ASG の決定:
    - event["asg_names"] (list[str]) があればそれを使う（払い出された payload 駆動）。
    - 無ければ環境変数 ASG_NAMES (JSON 配列文字列) の全リストを使う。
      → 手動 invoke や旧仕様の payload なしスケジュールで全 ASG を一括制御する用。

スケジュール構成（schedule.tf / 70_lambda.ps1 と一致させる）:
    bs-learner-start  cron(0 5 ? * SAT#2,SAT#4 *)  payload {"asg_names": ["bs-learner-asg"]}
    bs-learner-stop   cron(0 13 ? * SAT#2,SAT#4 *) 同上
    bs-demo-start     cron(0 5 ? * MON-FRI *)       payload {"asg_names": ["bs-demo-asg", "bs-streamer-asg"]}
    bs-demo-stop      cron(0 13 ? * MON-FRI *)      同上
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

ASG = boto3.client("autoscaling")
JST = ZoneInfo("Asia/Tokyo")


def _resolve_asg_names(event) -> list[str]:
    """払い出された payload に asg_names があればそれを、なければ env var を使う。"""
    if isinstance(event, dict) and event.get("asg_names"):
        return list(event["asg_names"])
    raw = os.environ.get("ASG_NAMES", "[]")
    return list(json.loads(raw))


def scale_up(event, context):
    today = datetime.now(JST).date()
    if jpholiday.is_holiday(today):
        name = jpholiday.is_holiday_name(today)
        LOG.info("Today (%s) is a Japanese holiday (%s); skipping scale up",
                 today.isoformat(), name)
        return {"skipped": True, "reason": "holiday", "date": today.isoformat()}

    names = _resolve_asg_names(event)
    for name in names:
        ASG.update_auto_scaling_group(AutoScalingGroupName=name, DesiredCapacity=1)
        LOG.info("scale up: %s desired=1", name)
    return {"scaled_up": names, "date": today.isoformat()}


def scale_down(event, context):
    names = _resolve_asg_names(event)
    today = datetime.now(JST).date()
    for name in names:
        ASG.update_auto_scaling_group(AutoScalingGroupName=name, DesiredCapacity=0)
        LOG.info("scale down: %s desired=0", name)
    return {"scaled_down": names, "date": today.isoformat()}
