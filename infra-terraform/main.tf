# block-stacker AWS インフラ Terraform エントリ。
# 設計書 §8 と docs/aws_deployment.md に対応。
#
# Backend は S3 + DynamoDB lock 想定。最初の terraform init 前に
# bs-tfstate-<ACCOUNT_ID> と bs-tfstate-lock を作成しておくこと。
# （手順は docs/aws_deployment.md §2.2 参照）

terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    # この値は init 時に -backend-config="bucket=..." で渡すか、ここを直接書き換える
    # bucket         = "bs-tfstate-<ACCOUNT_ID>"
    key            = "infra/terraform.tfstate"
    region         = "ap-northeast-1"
    dynamodb_table = "bs-tfstate-lock"
    encrypt        = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "block-stacker"
      ManagedBy = "terraform"
      Env       = var.env
    }
  }
}

# 共通 data sources
data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
