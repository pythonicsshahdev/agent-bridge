# Environment for the container (KEY=VALUE pairs as a flat JSON object): BOOMI_API_TOKEN, OAuth client
# secrets, and so on. Terraform creates the empty container; put values in with the AWS CLI so they
# never enter Terraform state:   aws secretsmanager put-secret-value --secret-id <name>/env --secret-string '{"KEY":"value"}'
resource "aws_secretsmanager_secret" "env" {
  name                    = "${var.name}/env"
  description             = "Environment variables for the a2a-bridge container"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "env_initial" {
  secret_id     = aws_secretsmanager_secret.env.id
  secret_string = "{}"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

# The config holds hashes and URLs only, never secrets, so a plain parameter is appropriate.
resource "aws_ssm_parameter" "agents_yaml" {
  name  = "/${var.name}/agents-yaml"
  type  = "String"
  tier  = "Intelligent-Tiering"
  value = file(var.agents_config_path)
}
