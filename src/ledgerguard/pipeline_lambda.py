"""One-shot S3 pipeline runner. No import-time clients, setup or model calls."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg.rows import dict_row

from ledgerguard.audit import PostgresSink, app_connection
from ledgerguard.db import runtime_database_url
from ledgerguard.gateway import Budget, decimal_value
from ledgerguard.monthly import MonthlyLedger
from ledgerguard.pipeline import PipelineModels, run_pipeline
from ledgerguard.pipeline_batch import (MANIFEST_KEY, MAX_MANIFEST_BYTES, MAX_PDF_BYTES,
                                       MAX_REFERENCE_BYTES, REFERENCE_FILES, parse_manifest, validate_pdf)
from ledgerguard.rules import load_reference
from ledgerguard.store import RunMetadata, save_run


class PipelineFailure(RuntimeError):
    """Lambda-visible failure retaining only the underlying exception class."""


def s3_client():
    return boto3.client('s3', region_name='us-east-1', config=Config(retries={'total_max_attempts': 1}))


def bedrock_client():
    return boto3.client('bedrock-runtime', region_name='us-east-1',
                        config=Config(retries={'total_max_attempts': 1}, connect_timeout=5, read_timeout=60))


def event_batch(event, bucket: str) -> str:
    if not bucket or not isinstance(event, dict) or not isinstance(event.get('Records'), list) or len(event['Records']) != 1:
        raise ValueError('Expected one S3 record')
    record = event['Records'][0]
    if not isinstance(record, dict) or record.get('eventSource') != 'aws:s3':
        raise ValueError('Expected S3 event')
    if record['s3']['bucket']['name'] != bucket:
        raise ValueError('Unexpected bucket')
    match = MANIFEST_KEY.fullmatch(unquote_plus(record['s3']['object']['key']))
    if not match:
        raise ValueError('Unexpected key')
    return match[1]


def read_object(s3, bucket, key, limit):
    response = s3.get_object(Bucket=bucket, Key=key)
    body = response['Body']
    try:
        if response.get('ContentLength', 0) > limit:
            raise ValueError('Object too large')
        data = body.read(limit + 1)
        if not isinstance(data, bytes) or len(data) > limit:
            raise ValueError('Object too large')
        return data
    finally:
        body.close()


def conditional_json(s3, bucket, key, value):
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=json.dumps(value, sort_keys=True).encode(),
                      ContentType='application/json', ServerSideEncryption='AES256', IfNoneMatch='*')
        return True
    except ClientError as error:
        if error.response.get('ResponseMetadata', {}).get('HTTPStatusCode') in {409, 412} or error.response.get('Error', {}).get('Code') in {'PreconditionFailed', 'ConditionalRequestConflict'}:
            return False
        raise


def build_revision():
    return Path(__file__).with_name('build_revision.txt').read_text().strip()


def execute_batch(pdfs, hashes, reference, max_cost):
    """Only this boundary connects the governed pipeline to durable app storage."""
    url = runtime_database_url()
    started_at = datetime.now(timezone.utc)
    revision = build_revision()
    models = PipelineModels()
    batch = Budget(label='batch', max_calls=len(pdfs) * 20, max_tool_calls=len(pdfs) * 10, max_cost=max_cost)
    with app_connection(url) as checkpoint_conn:
        # app_connection checks the role and uses prepare_threshold=None.
        checkpoint_conn.row_factory = dict_row
        saver = PostgresSaver(checkpoint_conn, serde=JsonPlusSerializer(allowed_msgpack_modules=[]))
        result = run_pipeline(pdfs, reference, client=bedrock_client(), models=models, batch=batch,
                              audit=PostgresSink(url), monthly=MonthlyLedger(url), checkpointer=saver)
    metadata = RunMetadata(run_id=result.pipeline_run_id, started_at=started_at,
                           finished_at=datetime.now(timezone.utc),
                           model_ids={'extract': models.extract, 'policy': models.policy, 'investigate': models.investigate},
                           document_hashes=hashes, git_revision=revision, mode='live')
    with app_connection(url) as conn:
        save_run(conn, result, metadata)
    return {'run_id': result.pipeline_run_id,
            'status': 'stopped' if result.stopped else 'needs_review' if result.referrals else 'completed',
            'document_count': len(pdfs), 'finding_count': len(result.findings),
            'referral_count': len(result.referrals), 'batch_cost': str(batch.cost), 'stopped_reason': result.stopped}


def handler(event, context):
    s3, batch_id = None, None
    bucket = os.environ.get('PIPELINE_BUCKET', '')
    try:
        batch_id = event_batch(event, bucket)
        s3 = s3_client()
        prefix = f'batches/{batch_id}'
        documents = parse_manifest(read_object(s3, bucket, f'{prefix}/manifest.json', MAX_MANIFEST_BYTES))
        pdfs, hashes = {}, {}
        for doc in documents:
            name, digest = doc['document_id'], doc['sha256']
            pdf = read_object(s3, bucket, f'{prefix}/invoices/{name}.pdf', MAX_PDF_BYTES)
            validate_pdf(pdf, digest)
            pdfs[name], hashes[name] = pdf, digest
        if not conditional_json(s3, bucket, f'claims/{batch_id}.json', {'batch_id': batch_id}):
            print(json.dumps({'batch_id': batch_id, 'status': 'duplicate'}))
            return {'status': 'duplicate', 'batch_id': batch_id}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in REFERENCE_FILES:
                (root / name).write_bytes(read_object(s3, bucket, f'{prefix}/reference/{name}', MAX_REFERENCE_BYTES))
            reference = load_reference(root)
            if os.environ.get('LEDGERGUARD_ENV') != 'aws':
                raise ValueError('AWS mode required')
            max_cost = decimal_value(os.environ['PIPELINE_BATCH_MAX_COST'])
            if not Decimal('0') <= max_cost <= Decimal('0.50'):
                raise ValueError('Invalid batch budget')
            result = execute_batch(pdfs, hashes, reference, max_cost)
        if not conditional_json(s3, bucket, f'results/{batch_id}.json', result):
            raise RuntimeError('Result already exists')
        print(json.dumps({'batch_id': batch_id, **{key: value for key, value in result.items() if key != 'stopped_reason'}}))
        return result
    except Exception as error:
        error_class = type(error).__name__
        print(json.dumps({'batch_id': batch_id, 'error': error_class}))
        if batch_id is not None and s3 is not None:
            try:
                conditional_json(s3, bucket, f'results/{batch_id}.json', {'status': 'failed', 'error': error_class})
            except Exception as write_error:
                print(json.dumps({'batch_id': batch_id, 'error': type(write_error).__name__}))
        # A bare re-raise would let Lambda log secret-bearing exception messages.
        raise PipelineFailure(error_class) from None
