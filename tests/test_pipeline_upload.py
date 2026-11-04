"""Only dev PDFs and explicitly named reference files may reach S3."""
import json
from shutil import copyfile

import boto3
import pytest

from pipeline_s3_fakes import FakeS3
from ledgerguard import pipeline_upload as module


def test_dev_upload_excludes_held_out_pdfs_without_limit(pipeline_case, tmp_path):
    root, _, _, _, splits = pipeline_case
    dev_ids = sorted(name for name, split in splits.items() if split == 'dev')[:2]
    test_ids = sorted(name for name, split in splits.items() if split == 'test')[:2]
    assert len(dev_ids) == len(test_ids) == 2
    # Keep the mixed cohort below the 40-document bound so removing the split
    # filter fails on held-out leakage, rather than on an unrelated size limit.
    data = tmp_path / 'mixed-splits'
    (data / 'invoices_pdf').mkdir(parents=True)
    for name in dev_ids + test_ids:
        copyfile(root / 'invoices_pdf' / f'{name}.pdf', data / 'invoices_pdf' / f'{name}.pdf')
    (data / 'splits.json').write_text(json.dumps({name: splits[name] for name in dev_ids + test_ids}))
    for name in ('policy.md', 'contracts.json', 'history.json'):
        copyfile(root / name, data / name)

    s3 = FakeS3()
    batch_id = module.upload(data, split='dev', limit=None, bucket='synthetic-bucket', s3=s3)
    manifest = json.loads(s3.objects[f'batches/{batch_id}/manifest.json'])
    manifest_ids = {item['document_id'] for item in manifest['documents']}
    assert all(name not in key for name in test_ids for key in s3.objects)
    assert manifest_ids.isdisjoint(test_ids)
    assert manifest_ids == set(dev_ids)
    assert {key for key in s3.objects if '/invoices/' in key} == {
        f'batches/{batch_id}/invoices/{name}.pdf' for name in dev_ids}


def test_upload_allowlist_and_manifest_last(pipeline_case):
    root, pdfs, _, _, splits = pipeline_case
    s3 = FakeS3()
    batch_id = module.upload(root, split='dev', limit=3, bucket='synthetic-bucket', s3=s3)
    assert len(batch_id) == 32
    prefix = f'batches/{batch_id}'
    selected = sorted(name for name in pdfs if splits[name] == 'dev')[:3]
    assert set(s3.objects) == {f'{prefix}/invoices/{name}.pdf' for name in selected} | {
        f'{prefix}/reference/policy.md', f'{prefix}/reference/contracts.json', f'{prefix}/reference/history.json',
        f'{prefix}/manifest.json'}
    assert s3.calls[-1] == ('put', f'{prefix}/manifest.json')
    assert all('/answer_key/' not in key and '/invoices/' not in key or key.endswith('.pdf') for key in s3.objects)
    manifest = json.loads(s3.objects[f'{prefix}/manifest.json'])
    assert {item['document_id'] for item in manifest['documents']} == set(selected)
    assert set(manifest) == {'documents'}


def test_test_split_refused_before_aws_or_upload(pipeline_case, monkeypatch):
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: pytest.fail('No AWS client'))
    with pytest.raises(SystemExit) as caught:
        module.main(['--data', str(pipeline_case[0]), '--split', 'test', '--bucket', 'synthetic-bucket'])
    assert caught.value.code == 2
    s3 = FakeS3()
    with pytest.raises(ValueError):
        module.upload(pipeline_case[0], split='test', limit=3, bucket='synthetic-bucket', s3=s3)
    assert not s3.calls


def test_failed_upload_never_publishes_manifest(pipeline_case):
    class BrokenS3(FakeS3):
        def put_object(self, **request):
            if '/reference/' in request['Key']:
                raise OSError('synthetic outage')
            return super().put_object(**request)
    s3 = BrokenS3()
    with pytest.raises(OSError):
        module.upload(pipeline_case[0], split='dev', limit=3, bucket='synthetic-bucket', s3=s3)
    assert not any(key.endswith('/manifest.json') for key in s3.objects)


def test_cli_prints_only_batch_id(pipeline_case, monkeypatch, capsys):
    s3 = FakeS3()
    monkeypatch.setattr(boto3, 'client', lambda *a, **kw: s3)
    module.main(['--data', str(pipeline_case[0]), '--split', 'dev', '--limit', '3', '--bucket', 'synthetic-bucket'])
    output = capsys.readouterr().out.strip()
    assert len(output) == 32 and all(c in '0123456789abcdef' for c in output)
