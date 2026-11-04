"""Offline S3 invocation validation, claims, safe results and failure boundaries."""
from copy import deepcopy
from decimal import Decimal
import json
import traceback

import boto3
from botocore.exceptions import ClientError
import pytest

from pipeline_s3_fakes import FakeS3, event
from ledgerguard import pipeline_lambda as module
from ledgerguard.pipeline_batch import MAX_PDF_BYTES, parse_manifest
from ledgerguard.pipeline_upload import upload

BUCKET = 'synthetic-pipeline-bucket'


@pytest.fixture
def invocation(pipeline_case, monkeypatch):
    s3 = FakeS3()
    batch_id = upload(pipeline_case[0], split='dev', limit=4, bucket=BUCKET, s3=s3)
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.setenv('PIPELINE_BUCKET', BUCKET)
    monkeypatch.setenv('PIPELINE_BATCH_MAX_COST', '0.50')
    monkeypatch.setattr(module, 's3_client', lambda: s3)
    calls = []
    def execute(pdfs, hashes, reference, max_cost):
        calls.append((pdfs, hashes, reference, max_cost))
        return {'run_id': 'a' * 32, 'status': 'completed', 'document_count': len(pdfs),
                'finding_count': 2, 'referral_count': 0, 'batch_cost': '0.01', 'stopped_reason': None}
    monkeypatch.setattr(module, 'execute_batch', execute)
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('No live clients allowed'))
    s3.calls.clear()
    return s3, batch_id, event(BUCKET, batch_id), calls


def manifest(s3, batch_id):
    key = f'batches/{batch_id}/manifest.json'
    return key, json.loads(s3.objects[key])


@pytest.mark.parametrize('mutation', ['bucket', 'key', 'multiple', 'missing', 'source'])
def test_event_rejected_before_s3_or_models(invocation, monkeypatch, mutation):
    s3, batch_id, request, calls = invocation
    if mutation == 'bucket': request['Records'][0]['s3']['bucket']['name'] = 'wrong-bucket'
    elif mutation == 'key': request['Records'][0]['s3']['object']['key'] = f'batches/{batch_id}/invoices/a.pdf'
    elif mutation == 'multiple': request['Records'] *= 2
    elif mutation == 'missing': request = {}
    else: request['Records'][0]['eventSource'] = 'not-s3'
    monkeypatch.setattr(module, 's3_client', lambda: pytest.fail('Event validation must come first'))
    with pytest.raises(module.PipelineFailure): module.handler(request, None)
    assert not calls and not s3.calls


@pytest.mark.parametrize('mutation', ['extra', 'unsafe', 'newline', 'hash', 'nonpdf', 'oversized', 'too-many', 'empty', 'duplicate', 'document-extra'])
def test_bad_batch_stops_before_claim_or_models(invocation, mutation):
    s3, batch_id, request, calls = invocation
    key, data = manifest(s3, batch_id)
    document = data['documents'][0]
    pdf_key = f'batches/{batch_id}/invoices/{document["document_id"]}.pdf'
    if mutation == 'extra': data['PIPELINE_BATCH_MAX_COST'] = '100'
    elif mutation == 'unsafe': document['document_id'] = '../answer_key'
    elif mutation == 'newline': document['document_id'] += '\n'
    elif mutation == 'hash': document['sha256'] = '0' * 64
    elif mutation == 'nonpdf': s3.objects[pdf_key] = b'PRIVATE-not-a-PDF'
    elif mutation == 'oversized': s3.objects[pdf_key] = b'%PDF' + b'x' * MAX_PDF_BYTES
    elif mutation == 'too-many': data['documents'] = [deepcopy(document) for _ in range(41)]
    elif mutation == 'empty': data['documents'] = []
    elif mutation == 'duplicate': data['documents'].append(deepcopy(document))
    else: document['max_cost'] = '100'
    s3.objects[key] = json.dumps(data).encode()
    with pytest.raises(module.PipelineFailure): module.handler(request, None)
    assert not calls
    assert f'claims/{batch_id}.json' not in s3.objects
    assert json.loads(s3.objects[f'results/{batch_id}.json']) == {'status': 'failed', 'error': 'ValueError'}
    assert all(body.closed for body in s3.bodies)


def test_duplicate_event_preserves_first_run_and_claim(invocation):
    s3, batch_id, request, calls = invocation
    first = module.handler(request, None)
    original = dict(s3.objects)
    assert module.handler(request, None)['status'] == 'duplicate'
    assert len(calls) == 1 and s3.objects == original
    assert json.loads(s3.objects[f'results/{batch_id}.json']) == first
    assert calls[0][3] == Decimal('0.50')
    claim_index = s3.calls.index(('put', f'claims/{batch_id}.json'))
    assert all('/invoices/' in key or key.endswith('/manifest.json') for method, key in s3.calls[:claim_index])
    assert s3.calls[claim_index + 1] == ('get', f'batches/{batch_id}/reference/policy.md')


def test_reference_failure_keeps_claim_and_prevents_reuse(invocation):
    s3, batch_id, request, calls = invocation
    del s3.objects[f'batches/{batch_id}/reference/policy.md']
    with pytest.raises(module.PipelineFailure): module.handler(request, None)
    assert f'claims/{batch_id}.json' in s3.objects
    result = s3.objects[f'results/{batch_id}.json']
    assert module.handler(request, None)['status'] == 'duplicate'
    assert s3.objects[f'results/{batch_id}.json'] == result and not calls


def test_secret_exception_is_never_logged_or_written(invocation, monkeypatch, capsys):
    s3, batch_id, request, _ = invocation
    secret = 'PRIVATE-DOCUMENT-MODEL-CONNECTION-DATA'
    def fail(*args): raise RuntimeError(secret)
    monkeypatch.setattr(module, 'execute_batch', fail)
    with pytest.raises(module.PipelineFailure) as caught:
        module.handler(request, None)
    output = capsys.readouterr()
    assert secret not in output.out + output.err + ''.join(traceback.format_exception(caught.value))
    assert str(caught.value) == 'RuntimeError'
    assert json.loads(s3.objects[f'results/{batch_id}.json']) == {'status': 'failed', 'error': 'RuntimeError'}


@pytest.mark.parametrize('value', ['NaN', '-1', '0.51', 'bad'])
def test_invalid_server_budget_refused_after_claim(invocation, monkeypatch, value):
    s3, batch_id, request, calls = invocation
    monkeypatch.setenv('PIPELINE_BATCH_MAX_COST', value)
    with pytest.raises(module.PipelineFailure): module.handler(request, None)
    assert not calls and f'claims/{batch_id}.json' in s3.objects


def test_server_budget_can_only_be_lowered(invocation, monkeypatch):
    _, _, request, calls = invocation
    monkeypatch.setenv('PIPELINE_BATCH_MAX_COST', '0.10')
    module.handler(request, None)
    assert calls[0][3] == Decimal('0.10')


def test_duplicate_json_manifest_fields_are_refused():
    with pytest.raises(ValueError, match='Duplicate'):
        parse_manifest(b'{"documents":[],"documents":[]}')


def test_conditional_conflict_never_retries_or_runs_models(invocation, monkeypatch):
    s3, _, request, calls = invocation
    original = s3.put_object
    def conflict(**kwargs):
        if kwargs['Key'].startswith('claims/'):
            raise ClientError({'Error': {'Code': 'ConditionalRequestConflict'},
                               'ResponseMetadata': {'HTTPStatusCode': 409}}, 'PutObject')
        return original(**kwargs)
    monkeypatch.setattr(s3, 'put_object', conflict)
    assert module.handler(request, None)['status'] == 'duplicate'
    assert not calls


def test_clients_disable_sdk_retries(monkeypatch):
    calls = []
    monkeypatch.setattr(boto3, 'client', lambda service, **kw: calls.append((service, kw)))
    module.bedrock_client(); module.s3_client()
    for _, options in calls:
        assert options['region_name'] == 'us-east-1'
        assert options['config'].retries == {'total_max_attempts': 1}
    config = calls[0][1]['config']
    assert config.connect_timeout == 5 and config.read_timeout == 60
