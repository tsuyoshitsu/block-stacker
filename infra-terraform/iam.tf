# IAM ロール: EC2 用、Lambda 用、EventBridge Scheduler 用。
# 付録 A (docs/aws_deployment.md) に対応。

locals {
  account_id = data.aws_caller_identity.current.account_id
  ecr_acct   = var.ecr_account_id != "" ? var.ecr_account_id : local.account_id
}

# ====================================================================
# EC2 (streamer / demo / learner 共通)
# ====================================================================

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "ec2" {
  name               = "bs-ec2-role"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

# S3 アクセス (app bucket のみ)
resource "aws_iam_role_policy" "ec2_s3" {
  name = "bs-ec2-s3"
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["s3:GetObject", "s3:PutObject", "s3:ListBucket", "s3:DeleteObject"]
      Resource = [
        "arn:aws:s3:::${var.app_bucket}",
        "arn:aws:s3:::${var.app_bucket}/*",
      ]
    }]
  })
}

# ECR pull
resource "aws_iam_role_policy" "ec2_ecr" {
  name = "bs-ec2-ecr"
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "ecr:GetAuthorizationToken",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchCheckLayerAvailability",
      ]
      Resource = "*"
    }]
  })
}

# CloudWatch Logs
resource "aws_iam_role_policy" "ec2_logs" {
  name = "bs-ec2-logs"
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "logs:DescribeLogStreams",
      ]
      Resource = "arn:aws:logs:${var.region}:${local.account_id}:*"
    }]
  })
}

# 配信 EC2 が EIP を関連付ける、デモが SSM へ private IP を書く
resource "aws_iam_role_policy" "ec2_dynamic" {
  name = "bs-ec2-dynamic"
  role = aws_iam_role.ec2.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["ec2:AssociateAddress", "ec2:DescribeAddresses", "ec2:DescribeInstances"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter", "ssm:PutParameter"]
        Resource = "arn:aws:ssm:${var.region}:${local.account_id}:parameter/bs/*"
      },
    ]
  })
}

# Systems Manager Session Manager 経由でログインしたい場合
resource "aws_iam_role_policy_attachment" "ec2_ssm" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "bs-ec2-profile"
  role = aws_iam_role.ec2.name
}

# ====================================================================
# Lambda (scale_up / scale_down)
# ====================================================================

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "bs-lambda-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "lambda_asg" {
  name = "bs-lambda-asg"
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "autoscaling:UpdateAutoScalingGroup",
        "autoscaling:DescribeAutoScalingGroups",
      ]
      Resource = "*"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "lambda_logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# ====================================================================
# EventBridge Scheduler -> Lambda 呼び出し用
# ====================================================================

data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "bs-scheduler-role"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler_invoke" {
  name = "bs-scheduler-invoke"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = ["lambda:InvokeFunction"]
      Resource = [
        aws_lambda_function.scale_up.arn,
        aws_lambda_function.scale_down.arn,
      ]
    }]
  })
}
