# VPC、サブネット、IGW、SG、S3 VPC Endpoint。
# 設計書 §8.4 ネットワーク図に対応。

resource "aws_vpc" "main" {
  cidr_block           = "10.10.0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = { Name = "bs-vpc" }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "bs-igw" }
}

# Public Subnet (配信 EC2 用)
resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.10.1.0/24"
  availability_zone       = "${var.region}a"
  map_public_ip_on_launch = false  # EIP を associate するため false
  tags                    = { Name = "bs-public-1a" }
}

# Private Subnet (デモ、学習)
resource "aws_subnet" "private" {
  vpc_id            = aws_vpc.main.id
  cidr_block        = "10.10.2.0/24"
  availability_zone = "${var.region}a"
  tags              = { Name = "bs-private-1a" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = { Name = "bs-public-rt" }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# プライベートサブネット用ルートテーブル (IGW なし、S3 だけ VPC Endpoint で抜ける)
resource "aws_route_table" "private" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "bs-private-rt" }
}

resource "aws_route_table_association" "private" {
  subnet_id      = aws_subnet.private.id
  route_table_id = aws_route_table.private.id
}

# S3 VPC エンドポイント (Gateway 型)。プライベートサブネット → S3 の転送料 $0。
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids = [
    aws_route_table.public.id,
    aws_route_table.private.id,
  ]
  tags = { Name = "bs-s3-endpoint" }
}

# ECR / CloudWatch Logs Interface Endpoint (NAT Gateway 代替、~$15/月)。
# Private Subnet の EC2 が ECR pull / CW Logs 送信を IGW 無しで行うため必須。
# ECR 実態のイメージレイヤ転送は S3 経由 → S3 Gateway Endpoint があれば追加コストなし。
resource "aws_security_group" "vpce" {
  name        = "bs-vpce"
  description = "VPC Interface Endpoints (HTTPS from VPC CIDR)"
  vpc_id      = aws_vpc.main.id

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [aws_vpc.main.cidr_block]
  }
}

resource "aws_vpc_endpoint" "ecr_api" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.ecr.api"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private.id]
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true
  tags                = { Name = "bs-ecr-api-endpoint" }
}

resource "aws_vpc_endpoint" "ecr_dkr" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.ecr.dkr"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private.id]
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true
  tags                = { Name = "bs-ecr-dkr-endpoint" }
}

resource "aws_vpc_endpoint" "logs" {
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.logs"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = [aws_subnet.private.id]
  security_group_ids  = [aws_security_group.vpce.id]
  private_dns_enabled = true
  tags                = { Name = "bs-logs-endpoint" }
}

# ----- Security Groups ------------------------------------------------

# 配信 EC2: 443 (wss) + 80 (Let's Encrypt ACME)
resource "aws_security_group" "streamer" {
  name        = "bs-streamer"
  description = "Streaming server (Caddy + WS reverse proxy)"
  vpc_id      = aws_vpc.main.id

  ingress {
    description = "HTTPS (wss)"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTP for ACME challenge"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# デモ EC2: 8765 (WebSocket) は streamer SG からのみ
resource "aws_security_group" "demo" {
  name        = "bs-demo"
  description = "Demo server (ai_server + WebSocket on :8765)"
  vpc_id      = aws_vpc.main.id

  ingress {
    description     = "WebSocket from streamer"
    from_port       = 8765
    to_port         = 8765
    protocol        = "tcp"
    security_groups = [aws_security_group.streamer.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

# 学習 EC2: inbound なし
resource "aws_security_group" "learner" {
  name        = "bs-learner"
  description = "Learner (SAC training, S3 only)"
  vpc_id      = aws_vpc.main.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

