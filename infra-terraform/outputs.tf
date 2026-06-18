output "vpc_id" {
  value       = aws_vpc.main.id
  description = "VPC ID"
}

output "streamer_eip" {
  value       = aws_eip.streamer.public_ip
  description = "Public IP for the streamer (Route 53 A record target)"
}

output "streamer_url" {
  value       = "wss://${var.domain_name}/"
  description = "Client connection URL"
}

output "app_bucket" {
  value       = var.app_bucket
  description = "S3 bucket used for models, world_state, configs"
}

output "asg_names" {
  value = {
    streamer = aws_autoscaling_group.streamer.name
    demo     = aws_autoscaling_group.demo.name
    learner  = aws_autoscaling_group.learner.name
  }
  description = "All ASG names (Lambda の ASG_NAMES env var に使う)"
}
