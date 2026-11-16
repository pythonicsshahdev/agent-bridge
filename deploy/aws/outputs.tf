output "url" {
  value = "https://${var.domain_name}"
}

output "alb_dns_name" {
  description = "Point domain_name at this (CNAME or alias) if you did not set route53_zone_id."
  value       = aws_lb.this.dns_name
}

output "instance_id" {
  value = aws_instance.this.id
}

output "shell" {
  description = "Open a shell on the instance (no SSH needed)."
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.this.id}"
}

output "set_secrets" {
  description = "Set the container's secret environment variables without putting them in Terraform state."
  value       = "aws secretsmanager put-secret-value --region ${var.region} --secret-id ${aws_secretsmanager_secret.env.name} --secret-string '{\"BOOMI_API_TOKEN\":\"...\"}'"
}

output "restart" {
  description = "Pick up new config, secrets or image."
  value       = "aws ssm send-command --region ${var.region} --instance-ids ${aws_instance.this.id} --document-name AWS-RunShellScript --parameters 'commands=[\"systemctl restart a2a-bridge\"]'"
}

output "log_group" {
  value = aws_cloudwatch_log_group.this.name
}
