"""Deployment boundaries for the asynchronous pipeline, without cloud access."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def block(source, kind, name):
    match = re.search(r'resource "' + kind + r'" "' + name + r'" \{(.*?)(?=\nresource |\Z)', source, re.S)
    assert match is not None
    return match[1]


def test_pipeline_retry_concurrency_permissions_and_trigger():
    source = (ROOT / 'infra/terraform/pipeline.tf').read_text()
    invoke = block(source, 'aws_lambda_function_event_invoke_config', 'pipeline')
    assert re.search(r'maximum_retry_attempts\s*=\s*0\b', invoke)
    assert re.search(r'maximum_event_age_in_seconds\s*=\s*3600\b', invoke)
    function = block(source, 'aws_lambda_function', 'pipeline')
    assert re.search(r'reserved_concurrent_executions\s*=\s*1\b', function)
    assert re.search(r'timeout\s*=\s*900\b', function)
    assert re.search(r'memory_size\s*=\s*1024\b', function)
    assert 'ledgerguard.pipeline_lambda.handler' in function and 'aws_s3_object.api.version_id' in function
    assert 'var.pipeline_batch_max_cost' in function
    assert 'direct_parameter' not in source
    policy = block(source, 'aws_iam_role_policy', 'pipeline')
    assert 'local.app_parameter_arn' in policy
    assert 's3:GetObject' in policy and '/batches/*' in policy
    assert 's3:PutObject' in policy and '/claims/*' in policy and '/results/*' in policy
    assert 's3:DeleteObject' not in policy and 'ssm:*' not in policy
    attachment = block(source, 'aws_iam_role_policy_attachment', 'pipeline_bedrock')
    assert 'aws_iam_role.pipeline.name' in attachment and 'aws_iam_policy.pipeline_bedrock.arn' in attachment
    permission = block(source, 'aws_lambda_permission', 'pipeline_s3')
    assert 'source_arn' in permission and 'aws_s3_bucket.pipeline.arn' in permission
    assert 'source_account' in permission and 'data.aws_caller_identity.current.account_id' in permission
    notification = block(source, 'aws_s3_bucket_notification', 'pipeline')
    assert '"batches/"' in notification and '"/manifest.json"' in notification
    for path in (ROOT / 'infra/terraform').glob('*.tf'):
        assert not re.search(r'resource\s+"aws_lambda_function_url"', path.read_text())
    api = block((ROOT / 'infra/terraform/main.tf').read_text(), 'aws_iam_role_policy', 'api')
    assert 'bedrock:' not in api


def test_pipeline_bucket_is_private_encrypted_and_expires():
    source = (ROOT / 'infra/terraform/pipeline.tf').read_text()
    public = block(source, 'aws_s3_bucket_public_access_block', 'pipeline')
    for setting in ['block_public_acls', 'block_public_policy', 'ignore_public_acls', 'restrict_public_buckets']:
        assert re.search(setting + r'\s*=\s*true', public)
    assert 'BucketOwnerEnforced' in source and 'AES256' in source
    assert 'aws:SecureTransport' in source
    assert re.search(r'expiration\s*\{\s*days\s*=\s*14\s*\}', source)


def test_packaging_revision_records_dirty_worktrees(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('revision_builder', ROOT / 'scripts/build_api.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clean = [False]
    def output(command, **kwargs):
        if command == ['git', 'rev-parse', 'HEAD']:
            return 'a' * 40 + '\n'
        assert command == ['git', 'status', '--porcelain']
        return '' if clean[0] else ' M src/ledgerguard/pipeline_lambda.py\n'
    monkeypatch.setattr(module.subprocess, 'check_output', output)
    assert module.git_revision() == 'a' * 40 + '-dirty'
    clean[0] = True
    assert module.git_revision() == 'a' * 40
