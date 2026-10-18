terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  required_version = ">= 1.10.0"

  # Applied locally with admin credentials, the GitHub role has no access to this key.
  # bucket and region are passed with -backend-config; the very first apply, which creates
  # the bucket, runs with a local state that is migrated afterwards (see scripts/setup.sh)
  backend "s3" {
    key          = "bootstrap/terraform.tfstate"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project = "vpn"
    }
  }
}

data "aws_caller_identity" "current" {}

locals {
  account_id      = data.aws_caller_identity.current.account_id
  bucket_arn      = "arn:aws:s3:::${var.bucket_name}"
  state_prefix    = "terraform/"
  profile_prefix  = "profiles/"
  bot_role_name   = "${var.bot_function_name}-lambda"
  bot_role_arn    = "arn:aws:iam::${local.account_id}:role/${local.bot_role_name}"
  bot_log_group   = "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/lambda/${var.bot_function_name}"
  lambda_basic    = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  boundary_arn    = "arn:aws:iam::${local.account_id}:policy/${local.bot_role_name}-boundary"
  github_oidc     = "token.actions.githubusercontent.com"
  github_oidc_arn = coalesce(var.github_oidc_provider_arn, one(aws_iam_openid_connect_provider.github[*].arn))
  github_subject = (var.github_immutable_subject
    ? "repo:${var.github_owner}@${var.github_owner_id}/${var.github_repository}@${var.github_repository_id}"
    : "repo:${var.github_owner}/${var.github_repository}"
  )
}

# S3 bucket for Terraform states and VPN client profiles

resource "aws_s3_bucket" "state" {
  bucket = var.bucket_name

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled       = true
    blocked_encryption_types = ["SSE-C"]
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "noncurrent-versions"
    status = "Enabled"

    filter {}

    # The last 5 old versions of every file are kept for a rollback however old they are,
    # older ones go after 30 days (mostly the history of the .tflock lock files).
    noncurrent_version_expiration {
      noncurrent_days           = 30
      newer_noncurrent_versions = 5
    }

    expiration {
      expired_object_delete_marker = true
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  # Profiles hold VPN private keys: deleted ones must not linger as old versions.
  # Current profiles live as long as the server, the deploy workflow removes them.
  rule {
    id     = "profiles-noncurrent-versions"
    status = "Enabled"

    filter {
      prefix = local.profile_prefix
    }

    noncurrent_version_expiration {
      noncurrent_days = 1
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

data "aws_iam_policy_document" "bucket" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [local.bucket_arn, "${local.bucket_arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.bucket.json

  depends_on = [aws_s3_bucket_public_access_block.state]
}

# GitHub Actions OIDC, an account can have only one provider per URL

resource "aws_iam_openid_connect_provider" "github" {
  count = var.github_oidc_provider_arn == null ? 1 : 0

  url            = "https://${local.github_oidc}"
  client_id_list = ["sts.amazonaws.com"]

  # Other repositories of the account may trust it as well
  lifecycle {
    prevent_destroy = true
  }
}

# Immutable OIDC subjects (repo:<owner>@<owner_id>/<repo>@<repo_id>:...) keep a recreated
# repository with the same name out.
data "aws_iam_policy_document" "github_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["${local.github_subject}:ref:refs/heads/${var.github_branch}"]
    }
  }
}

resource "aws_iam_role" "github" {
  name               = var.github_role_name
  assume_role_policy = data.aws_iam_policy_document.github_trust.json
}

data "aws_iam_policy_document" "github" {
  statement {
    sid       = "Bucket"
    actions   = ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetBucketNotification", "s3:PutBucketNotification"]
    resources = [local.bucket_arn]
  }

  statement {
    sid       = "StatesAndLocks"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${local.bucket_arn}/${local.state_prefix}*"]
  }

  statement {
    sid       = "ReleaseLocks"
    actions   = ["s3:DeleteObject"]
    resources = ["${local.bucket_arn}/${local.state_prefix}*.tflock"]
  }

  # PutObject is what the presigned POST for the server is signed with
  statement {
    sid       = "Profiles"
    actions   = ["s3:PutObject", "s3:DeleteObject"]
    resources = ["${local.bucket_arn}/${local.profile_prefix}*"]
  }

  statement {
    sid       = "Ec2Read"
    actions   = ["ec2:Describe*", "ec2:GetInstanceUefiData"]
    resources = ["*"]
  }

  statement {
    sid = "Ec2VpnServer"
    actions = [
      "ec2:RunInstances",
      "ec2:TerminateInstances",
      "ec2:StartInstances",
      "ec2:StopInstances",
      "ec2:ModifyInstanceAttribute",
      "ec2:ModifyInstanceMetadataOptions",
      "ec2:CreateTags",
      "ec2:DeleteTags",
      "ec2:AllocateAddress",
      "ec2:ReleaseAddress",
      "ec2:AssociateAddress",
      "ec2:DisassociateAddress",
      "ec2:CreateSecurityGroup",
      "ec2:DeleteSecurityGroup",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:AuthorizeSecurityGroupEgress",
      "ec2:RevokeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupEgress",
      "ec2:ModifySecurityGroupRules",
      "ec2:UpdateSecurityGroupRuleDescriptionsIngress",
      "ec2:UpdateSecurityGroupRuleDescriptionsEgress",
    ]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "aws:RequestedRegion"
      values   = [var.region]
    }
  }

  statement {
    sid = "BotLambda"
    actions = [
      "lambda:CreateFunction",
      "lambda:DeleteFunction",
      "lambda:UpdateFunctionCode",
      "lambda:UpdateFunctionConfiguration",
      "lambda:AddPermission",
      "lambda:RemovePermission",
      "lambda:TagResource",
      "lambda:UntagResource",
      "lambda:Get*",
      "lambda:List*",
    ]
    resources = [
      "arn:aws:lambda:${var.region}:${local.account_id}:function:${var.bot_function_name}",
      "arn:aws:lambda:${var.region}:${local.account_id}:function:${var.bot_function_name}:*",
    ]
  }

  statement {
    sid     = "BotHttpApi"
    actions = ["apigateway:GET", "apigateway:POST", "apigateway:PUT", "apigateway:PATCH", "apigateway:DELETE"]
    resources = [
      "arn:aws:apigateway:${var.region}::/apis",
      "arn:aws:apigateway:${var.region}::/apis/*",
      "arn:aws:apigateway:${var.region}::/tags/*",
    ]
  }

  statement {
    sid       = "ApiGatewayServiceLinkedRole"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["arn:aws:iam::${local.account_id}:role/aws-service-role/ops.apigateway.amazonaws.com/*"]

    condition {
      test     = "StringEquals"
      variable = "iam:AWSServiceName"
      values   = ["ops.apigateway.amazonaws.com"]
    }
  }

  statement {
    sid = "BotLogGroup"
    actions = [
      "logs:CreateLogGroup",
      "logs:DeleteLogGroup",
      "logs:PutRetentionPolicy",
      "logs:DeleteRetentionPolicy",
      "logs:TagResource",
      "logs:UntagResource",
      "logs:ListTagsForResource",
    ]
    resources = [local.bot_log_group, "${local.bot_log_group}:*"]
  }

  statement {
    sid       = "DescribeLogGroups"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["*"]
  }

  statement {
    sid = "BotRoleRead"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:ListInstanceProfilesForRole",
      "iam:ListRoleTags",
      "iam:TagRole",
      "iam:UntagRole",
    ]
    resources = [local.bot_role_arn]
  }

  # Without the boundary any inline policy written here would turn the bot role into an escalation path
  statement {
    sid = "BotRoleWriteWithinBoundary"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:PutRolePermissionsBoundary",
    ]
    resources = [local.bot_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.boundary_arn]
    }
  }

  statement {
    sid       = "BotRoleManagedPolicy"
    actions   = ["iam:AttachRolePolicy", "iam:DetachRolePolicy"]
    resources = [local.bot_role_arn]

    condition {
      test     = "ArnEquals"
      variable = "iam:PolicyARN"
      values   = [local.lambda_basic]
    }

    condition {
      test     = "StringEquals"
      variable = "iam:PermissionsBoundary"
      values   = [local.boundary_arn]
    }
  }

  statement {
    sid       = "PassBotRole"
    actions   = ["iam:PassRole"]
    resources = [local.bot_role_arn]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "github" {
  name   = "deploy-vpn-and-bot"
  role   = aws_iam_role.github.id
  policy = data.aws_iam_policy_document.github.json

  depends_on = [aws_iam_policy.bot_boundary]
}

resource "aws_iam_role_policies_exclusive" "github" {
  role_name    = aws_iam_role.github.name
  policy_names = [aws_iam_role_policy.github.name]
}

# Detaches the AWS managed *FullAccess policies the role was created with
resource "aws_iam_role_policy_attachments_exclusive" "github" {
  role_name   = aws_iam_role.github.name
  policy_arns = []

  depends_on = [aws_iam_role_policy.github]
}

# Permissions boundary of the bot Lambda role, see BotRoleWriteWithinBoundary

data "aws_iam_policy_document" "bot_boundary" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [local.bot_log_group, "${local.bot_log_group}:*"]
  }

  statement {
    sid       = "ListProfiles"
    actions   = ["s3:ListBucket"]
    resources = [local.bucket_arn]
  }

  statement {
    sid       = "ReadProfiles"
    actions   = ["s3:GetObject"]
    resources = ["${local.bucket_arn}/${local.profile_prefix}*"]
  }

  # Only the cheap Amazon Nova models, directly or through cross-region inference profiles
  statement {
    sid     = "InvokeNova"
    actions = ["bedrock:InvokeModel"]
    resources = [
      "arn:aws:bedrock:${var.region}:${local.account_id}:inference-profile/*.amazon.nova-*",
      "arn:aws:bedrock:*::foundation-model/amazon.nova-*",
    ]
  }
}

resource "aws_iam_policy" "bot_boundary" {
  name        = "${local.bot_role_name}-boundary"
  description = "Permissions boundary of the Telegram bot Lambda role"
  policy      = data.aws_iam_policy_document.bot_boundary.json
}
