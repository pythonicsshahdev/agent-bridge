data "aws_ssm_parameter" "ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

resource "aws_ssm_parameter" "image" {
  name  = "/${var.name}/image"
  type  = "String"
  value = var.container_image
}

resource "aws_ebs_volume" "data" {
  count             = var.use_rds ? 0 : 1
  availability_zone = aws_subnet.public[0].availability_zone
  size              = var.data_volume_gb
  type              = "gp3"
  encrypted         = true
  tags              = { Name = "${var.name}-data" }
}

resource "aws_instance" "this" {
  ami                    = data.aws_ssm_parameter.ami.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.instance.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name
  monitoring             = true

  # IMDSv2 only, with a hop limit of 1 so a process in the container cannot reach the instance role's credentials.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 20
    encrypted   = true
  }

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    use_rds            = var.use_rds
    region             = var.region
    log_group          = aws_cloudwatch_log_group.this.name
    secret_id          = aws_secretsmanager_secret.env.name
    config_param       = aws_ssm_parameter.agents_yaml.name
    image_param        = aws_ssm_parameter.image.name
    public_url         = "https://${var.domain_name}"
    data_volume_serial = var.use_rds ? "" : replace(aws_ebs_volume.data[0].id, "-", "")
    db_secret_arn      = var.use_rds ? aws_db_instance.this[0].master_user_secret[0].secret_arn : ""
    db_host            = var.use_rds ? aws_db_instance.this[0].address : ""
    prestart_script    = file("${path.module}/files/prestart.sh")
    run_script         = file("${path.module}/files/run.sh")
    unit_file          = file("${path.module}/files/a2a-bridge.service")
  })
  user_data_replace_on_change = true

  tags = { Name = var.name }
}

resource "aws_volume_attachment" "data" {
  count       = var.use_rds ? 0 : 1
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data[0].id
  instance_id = aws_instance.this.id
}
