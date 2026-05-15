# Terraform Intuition Guide — Building Infrastructure From Scratch

A personal reference for building real Terraform projects without copy-pasting. Built from lessons learned on the Alex project.

---

## The Mental Model

Terraform is not code to memorize — it's a **dependency graph you declare**. Every resource you create pulls in other resources it depends on. Your job is to figure out what depends on what, then write it down.

---

## The 5 Layers

Every infrastructure project follows this top-down structure. Work through them in order.

### Layer 1: WHO am I?
Provider, region, account identity.

```hcl
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
```

This is identical in every AWS Terraform project. Write it once, never think about it again.

### Layer 2: WHERE do things live?
VPC, subnets, security groups — the network.

Ask: Does my resource need to live inside a network?
- **Databases (RDS, Aurora)** → Yes, always inside a VPC
- **Lambda** → Usually no (unless connecting to a VPC resource directly)
- **S3, DynamoDB, SQS** → No, they're global/regional services
- **App Runner, ECS** → Depends on configuration

If yes, use the default VPC (for simplicity) or create a custom one:

```hcl
# Use existing default VPC
data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}
```

### Layer 3: WHO can do WHAT?
IAM roles and policies — the permission system.

This is the most important layer to understand. See "The Three-Part Pattern" below.

### Layer 4: WHAT am I creating?
The actual resources — database, Lambda, API, queue, etc.

### Layer 5: HOW do they connect?
References between resources, environment variables, integration configs.

---

## The Three-Part Pattern

Almost every AWS service follows this pattern. Master it and you can scaffold anything.

```hcl
# Part 1: The THING
resource "aws_lambda_function" "my_function" {
  function_name = "my-function"
  role          = aws_iam_role.my_role.arn   # ← connects to Part 2
  # ... configuration ...
}

# Part 2: The BADGE (IAM Role)
resource "aws_iam_role" "my_role" {
  name = "my-function-role"

  # Trust policy: WHO can wear this badge?
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action    = "sts:AssumeRole"
      Effect    = "Allow"
      Principal = {
        Service = "lambda.amazonaws.com"  # ← Only Lambda can wear it
      }
    }]
  })
}

# Part 3: The ACCESS (IAM Policy)
resource "aws_iam_role_policy" "my_policy" {
  name = "my-function-policy"
  role = aws_iam_role.my_role.id            # ← attaches to the badge

  # Permission policy: WHAT can the badge holder do?
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["dynamodb:PutItem", "dynamodb:GetItem"]
      Resource = aws_dynamodb_table.my_table.arn  # ← on which resource
    }]
  })
}
```

### Common Trust Policy Principals

| Service | Principal |
|---------|-----------|
| Lambda | `lambda.amazonaws.com` |
| App Runner (build) | `build.apprunner.amazonaws.com` |
| App Runner (runtime) | `tasks.apprunner.amazonaws.com` |
| EventBridge | `events.amazonaws.com` |
| API Gateway | `apigateway.amazonaws.com` |
| ECS Tasks | `ecs-tasks.amazonaws.com` |
| SageMaker | `sagemaker.amazonaws.com` |

### Two Ways to Attach Policies

```hcl
# Way 1: Inline policy (custom, embedded in the role)
resource "aws_iam_role_policy" "custom" {
  role   = aws_iam_role.my_role.id
  policy = jsonencode({ ... })
}

# Way 2: Managed policy attachment (AWS pre-built or shared)
resource "aws_iam_role_policy_attachment" "managed" {
  role       = aws_iam_role.my_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}
```

Use inline for project-specific permissions. Use managed for standard AWS policies.

---

## The "Write It Wrong" Method

The fastest way to learn what a resource needs:

### Step 1: Write the minimum
```hcl
resource "aws_rds_cluster" "db" {
  cluster_identifier = "my-db"
  engine            = "aurora-postgresql"
}
```

### Step 2: Run `terraform plan`
```
Error: Missing required argument: master_username
Error: Missing required argument: engine_version
```

### Step 3: Fix one error at a time
Add the missing field, run plan again. Repeat until clean.

### Step 4: Run `terraform plan` and READ the output
The plan shows every attribute including defaults you didn't set. This teaches you what Terraform fills in automatically vs. what you must provide.

This method builds muscle memory faster than reading docs top-to-bottom.

---

## Start From What You Need, Work Backwards

Don't think "what resources do I write?" Think "what do I need, and what does it depend on?"

### Example: "I need a Lambda that reads from a database"

```
I need a Lambda function
  └── needs an IAM role
        └── needs permission to call Data API (rds-data:ExecuteStatement)
        └── needs permission to read the secret (secretsmanager:GetSecretValue)
        └── needs permission to write logs (logs:*)
  └── needs environment variables (cluster ARN, secret ARN)
  └── needs a deployment package (.zip file)

The database needs to exist
  └── needs a subnet group
        └── needs subnets (from VPC)
  └── needs a security group
        └── needs the VPC ID
  └── needs credentials
        └── needs a random password
        └── needs Secrets Manager secret + version
  └── needs Data API enabled (enable_http_endpoint = true)
```

Each indented item is a Terraform resource or data source. The arrows between them become resource references.

---

## Draw Before You Code

Sketch your architecture before touching Terraform:

```
[Lambda] --needs--> [IAM Role] --allows--> [Data API] --auth-via--> [Secret]
                                                |
                                          [Aurora Cluster]
                                                |
                                      [Subnet Group] --> [VPC/Subnets]
                                      [Security Group]
```

- Each **box** → a `resource` or `data` block
- Each **arrow** → a reference like `aws_iam_role.x.arn`

---

## Common Patterns by Architecture

### Lambda + DynamoDB
```
Resources: Lambda function, IAM role, IAM policy, DynamoDB table
Networking: None needed
Permissions: dynamodb:PutItem, GetItem, Query, etc.
```

### Lambda + Aurora Data API
```
Resources: Lambda, IAM role, IAM policy, Aurora cluster, Aurora instance,
           subnet group, security group, Secrets Manager secret, random password
Networking: VPC for Aurora (use default), none for Lambda
Permissions: rds-data:*, secretsmanager:GetSecretValue, logs:*
```

### Lambda + S3
```
Resources: Lambda, IAM role, IAM policy, S3 bucket, bucket policy
Networking: None needed
Permissions: s3:GetObject, PutObject, DeleteObject, ListBucket
```

### API Gateway + Lambda
```
Resources: REST API, resource, method, integration, deployment, stage,
           API key, usage plan, usage plan key, Lambda permission
Networking: None needed
Extra: Lambda needs a resource-based policy (aws_lambda_permission)
       to allow API Gateway to invoke it
```

### App Runner + ECR
```
Resources: ECR repo, App Runner service, access role (for ECR pull),
           instance role (for runtime AWS access)
Networking: App Runner handles it internally
Permissions: Access role needs ECR pull; instance role needs whatever
            AWS services the container calls
```

---

## Resource Naming Conventions

Pick a pattern and stay consistent:

```hcl
# Pattern: {project}-{component}-{resource-type}
"my-app-api-lambda-role"
"my-app-aurora-cluster"
"my-app-ingest-lambda"
```

Use the same project prefix in all tags:
```hcl
tags = {
  Project = "my-app"
  Environment = "dev"
}
```

---

## Variables Strategy

### variables.tf — Declare what's configurable
```hcl
variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "min_capacity" {
  description = "Minimum Aurora ACUs"
  type        = number
  default     = 0.5
}
```

### terraform.tfvars — Set values (gitignored!)
```hcl
aws_region   = "eu-west-1"
min_capacity = 0.5
```

### outputs.tf — Export values other configs need
```hcl
output "cluster_arn" {
  value = aws_rds_cluster.aurora.arn
}
```

**Rule:** Never hardcode secrets in `.tf` files. Use variables or Secrets Manager.

---

## Key Terraform Commands

```bash
terraform init          # Download providers, initialize state
terraform plan          # Preview changes (read-only, safe)
terraform apply         # Create/update resources (prompts yes/no)
terraform destroy       # Delete everything (prompts yes/no)
terraform output        # Show output values
terraform state list    # List all managed resources
terraform fmt           # Auto-format your .tf files
terraform validate      # Check syntax without talking to AWS
```

### Targeted Operations
```bash
# Deploy only specific resources (useful for incremental builds)
terraform apply -target="aws_ecr_repository.my_repo"

# Destroy only one resource
terraform destroy -target="aws_lambda_function.my_function"
```

---

## Data Sources vs. Resources

```hcl
# RESOURCE — Terraform CREATES and MANAGES this
resource "aws_s3_bucket" "my_bucket" {
  bucket = "my-bucket"
}

# DATA SOURCE — Terraform READS this (it already exists)
data "aws_vpc" "default" {
  default = true
}
```

Use data sources for things you didn't create (default VPC, AWS account ID, existing resources from other Terraform directories).

---

## State File Essentials

- `terraform.tfstate` — JSON file tracking what resources Terraform manages
- **Always gitignore it** (contains secrets in plaintext)
- **Never edit manually**
- If you lose it, Terraform thinks nothing exists and will try to recreate everything
- For team projects, use remote state (S3 backend). For learning, local is fine.

---

## Debugging Checklist

When `terraform apply` fails:

1. **Read the error message carefully** — Terraform errors are usually descriptive
2. **Check the resource docs** — Is there a required field you missed?
3. **Check IAM permissions** — Does your AWS user have permission to create this resource?
4. **Check region** — Is the service available in your region?
5. **Check quotas** — Some services have account limits (e.g., max VPCs, max Lambdas)
6. **Check state** — `terraform state list` to see what Terraform thinks exists
7. **Check the console** — Does the resource actually exist in AWS Console?

---

## Dev vs. Production Differences

| Setting | Dev (this project) | Production |
|---------|-------------------|------------|
| `skip_final_snapshot` | `true` | `false` |
| `apply_immediately` | `true` | `false` |
| `recovery_window_in_days` | `0` | `30` |
| `performance_insights_enabled` | `false` | `true` |
| `backup_retention_period` | `7` | `30+` |
| State backend | Local file | S3 + DynamoDB locking |
| `deletion_protection` | Not set | `true` |
| Security group ingress | VPC CIDR | Specific security group IDs |

---

## Practice Projects (Progressive Difficulty)

1. **S3 + Lambda** — Lambda writes a file to S3 on invocation
2. **API Gateway + Lambda** — HTTP endpoint that returns JSON
3. **Lambda + DynamoDB** — CRUD API with a NoSQL database
4. **Lambda + Aurora Data API** — SQL database with Secrets Manager
5. **App Runner + ECR** — Containerized web service
6. **Full stack** — API Gateway + Lambda + Aurora + S3 + CloudFront

For each project: draw first, write wrong, fix errors, read the plan.

---

## Quick Reference: Common IAM Actions

| Service | Common Actions |
|---------|---------------|
| S3 | `s3:GetObject`, `PutObject`, `DeleteObject`, `ListBucket` |
| DynamoDB | `dynamodb:GetItem`, `PutItem`, `Query`, `Scan`, `DeleteItem` |
| Data API | `rds-data:ExecuteStatement`, `BatchExecuteStatement`, `BeginTransaction`, `CommitTransaction`, `RollbackTransaction` |
| Secrets Manager | `secretsmanager:GetSecretValue` |
| SQS | `sqs:SendMessage`, `ReceiveMessage`, `DeleteMessage` |
| Lambda | `lambda:InvokeFunction` |
| SageMaker | `sagemaker:InvokeEndpoint` |
| Bedrock | `bedrock:InvokeModel`, `InvokeModelWithResponseStream` |
| CloudWatch | `logs:CreateLogGroup`, `CreateLogStream`, `PutLogEvents` |

---

*Last updated: May 2026. Built from Alex project learnings.*
