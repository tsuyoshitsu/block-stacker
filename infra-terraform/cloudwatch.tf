# CloudWatch Logs グループ + 最低限のアラート。
# 詳細ダッシュボードは docs/aws_deployment.md 付録 C を参照。

resource "aws_cloudwatch_log_group" "streamer" {
  name              = "/aws/ec2/bs-streamer"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "demo" {
  name              = "/aws/ec2/bs-demo"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "learner" {
  name              = "/aws/ec2/bs-learner"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "lambda_scale_up" {
  name              = "/aws/lambda/bs-scale-up"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "lambda_scale_down" {
  name              = "/aws/lambda/bs-scale-down"
  retention_in_days = 30
}

# ----- SNS topic for alarms -----

resource "aws_sns_topic" "alarms" {
  name = "bs-alarms"
}

# ----- Alarm: Lambda エラー -----

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "bs-lambda-scale-errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "Scaler Lambda errored"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  dimensions = {
    FunctionName = aws_lambda_function.scale_up.function_name
  }
}

# ----- Alarm: ASG が稼働時間中に desired を満たさない (健全性) -----

resource "aws_cloudwatch_metric_alarm" "demo_unhealthy" {
  alarm_name          = "bs-demo-asg-not-running"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 3
  metric_name         = "GroupInServiceInstances"
  namespace           = "AWS/AutoScaling"
  period              = 300
  statistic           = "Average"
  threshold           = 1
  alarm_description   = "Demo ASG has no running instance during operating hours"
  alarm_actions       = [aws_sns_topic.alarms.arn]
  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.demo.name
  }
  # NOTE: 平日帯は ASG=0 にしているので、稼働時間外も alarm が燃える。
  # 厳密にやるなら時間帯付き Composite Alarm にする (将来の改善余地)。
  treat_missing_data = "notBreaching"
}
