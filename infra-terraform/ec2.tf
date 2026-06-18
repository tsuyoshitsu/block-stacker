# EC2 起動テンプレート + Auto Scaling Group (全 Spot)。
# desired_capacity を初期値 0 にし、EventBridge Scheduler が稼働時間に 1 へ。

# ====================================================================
# AMI
# ====================================================================

# ARM (t4g.small) 用
data "aws_ami" "amzn2023_arm64" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-arm64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# x86 (c6i.xlarge) 用
data "aws_ami" "amzn2023_x86" {
  most_recent = true
  owners      = ["amazon"]
  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }
  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ====================================================================
# Launch Templates
# ====================================================================

locals {
  ecr_registry = "${local.ecr_acct}.dkr.ecr.${var.region}.amazonaws.com"

  userdata_common_vars = {
    region       = var.region
    app_bucket   = var.app_bucket
    ecr_registry = local.ecr_registry
  }
}

# ----- 配信 EC2 (t4g.small ARM Spot) -----

resource "aws_launch_template" "streamer" {
  name          = "bs-streamer-lt"
  image_id      = data.aws_ami.amzn2023_arm64.id
  instance_type = "t4g.small"

  iam_instance_profile {
    name = aws_iam_instance_profile.ec2.name
  }

  network_interfaces {
    subnet_id                   = aws_subnet.public.id
    security_groups             = [aws_security_group.streamer.id]
    associate_public_ip_address = false
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/streamer.sh",
    merge(local.userdata_common_vars, {
      domain     = var.domain_name
      eip_alloc  = aws_eip.streamer.id
    })
  ))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "bs-streamer" }
  }
}

# ----- デモ EC2 (c6i.xlarge Spot) -----

resource "aws_launch_template" "demo" {
  name          = "bs-demo-lt"
  image_id      = data.aws_ami.amzn2023_x86.id
  instance_type = "c6i.xlarge"

  iam_instance_profile {
    name = aws_iam_instance_profile.ec2.name
  }

  network_interfaces {
    subnet_id       = aws_subnet.private.id
    security_groups = [aws_security_group.demo.id]
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/demo.sh",
    local.userdata_common_vars
  ))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "bs-demo" }
  }
}

# ----- 学習 EC2 (c6a.4xlarge Spot, AMD EPYC CPU-only) -----
# NN は小規模で PyBullet が CPU bound なため GPU は過剰投資。
# c6a で g4dn 比 約 40% コスト削減。

resource "aws_launch_template" "learner" {
  name          = "bs-learner-lt"
  image_id      = data.aws_ami.amzn2023_x86.id
  instance_type = "c6a.4xlarge"

  iam_instance_profile {
    name = aws_iam_instance_profile.ec2.name
  }

  network_interfaces {
    subnet_id       = aws_subnet.private.id
    security_groups = [aws_security_group.learner.id]
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 100
      volume_type = "gp3"
    }
  }

  user_data = base64encode(templatefile(
    "${path.module}/userdata/learner.sh",
    local.userdata_common_vars
  ))

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "bs-learner" }
  }
}

# ====================================================================
# Auto Scaling Groups (Mixed Instances, 100% Spot, capacity-optimized)
# ====================================================================

resource "aws_autoscaling_group" "streamer" {
  name                = "bs-streamer-asg"
  vpc_zone_identifier = [aws_subnet.public.id]
  min_size            = 0
  max_size            = 1
  desired_capacity    = 0  # EventBridge が 1 に変更

  health_check_type         = "EC2"
  health_check_grace_period = 180

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "capacity-optimized"
    }
    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.streamer.id
        version            = "$Latest"
      }
    }
  }

  tag {
    key                 = "Project"
    value               = "block-stacker"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_group" "demo" {
  name                = "bs-demo-asg"
  vpc_zone_identifier = [aws_subnet.private.id]
  min_size            = 0
  max_size            = 1
  desired_capacity    = 0

  health_check_type         = "EC2"
  health_check_grace_period = 180

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "capacity-optimized"
    }
    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.demo.id
        version            = "$Latest"
      }
    }
  }

  tag {
    key                 = "Project"
    value               = "block-stacker"
    propagate_at_launch = true
  }
}

resource "aws_autoscaling_group" "learner" {
  name                = "bs-learner-asg"
  vpc_zone_identifier = [aws_subnet.private.id]
  min_size            = 0
  max_size            = 1
  desired_capacity    = 0

  health_check_type         = "EC2"
  health_check_grace_period = 180  # AL2023 はブートが速い

  mixed_instances_policy {
    instances_distribution {
      on_demand_base_capacity                  = 0
      on_demand_percentage_above_base_capacity = 0
      spot_allocation_strategy                 = "capacity-optimized"
    }
    launch_template {
      launch_template_specification {
        launch_template_id = aws_launch_template.learner.id
        version            = "$Latest"
      }
      # Spot 供給が薄い時間帯のフォールバック (同 8 物理コア x86 系)
      dynamic "override" {
        for_each = var.spot_fallback_instance_types_learner
        content {
          instance_type = override.value
        }
      }
    }
  }

  tag {
    key                 = "Project"
    value               = "block-stacker"
    propagate_at_launch = true
  }
}
