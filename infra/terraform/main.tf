data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
locals {
  app_parameter     = "/${var.name}/app-database-url"
  direct_parameter  = "/${var.name}/direct-database-url"
  app_parameter_arn = "arn:${data.aws_partition.current.partition}:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.app_parameter}"
  zip_path          = abspath("${path.module}/${var.api_zip}")
}
# SSM values cannot be empty. Names/ARNs are outputs only: the owner creates
# SecureStrings privately. Terraform never creates, reads or imports their values.
resource "aws_cloudwatch_log_group" "api" {
  name              = "/aws/lambda/${var.name}-review-api"
  retention_in_days = 14
}
resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/aws/apigateway/${var.name}-review-api"
  retention_in_days = 14
}
resource "aws_iam_role" "api" {
  name = "${var.name}-review-api"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy" "api" {
  role = aws_iam_role.api.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.api.arn}:*" },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.app_parameter_arn }
  ] })
}
resource "aws_lambda_function" "api" {
  function_name                  = "${var.name}-review-api"
  role                           = aws_iam_role.api.arn
  runtime                        = "python3.12"
  architectures                  = ["x86_64"]
  handler                        = "ledgerguard.api.handler"
  s3_bucket                      = aws_s3_object.api.bucket
  s3_key                         = aws_s3_object.api.key
  s3_object_version              = aws_s3_object.api.version_id
  source_code_hash               = filebase64sha256(local.zip_path)
  timeout                        = 30
  memory_size                    = 512
  reserved_concurrent_executions = 2
  environment {
    variables = {
      LEDGERGUARD_ENV        = "aws"
      APP_DATABASE_PARAMETER = local.app_parameter
    }
  }
  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.api.name
  }
  depends_on = [aws_iam_role_policy.api]
}
# Deliberately no aws_lambda_function_url and no Bedrock grant on the review API.
# Scoped Bedrock policy is prepared for the separate pipeline role in 8c.
resource "aws_iam_policy" "pipeline_bedrock" {
  name = "${var.name}-pipeline-bedrock"
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = [
      for model in ["global.amazon.nova-2-lite-v1:0", "global.anthropic.claude-sonnet-4-6"] :
      "arn:${data.aws_partition.current.partition}:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${model}"
    ] },
    { Effect = "Allow", Action = ["bedrock:InvokeModel"], Resource = [
      "arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/amazon.nova-2-lite-v1:0",
      "arn:${data.aws_partition.current.partition}:bedrock:*::foundation-model/anthropic.claude-sonnet-4-6"
      ], Condition = { StringEquals = { "bedrock:InferenceProfileArn" = [
        for model in ["global.amazon.nova-2-lite-v1:0", "global.anthropic.claude-sonnet-4-6"] :
        "arn:${data.aws_partition.current.partition}:bedrock:${var.aws_region}:${data.aws_caller_identity.current.account_id}:inference-profile/${model}"
    ] } } }
  ] })
}
resource "aws_cognito_user_pool" "reviewers" {
  name                     = "${var.name}-reviewers"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  deletion_protection      = var.protect_user_pool ? "ACTIVE" : "INACTIVE"
  admin_create_user_config { allow_admin_create_user_only = true }
  user_attribute_update_settings { attributes_require_verification_before_update = ["email"] }
  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 1
  }
}
resource "aws_cognito_user_pool_client" "review" {
  name                                 = "${var.name}-review"
  user_pool_id                         = aws_cognito_user_pool.reviewers.id
  generate_secret                      = false
  prevent_user_existence_errors        = "ENABLED"
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${aws_apigatewayv2_api.web.api_endpoint}/auth/callback"]
  logout_urls                          = ["${aws_apigatewayv2_api.web.api_endpoint}/signed-out"]
  enable_token_revocation              = true
  access_token_validity                = 1
  id_token_validity                    = 1
  refresh_token_validity               = 1
}
resource "aws_cognito_user_pool_domain" "review" {
  domain       = "${var.name}-${data.aws_caller_identity.current.account_id}"
  user_pool_id = aws_cognito_user_pool.reviewers.id
}
resource "aws_apigatewayv2_api" "review" {
  name          = "${var.name}-review"
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_authorizer" "review" {
  api_id           = aws_apigatewayv2_api.review.id
  name             = "cognito-reviewers"
  authorizer_type  = "JWT"
  identity_sources = ["$request.header.Authorization"]
  jwt_configuration {
    audience = [aws_cognito_user_pool_client.review.id]
    issuer   = "https://${aws_cognito_user_pool.reviewers.endpoint}"
  }
}
resource "aws_apigatewayv2_integration" "api" {
  api_id                 = aws_apigatewayv2_api.review.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.api.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 30000
}
resource "aws_apigatewayv2_route" "review" {
  for_each = toset(["/health", "/runs", "/runs/{run_id}/findings", "/findings/{finding_id}", "/findings/{finding_id}/decisions"])
  api_id   = aws_apigatewayv2_api.review.id
  # Preserve FastAPI's 405 for unsupported methods without exposing extra APIs.
  route_key          = "ANY ${each.value}"
  target             = "integrations/${aws_apigatewayv2_integration.api.id}"
  authorization_type = each.value == "/health" ? "NONE" : "JWT"
  authorizer_id      = each.value == "/health" ? null : aws_apigatewayv2_authorizer.review.id
}
resource "aws_apigatewayv2_stage" "review" {
  api_id      = aws_apigatewayv2_api.review.id
  name        = "$default"
  auto_deploy = true
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.gateway.arn
    format          = jsonencode({ request_id = "$context.requestId", route = "$context.routeKey", status = "$context.status" })
  }
  default_route_settings {
    throttling_burst_limit = 10
    throttling_rate_limit  = 5
  }
}
resource "aws_lambda_permission" "gateway" {
  statement_id  = "ApiGatewayOnly"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.api.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.review.execution_arn}/*/*"
}
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name}-monthly"
  budget_type  = "COST"
  limit_amount = "10.00"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"
  dynamic "notification" {
    for_each = [80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }
}
