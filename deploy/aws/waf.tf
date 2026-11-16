# Rate limiting and the AWS managed rule groups in front of the ALB. Start with waf_mode = "count" to see what
# would be blocked, then switch to "block". Request bodies larger than 8 KB are deliberately allowed, because
# A2A messages and file parts can be larger; the proxy enforces its own max_request_bytes.
resource "aws_wafv2_web_acl" "this" {
  count = var.enable_waf ? 1 : 0
  name  = var.name
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "per-ip-rate-limit"
    priority = 1

    action {
      dynamic "block" {
        for_each = var.waf_mode == "block" ? [1] : []
        content {}
      }
      dynamic "count" {
        for_each = var.waf_mode == "count" ? [1] : []
        content {}
      }
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  dynamic "rule" {
    for_each = {
      common    = { priority = 2, group = "AWSManagedRulesCommonRuleSet" }
      bad-input = { priority = 3, group = "AWSManagedRulesKnownBadInputsRuleSet" }
    }

    content {
      name     = rule.key
      priority = rule.value.priority

      override_action {
        dynamic "none" {
          for_each = var.waf_mode == "block" ? [1] : []
          content {}
        }
        dynamic "count" {
          for_each = var.waf_mode == "count" ? [1] : []
          content {}
        }
      }

      statement {
        managed_rule_group_statement {
          name        = rule.value.group
          vendor_name = "AWS"

          dynamic "rule_action_override" {
            for_each = rule.key == "common" ? ["SizeRestrictions_BODY"] : []
            content {
              name = rule_action_override.value
              action_to_use {
                count {}
              }
            }
          }
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = "${var.name}-${rule.key}"
        sampled_requests_enabled   = true
      }
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = var.name
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "this" {
  count        = var.enable_waf ? 1 : 0
  resource_arn = aws_lb.this.arn
  web_acl_arn  = aws_wafv2_web_acl.this[0].arn
}
