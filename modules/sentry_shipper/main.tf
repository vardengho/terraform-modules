data "aws_iam_policy_document" "lambda_assume_role_policy" {
    statement {
        actions = ["sts:AssumeRole"]

        principals {
            type = "Service"
            identifiers = ["lambda.amazonaws.com"]
        }
    }
}

data "aws_iam_policy_document" "cw_to_sentry" {
    statement {
        actions = [
            "logs:PutResourcePolicy",
            "logs:CreateLogGroup",
            "logs:CreateLogStream",
            "logs:PutLogEvents"
        ]

        resources = ["*"]

        effect = "Allow"
    }
}

data "aws_region" "current" {}

locals {

    safe_name = "sentry-shipper"

    env_name = lower(var.environment)

    enrich_values = merge(
        var.additional_values
    )

    additional_values = join(";", [for name, value in local.enrich_values : "${name}=${value}"])
}

resource "aws_iam_role" "this" {
    name = "${var.family}-${local.safe_name}-${local.env_name}-role"
    tags = merge(var.tags, { Name = "${var.family}-${local.safe_name}-${local.env_name}-role" })
    assume_role_policy = data.aws_iam_policy_document.lambda_assume_role_policy.json

    inline_policy {
        name = "cw_to_sentry"
        policy = data.aws_iam_policy_document.cw_to_sentry.json
    }
}

resource "aws_cloudwatch_log_group" "this" {
    name = "/aws/lambda/${aws_lambda_function.this.function_name}"
    tags = merge(var.tags, { Name = "${var.family}-${local.safe_name}-${local.env_name}-log-group" })
    retention_in_days = 14
}

data "archive_file" "lambda_zip" {
    type = "zip"
    source_dir = "${path.module}/../../src/sentry_shipper"
    output_path = "${path.module}/dist/lambda.zip"
}

resource "aws_lambda_function" "this" {
    function_name = "${var.family}-${local.safe_name}-${local.env_name}"
    role = aws_iam_role.this.arn
    filename = data.archive_file.lambda_zip.output_path
    source_code_hash = data.archive_file.lambda_zip.output_base64sha256
    runtime = "python3.9"
    handler = "lambda_function.lambda_handler"
    timeout = var.timeout
    memory_size = var.memory_size
    tags = merge(var.tags, { Name = "${var.family}-${local.safe_name}-${local.env_name}-function" })
    environment {
        variables = {
            SENTRY_DSN = var.dsn
            ENRICH = local.additional_values
        }
    }
}

resource "aws_lambda_permission" "this" {
    count = length(var.log_groups)
    statement_id = "${var.family}-${local.safe_name}-${local.env_name}-${replace(var.log_groups[count.index].name, "/", "-")}-statement"
    action = "lambda:InvokeFunction"
    function_name = aws_lambda_function.this.function_name
    principal = "logs.${data.aws_region.current.name}.amazonaws.com"
    source_arn = format("%s:*", var.log_groups[count.index].arn)
}

resource "aws_cloudwatch_log_subscription_filter" "this" {
    count = length(var.log_groups)
    name = "${var.family}-${local.safe_name}-${local.env_name}-${replace(var.log_groups[count.index].name, "/", "-")}-subscription"
    filter_pattern = (var.filter_pattern == null) ? "" : var.filter_pattern
    destination_arn = aws_lambda_function.this.arn
    log_group_name = var.log_groups[count.index].name
    depends_on = [aws_lambda_permission.this]
}
