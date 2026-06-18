# 既存の app S3 バケットを Terraform 管理下に取り込む。
# 事前作成手順は docs/aws_deployment.md §2.3。
#
# import 例:
#   terraform import aws_s3_bucket.app bs-app-<ACCOUNT_ID>

data "aws_s3_bucket" "app" {
  bucket = var.app_bucket
}

# バージョニングを「もし無効なら有効化」、Terraform 管理で表明
resource "aws_s3_bucket_versioning" "app" {
  bucket = data.aws_s3_bucket.app.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "app" {
  bucket                  = data.aws_s3_bucket.app.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "app" {
  bucket = data.aws_s3_bucket.app.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
