# 配信 EC2 用 EIP + Route 53 A レコード。
# 配信 EC2 (ASG) は起動のたびに新しいインスタンスになるので、user-data の中で
# aws ec2 associate-address を呼んでこの EIP を取り直す設計。

resource "aws_eip" "streamer" {
  domain = "vpc"
  tags = {
    Name = "bs-streamer-eip"
  }
}

data "aws_route53_zone" "main" {
  name = var.domain_zone
}

resource "aws_route53_record" "streamer" {
  zone_id = data.aws_route53_zone.main.zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.streamer.public_ip]
}
