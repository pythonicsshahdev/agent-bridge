resource "aws_db_subnet_group" "this" {
  count      = var.use_rds ? 1 : 0
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

# RDS creates the master password and stores the master password in Secrets Manager; it never appears in Terraform state or config.
resource "aws_db_instance" "this" {
  count                        = var.use_rds ? 1 : 0
  identifier                   = var.name
  engine                       = "postgres"
  engine_version               = "16"
  instance_class               = var.db_instance_class
  allocated_storage            = 20
  max_allocated_storage        = 100
  storage_encrypted            = true
  db_name                      = "a2a"
  username                     = "a2a"
  manage_master_user_password  = true
  db_subnet_group_name         = aws_db_subnet_group.this[0].name
  vpc_security_group_ids       = [aws_security_group.db[0].id]
  publicly_accessible          = false
  backup_retention_period      = 7
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = !var.deletion_protection
  final_snapshot_identifier    = var.deletion_protection ? "${var.name}-final" : null
  auto_minor_version_upgrade   = true
  performance_insights_enabled = true
}
