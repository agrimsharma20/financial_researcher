variable "aws_region" {
  description = "AWS region for deployment"
  type        = string
  default     = "us-east-1"
}

# Clerk validation happens in Lambda, not at API Gateway level
variable "clerk_jwks_url" {
  description = "Clerk JWKS URL for JWT validation in Lambda"
  type        = string
}

variable "clerk_issuer" {
  description = "Clerk issuer URL (kept for Lambda environment)"
  type        = string
  default     = ""  # Not actually used but kept for backwards compatibility
}

# ── Variables migrated from terraform_remote_state ──
# Previously these were read via data.terraform_remote_state.database and
# data.terraform_remote_state.agents. Since dir 5 stays on local backend
# (manual lifecycle), we pass these explicitly via terraform.tfvars.
# Get values with: cd terraform/5_database && terraform output
#                  cd terraform/6_agents && terraform output

variable "aurora_cluster_arn" {
  description = "Aurora cluster ARN from Part 5"
  type        = string
}

variable "aurora_secret_arn" {
  description = "Aurora secret ARN from Part 5"
  type        = string
}

variable "aurora_database_name" {
  description = "Aurora database name from Part 5"
  type        = string
  default     = "alex"
}

variable "sqs_queue_arn" {
  description = "SQS queue ARN from Part 6"
  type        = string
}

variable "sqs_queue_url" {
  description = "SQS queue URL from Part 6"
  type        = string
}