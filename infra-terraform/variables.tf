variable "region" {
  description = "AWS region"
  type        = string
  default     = "ap-northeast-1"
}

variable "env" {
  description = "Environment name (prod / dev)"
  type        = string
  default     = "prod"
}

variable "app_bucket" {
  description = "S3 bucket for models, world_state, configs (作成は事前に手動 or bootstrap)"
  type        = string
}

variable "domain_zone" {
  description = "Route 53 hosted zone name (例: example.com)"
  type        = string
}

variable "domain_name" {
  description = "DNS name for the streamer (例: bs.example.com)"
  type        = string
}

variable "ecr_account_id" {
  description = "ECR images が置かれる AWS アカウント ID (通常はデプロイ先と同一)"
  type        = string
  default     = ""  # "" の場合はカレントアカウントを使う
}

variable "spot_fallback_instance_types_learner" {
  description = "学習用 Spot のフォールバックインスタンス候補 (CPU-only, 同 8 物理コア x86 系)"
  type        = list(string)
  default     = ["c6a.4xlarge", "c6i.4xlarge", "c7a.4xlarge", "m6a.4xlarge"]
}
