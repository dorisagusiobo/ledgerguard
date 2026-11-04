resource "aws_s3_bucket" "pipeline" {
  bucket        = "${var.name}-${data.aws_caller_identity.current.account_id}-${var.aws_region}-pipeline"
  force_destroy = false
}
resource "aws_s3_bucket_public_access_block" "pipeline" {
  bucket                  = aws_s3_bucket.pipeline.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "pipeline_tls" {
  bucket = aws_s3_bucket.pipeline.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Sid       = "RequireTLS", Effect = "Deny", Principal = "*", Action = "s3:*",
    Resource  = [aws_s3_bucket.pipeline.arn, "${aws_s3_bucket.pipeline.arn}/*"],
    Condition = { Bool = { "aws:SecureTransport" = "false" } }
  }] })
}
resource "aws_s3_bucket_lifecycle_configuration" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  rule {
    id     = "expire-pipeline-data"
    status = "Enabled"
    filter { prefix = "" }
    expiration { days = 14 }
    abort_incomplete_multipart_upload { days_after_initiation = 1 }
  }
}
resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/aws/lambda/${var.name}-pipeline"
  retention_in_days = 14
}
resource "aws_iam_role" "pipeline" {
  name = "${var.name}-pipeline"
  assume_role_policy = jsonencode({ Version = "2012-10-17", Statement = [{
    Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole"
  }] })
}
resource "aws_iam_role_policy" "pipeline" {
  role = aws_iam_role.pipeline.id
  policy = jsonencode({ Version = "2012-10-17", Statement = [
    { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.pipeline.arn}:*" },
    { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = local.app_parameter_arn },
    { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.pipeline.arn}/batches/*" },
    { Effect = "Allow", Action = ["s3:PutObject"], Resource = ["${aws_s3_bucket.pipeline.arn}/claims/*", "${aws_s3_bucket.pipeline.arn}/results/*"] }
  ] })
}
resource "aws_iam_role_policy_attachment" "pipeline_bedrock" {
  role       = aws_iam_role.pipeline.name
  policy_arn = aws_iam_policy.pipeline_bedrock.arn
}
resource "aws_lambda_function" "pipeline" {
  function_name                  = "${var.name}-pipeline"
  role                           = aws_iam_role.pipeline.arn
  runtime                        = "python3.12"
  architectures                  = ["x86_64"]
  handler                        = "ledgerguard.pipeline_lambda.handler"
  s3_bucket                      = aws_s3_object.api.bucket
  s3_key                         = aws_s3_object.api.key
  s3_object_version              = aws_s3_object.api.version_id
  source_code_hash               = filebase64sha256(local.zip_path)
  timeout                        = 900
  memory_size                    = 1024
  reserved_concurrent_executions = 1
  environment {
    variables = {
      LEDGERGUARD_ENV         = "aws"
      APP_DATABASE_PARAMETER  = local.app_parameter
      PIPELINE_BUCKET         = aws_s3_bucket.pipeline.id
      PIPELINE_BATCH_MAX_COST = var.pipeline_batch_max_cost
    }
  }
  logging_config {
    log_format = "JSON"
    log_group  = aws_cloudwatch_log_group.pipeline.name
  }
  depends_on = [aws_iam_role_policy.pipeline, aws_iam_role_policy_attachment.pipeline_bedrock]
}
resource "aws_lambda_function_event_invoke_config" "pipeline" {
  function_name                = aws_lambda_function.pipeline.function_name
  maximum_retry_attempts       = 0
  maximum_event_age_in_seconds = 3600
}
resource "aws_lambda_permission" "pipeline_s3" {
  statement_id   = "PipelineBucketOnly"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.pipeline.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.pipeline.arn
  source_account = data.aws_caller_identity.current.account_id
}
resource "aws_s3_bucket_notification" "pipeline" {
  bucket = aws_s3_bucket.pipeline.id
  lambda_function {
    lambda_function_arn = aws_lambda_function.pipeline.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "batches/"
    filter_suffix       = "/manifest.json"
  }
  depends_on = [aws_lambda_permission.pipeline_s3, aws_lambda_function_event_invoke_config.pipeline,
    aws_s3_bucket_public_access_block.pipeline, aws_s3_bucket_ownership_controls.pipeline,
  aws_s3_bucket_server_side_encryption_configuration.pipeline, aws_s3_bucket_policy.pipeline_tls]
}
