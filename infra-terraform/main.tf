# block-stacker AWS インフラ Terraform エントリ。
#
# ⚠️ **これは参照用の「ASG 版」であり、現行のデプロイ経路ではない。**
#   現行は deploy/ の PowerShell + AWS CLI（docs/aws_deployment.md）。
#   そちらは **ASG を撤去して単一 EC2 の start/stop 方式**に移行済み。
#   この Terraform 一式は ASG（Mixed Instances Policy による Spot フォールバック、
#   中断時の自動再起動）を含む完全な構成を残してあり、将来 ASG を復活させる際の
#   設計リファレンスとして保持している。撤去の経緯は docs/design_change_record.md。
#   **現行構成と差分がある前提で読むこと**（インスタンスタイプ・スケジュール等も未追従）。
#
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
