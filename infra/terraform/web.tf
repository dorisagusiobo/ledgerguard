locals {
  web_zip_path = abspath("${path.module}/${var.web_zip}")
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/aws/lambda/${var.name}-review-web"
  retention_in_days = 14
}
resource "aws_cloudwatch_log_group" "web_gateway" {
  name              = "/aws/apigateway/${var.name}-review-web"
  retention_in_days = 14
}
resource "aws_iam_role" "web" {
  name = "${var.name}-review-web"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy" "web" {
  role = aws_iam_role.web.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.web.arn}:*"
  }] })
}
resource "aws_s3_object" "web" {
  bucket                 = aws_s3_bucket.artifacts.id
  key                    = "web/web.zip"
  source                 = local.web_zip_path
  source_hash            = filemd5(local.web_zip_path)
  server_side_encryption = "AES256"
  depends_on = [aws_s3_bucket_versioning.artifacts,
    aws_s3_bucket_public_access_block.artifacts,
    aws_s3_bucket_policy.artifacts_tls,
  aws_s3_bucket_server_side_encryption_configuration.artifacts]
}
resource "aws_lambda_function" "web" {
  function_name     = "${var.name}-review-web"
  role              = aws_iam_role.web.arn
  runtime           = "nodejs24.x"
  architectures     = ["x86_64"]
  handler           = "run.sh"
  s3_bucket         = aws_s3_object.web.bucket
  s3_key            = aws_s3_object.web.key
  s3_object_version = aws_s3_object.web.version_id
  source_code_hash  = filebase64sha256(local.web_zip_path)
  memory_size       = 1024
  timeout           = 15
  layers            = ["arn:aws:lambda:us-east-1:753240598075:layer:LambdaAdapterLayerX86:30"]
  environment {
    variables = {
      AWS_LAMBDA_EXEC_WRAPPER      = "/opt/bootstrap"
      PORT                         = "8080"
      HOSTNAME                     = "127.0.0.1"
      AWS_LWA_READINESS_CHECK_PATH = "/auth/health"
      NODE_ENV                     = "production"
      NEXT_TELEMETRY_DISABLED      = "1"
      LEDGERGUARD_ENV              = "aws"
      APP_ORIGIN                   = aws_apigatewayv2_api.web.api_endpoint
      REVIEW_API_URL               = aws_apigatewayv2_api.review.api_endpoint
      COGNITO_DOMAIN               = "https://${aws_cognito_user_pool_domain.review.domain}.auth.${var.aws_region}.amazoncognito.com"
      COGNITO_USER_POOL_ID         = aws_cognito_user_pool.reviewers.id
      COGNITO_CLIENT_ID            = aws_cognito_user_pool_client.review.id
    }
  }
  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.web.name
  }
  depends_on = [aws_iam_role_policy.web]
}
resource "aws_apigatewayv2_api" "web" {
  name          = "${var.name}-review-web"
  protocol_type = "HTTP"
}
resource "aws_apigatewayv2_integration" "web" {
  api_id                 = aws_apigatewayv2_api.web.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.web.invoke_arn
  payload_format_version = "2.0"
  timeout_milliseconds   = 15000
}
resource "aws_apigatewayv2_route" "web" {
  for_each           = toset(["ANY /", "ANY /{proxy+}"])
  api_id             = aws_apigatewayv2_api.web.id
  route_key          = each.value
  target             = "integrations/${aws_apigatewayv2_integration.web.id}"
  authorization_type = "NONE"
}
resource "aws_apigatewayv2_stage" "web" {
  api_id      = aws_apigatewayv2_api.web.id
  name        = "$default"
  auto_deploy = true
  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.web_gateway.arn
    format          = jsonencode({ request_id = "$context.requestId", route = "$context.routeKey", status = "$context.status" })
  }
  default_route_settings {
    throttling_burst_limit = 20
    throttling_rate_limit  = 10
  }
}
resource "aws_lambda_permission" "web_gateway" {
  statement_id  = "WebApiGatewayOnly"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.web.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.web.execution_arn}/*/*"
}
