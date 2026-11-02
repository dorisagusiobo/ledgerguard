output "api_url" { value = aws_apigatewayv2_stage.review.invoke_url }
output "web_url" { value = aws_apigatewayv2_api.web.api_endpoint }
output "user_pool_id" { value = aws_cognito_user_pool.reviewers.id }
output "user_pool_client_id" { value = aws_cognito_user_pool_client.review.id }
output "cognito_domain" { value = "https://${aws_cognito_user_pool_domain.review.domain}.auth.${var.aws_region}.amazoncognito.com" }
output "app_database_parameter_name" { value = local.app_parameter }
output "direct_database_parameter_name" { value = local.direct_parameter }
output "pipeline_bedrock_policy_arn" { value = aws_iam_policy.pipeline_bedrock.arn }
