# Offline tests: a full apply against a mocked AWS provider, asserting on the resulting configuration.
#   cd deploy/aws && terraform init -backend=false && terraform test

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b", "us-east-1c"] }
  }
  mock_data "aws_ssm_parameter" {
    defaults = { value = "ami-0123456789abcdef0" }
  }
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}" }
  }

  # The mock invents random strings; give ARN-valued attributes a realistic shape.
  mock_resource "aws_lb" {
    defaults = {
      arn      = "arn:aws:elasticloadbalancing:us-east-1:111122223333:loadbalancer/app/a2a/50dc6c495c0c9188"
      dns_name = "a2a-123.us-east-1.elb.amazonaws.com"
      zone_id  = "Z35SXDOTRQ7X7K"
    }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/a2a/73e2d6bc24d8a067" }
  }
  mock_resource "aws_wafv2_web_acl" {
    defaults = { arn = "arn:aws:wafv2:us-east-1:111122223333:regional/webacl/a2a/a1b2c3d4-0000-0000-0000-000000000000" }
  }
  mock_resource "aws_acm_certificate" {
    defaults = {
      arn = "arn:aws:acm:us-east-1:111122223333:certificate/11111111-2222-3333-4444-555555555555"
      domain_validation_options = [{
        domain_name           = "a2a.example.com"
        resource_record_name  = "_abc.a2a.example.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_def.acm-validations.aws."
      }]
    }
  }
  mock_resource "aws_acm_certificate_validation" {
    defaults = { certificate_arn = "arn:aws:acm:us-east-1:111122223333:certificate/11111111-2222-3333-4444-555555555555" }
  }
  mock_resource "aws_secretsmanager_secret" {
    defaults = { arn = "arn:aws:secretsmanager:us-east-1:111122223333:secret:a2a-bridge/env-AbCdEf" }
  }
  mock_resource "aws_ssm_parameter" {
    defaults = { arn = "arn:aws:ssm:us-east-1:111122223333:parameter/a2a-bridge/agents-yaml" }
  }
  mock_resource "aws_cloudwatch_log_group" {
    defaults = { arn = "arn:aws:logs:us-east-1:111122223333:log-group:/a2a-bridge" }
  }
  mock_resource "aws_db_instance" {
    defaults = {
      address = "a2a-bridge.abc123.us-east-1.rds.amazonaws.com"
      master_user_secret = [{
        secret_arn    = "arn:aws:secretsmanager:us-east-1:111122223333:secret:rds!db-1234-AbCdEf"
        kms_key_id    = "alias/aws/secretsmanager"
        secret_status = "active"
      }]
    }
  }
}

variables {
  domain_name        = "a2a.example.com"
  route53_zone_id    = "Z0123456789"
  agents_config_path = "../../agents.example.yaml"
}

run "defaults_are_locked_down" {
  command = apply

  assert {
    condition     = aws_lb.this.internal == false && aws_lb.this.drop_invalid_header_fields == true
    error_message = "default is an internet-facing ALB that drops invalid headers"
  }
  assert {
    condition     = aws_lb.this.idle_timeout >= 330
    error_message = "idle timeout must outlast the facade's 300 second wait, or /invoke is cut off"
  }
  assert {
    condition     = aws_lb_listener.https.ssl_policy == "ELBSecurityPolicy-TLS13-1-2-2021-06" && aws_lb_listener.http_redirect.default_action[0].type == "redirect"
    error_message = "TLS 1.2+ on 443 and a redirect from 80"
  }
  assert {
    condition     = aws_lb_target_group.this.health_check[0].path == "/readyz"
    error_message = "health check must use readiness, not liveness"
  }
  assert {
    condition     = aws_instance.this.metadata_options[0].http_tokens == "required" && aws_instance.this.metadata_options[0].http_put_response_hop_limit == 1
    error_message = "IMDSv2 only, hop limit 1, so the container cannot reach the instance role"
  }
  assert {
    condition     = aws_instance.this.root_block_device[0].encrypted == true && aws_ebs_volume.data[0].encrypted == true
    error_message = "all volumes encrypted"
  }
  assert {
    condition     = aws_vpc_security_group_ingress_rule.instance_from_alb.from_port == 8000 && aws_vpc_security_group_ingress_rule.instance_from_alb.to_port == 8000
    error_message = "the instance admits only the app port"
  }
  assert {
    condition     = alltrue([for r in values(aws_vpc_security_group_ingress_rule.alb_https) : r.from_port == 443 && r.ip_protocol == "tcp"])
    error_message = "ALB https rules"
  }
  assert {
    condition     = length(aws_wafv2_web_acl.this) == 1 && length(aws_wafv2_web_acl_association.this) == 1
    error_message = "WAF on by default"
  }
  assert {
    condition     = length(aws_db_instance.this) == 0 && length(aws_ebs_volume.data) == 1 && length(aws_volume_attachment.data) == 1
    error_message = "SQLite mode uses an EBS volume and no database"
  }
  assert {
    condition     = !strcontains(aws_instance.this.user_data, "%%{") && !strcontains(aws_instance.this.user_data, "$${region}") && !strcontains(aws_instance.this.user_data, "$${secret_id}") && strcontains(aws_instance.this.user_data, "mkfs.xfs") && strcontains(aws_instance.this.user_data, "PUBLIC_URL=https://a2a.example.com")
    error_message = "user data renders fully and mounts the data volume"
  }
  assert {
    condition     = strcontains(aws_instance.this.user_data, "--cap-drop ALL") && strcontains(aws_instance.this.user_data, "--read-only")
    error_message = "container runs read-only with no capabilities"
  }
  assert {
    condition     = aws_secretsmanager_secret.env.name == "a2a-bridge/env" && aws_ssm_parameter.agents_yaml.type == "String"
    error_message = "secrets live in Secrets Manager, config in a plain parameter"
  }
}

run "internal_defaults_to_the_vpc_cidr" {
  command = apply
  variables {
    internal = true
  }

  assert {
    condition     = aws_lb.this.internal == true && toset(keys(aws_vpc_security_group_ingress_rule.alb_https)) == toset(["10.42.0.0/16"])
    error_message = "an internal ALB is reachable from the VPC only unless allowed_cidrs says otherwise"
  }
}

run "internet_facing_can_be_restricted" {
  command = apply
  variables {
    allowed_cidrs = ["203.0.113.0/24", "198.51.100.7/32"]
  }

  assert {
    condition     = toset(keys(aws_vpc_security_group_ingress_rule.alb_https)) == toset(["203.0.113.0/24", "198.51.100.7/32"])
    error_message = "allowed_cidrs controls who reaches the ALB"
  }
}

run "rds_mode" {
  command = apply
  variables {
    use_rds = true
  }

  assert {
    condition     = aws_db_instance.this[0].storage_encrypted == true && aws_db_instance.this[0].publicly_accessible == false && aws_db_instance.this[0].manage_master_user_password == true && aws_db_instance.this[0].deletion_protection == true
    error_message = "database is encrypted, private, protected, and its password is managed by AWS"
  }
  assert {
    condition     = length(aws_ebs_volume.data) == 0 && length(aws_subnet.private) == 2 && length(aws_vpc_security_group_egress_rule.instance_to_db) == 1
    error_message = "no data volume; isolated subnets; the instance may reach the database"
  }
  assert {
    condition     = !strcontains(aws_instance.this.user_data, "mkfs.xfs") && strcontains(aws_instance.this.user_data, "DB_HOST=a2a-bridge.abc123")
    error_message = "user data knows the database host"
  }
}

run "bring_your_own_certificate" {
  command = apply
  variables {
    route53_zone_id = ""
    certificate_arn = "arn:aws:acm:us-east-1:111122223333:certificate/abc"
  }

  assert {
    condition     = length(aws_acm_certificate.this) == 0 && aws_lb_listener.https.certificate_arn == "arn:aws:acm:us-east-1:111122223333:certificate/abc" && length(aws_route53_record.alias) == 0
    error_message = "an existing certificate is used and no Route 53 records are created"
  }
}

run "no_certificate_source_is_rejected" {
  command = plan
  variables {
    route53_zone_id = ""
    certificate_arn = ""
  }
  expect_failures = [aws_acm_certificate.this]
}

run "waf_can_be_disabled" {
  command = apply
  variables {
    enable_waf = false
  }

  assert {
    condition     = length(aws_wafv2_web_acl.this) == 0 && length(aws_wafv2_web_acl_association.this) == 0
    error_message = "WAF is optional"
  }
}

run "bad_waf_mode_is_rejected" {
  command = plan
  variables {
    waf_mode = "yolo"
  }
  expect_failures = [var.waf_mode]
}
