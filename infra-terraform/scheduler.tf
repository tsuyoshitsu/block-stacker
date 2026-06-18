# EventBridge Scheduler を 2 系統に分離:
#   学習      隔週土曜 14-22 JST (月 2 回 × 8h = 16h/月)
#   デモ+配信 平日 14-22 JST    (月 22 日 × 8h = 176h/月)
#
# Lambda は 1 ペア（scale_up / scale_down）を共有し、scheduler の input payload で
# どの ASG を対象にするかを伝える。Lambda 側は handler.py の _resolve_asg_names を参照。
#
# Lambda パッケージは lambda/build.ps1 で lambda_scheduler.zip を作る。

resource "aws_lambda_function" "scale_up" {
  function_name = "bs-scale-up"
  filename      = "${path.module}/lambda_scheduler.zip"
  handler       = "handler.scale_up"
  runtime       = "python3.12"
  role          = aws_iam_role.lambda.arn
  timeout       = 60

  source_code_hash = filebase64sha256("${path.module}/lambda_scheduler.zip")

  # 環境変数 ASG_NAMES は「payload で指定がない場合の fallback」用。
  # 手動 invoke で全 ASG を一括スケールする時にだけ参照される。
  environment {
    variables = {
      ASG_NAMES = jsonencode([
        aws_autoscaling_group.streamer.name,
        aws_autoscaling_group.demo.name,
        aws_autoscaling_group.learner.name,
      ])
    }
  }
}

resource "aws_lambda_function" "scale_down" {
  function_name = "bs-scale-down"
  filename      = "${path.module}/lambda_scheduler.zip"
  handler       = "handler.scale_down"
  runtime       = "python3.12"
  role          = aws_iam_role.lambda.arn
  timeout       = 60

  source_code_hash = filebase64sha256("${path.module}/lambda_scheduler.zip")

  environment {
    variables = {
      ASG_NAMES = jsonencode([
        aws_autoscaling_group.streamer.name,
        aws_autoscaling_group.demo.name,
        aws_autoscaling_group.learner.name,
      ])
    }
  }
}

# ---- 学習 ASG: 隔週土曜 14-22 JST (= 05:00-13:00 UTC) ----
# AWS EventBridge cron は SAT#2 / SAT#4 で第 2・第 4 土曜を指定可能。

resource "aws_scheduler_schedule" "learner_start" {
  name = "bs-learner-start"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(0 5 ? * SAT#2,SAT#4 *)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.scale_up.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({
      asg_names = [aws_autoscaling_group.learner.name]
    })
  }
}

resource "aws_scheduler_schedule" "learner_stop" {
  name = "bs-learner-stop"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(0 13 ? * SAT#2,SAT#4 *)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.scale_down.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({
      asg_names = [aws_autoscaling_group.learner.name]
    })
  }
}

# ---- デモ + 配信 ASG: 平日 14-22 JST (= 05:00-13:00 UTC, MON-FRI) ----
# デモが起動すると配信も必要なので、両方を同じ payload で同時にスケールする。

resource "aws_scheduler_schedule" "demo_start" {
  name = "bs-demo-start"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(0 5 ? * MON-FRI *)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.scale_up.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({
      asg_names = [
        aws_autoscaling_group.demo.name,
        aws_autoscaling_group.streamer.name,
      ]
    })
  }
}

resource "aws_scheduler_schedule" "demo_stop" {
  name = "bs-demo-stop"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(0 13 ? * MON-FRI *)"
  schedule_expression_timezone = "UTC"

  target {
    arn      = aws_lambda_function.scale_down.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({
      asg_names = [
        aws_autoscaling_group.demo.name,
        aws_autoscaling_group.streamer.name,
      ]
    })
  }
}
