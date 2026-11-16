variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "a2a-bridge"
}

variable "domain_name" {
  description = "Public (or private) DNS name clients use, e.g. a2a.example.com. Must match the certificate."
  type        = string
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone for domain_name. Enables automatic certificate validation and the DNS record. Leave empty to bring your own certificate and DNS."
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "Existing ACM certificate in this region. If empty, one is requested and validated through route53_zone_id."
  type        = string
  default     = ""
}

variable "internal" {
  description = "true: internal load balancer, reachable only from inside the VPC (or peered networks). false: internet-facing."
  type        = bool
  default     = false
}

variable "allowed_cidrs" {
  description = "Who may reach the load balancer. Restrict this for an internet-facing deployment. Internal deployments default to the VPC CIDR."
  type        = list(string)
  default     = ["0.0.0.0/0"]
}

variable "vpc_cidr" {
  type    = string
  default = "10.42.0.0/16"
}

variable "instance_type" {
  type    = string
  default = "t3.small"
}

variable "container_image" {
  description = "Image to run. Pin a version or digest in production, not :latest."
  type        = string
  default     = "ghcr.io/pythonicsshahdev/agent-bridge:latest"
}

variable "agents_config_path" {
  description = "Path to your agents.yaml. It must not contain secrets (use $${ENV} references)."
  type        = string
}

variable "use_rds" {
  description = "Use RDS Postgres instead of SQLite on the instance's EBS volume. Needed for more than one instance or point-in-time recovery."
  type        = bool
  default     = false
}

variable "db_instance_class" {
  type    = string
  default = "db.t4g.micro"
}

variable "data_volume_gb" {
  description = "Size of the encrypted EBS volume holding SQLite (when use_rds is false)."
  type        = number
  default     = 10
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "enable_waf" {
  type    = bool
  default = true
}

variable "waf_mode" {
  description = "block, or count to observe what the managed rules would block before enforcing."
  type        = string
  default     = "block"

  validation {
    condition     = contains(["block", "count"], var.waf_mode)
    error_message = "waf_mode must be block or count."
  }
}

variable "waf_rate_limit" {
  description = "Requests per 5 minutes per source IP before WAF blocks it."
  type        = number
  default     = 2000
}

variable "deletion_protection" {
  description = "Protect the load balancer and database from accidental deletion. Set false only to tear a test stack down."
  type        = bool
  default     = true
}
