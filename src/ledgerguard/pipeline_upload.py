"""Upload allowlisted dev PDFs/reference files; publish the manifest last."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from ledgerguard.pdf import load_pdf_inputs
from ledgerguard.pipeline_batch import MAX_REFERENCE_BYTES, REFERENCE_FILES, parse_manifest, validate_pdf
from ledgerguard.rules import load_reference


def upload(data: Path, *, split: str, limit: int | None, bucket: str, s3) -> str:
    if split != 'dev':
        raise ValueError('Only dev uploads are allowed')
    if limit is not None and (type(limit) is not int or not 1 <= limit <= 40):
        raise ValueError('Limit must be 1–40')
    if not bucket or '/' in bucket:
        raise ValueError('Expected a bucket name')
    data = Path(data)
    splits = json.loads((data / 'splits.json').read_text())
    pdfs = load_pdf_inputs(data)
    selected = sorted(name for name in pdfs if splits.get(name) == 'dev')
    if limit is not None:
        selected = selected[:limit]
    manifest = {'documents': [{'document_id': name, 'sha256': sha256(pdfs[name]).hexdigest()}
                              for name in selected]}
    raw = json.dumps(manifest, sort_keys=True).encode()
    parse_manifest(raw)
    for doc in manifest['documents']:
        validate_pdf(pdfs[doc['document_id']], doc['sha256'])
    references = {}
    for name in REFERENCE_FILES:
        path = data / name
        if path.is_symlink() or path.stat().st_size > MAX_REFERENCE_BYTES:
            raise ValueError('Invalid reference file')
        references[name] = path.read_bytes()
    load_reference(data)  # Validate before uploading anything; never loads truth.
    batch_id = uuid4().hex
    prefix = f'batches/{batch_id}'
    for name in selected:
        s3.put_object(Bucket=bucket, Key=f'{prefix}/invoices/{name}.pdf', Body=pdfs[name],
                      ContentType='application/pdf', ServerSideEncryption='AES256', IfNoneMatch='*')
    for name, content in references.items():
        s3.put_object(Bucket=bucket, Key=f'{prefix}/reference/{name}', Body=content,
                      ServerSideEncryption='AES256', IfNoneMatch='*')
    s3.put_object(Bucket=bucket, Key=f'{prefix}/manifest.json', Body=raw,
                  ContentType='application/json', ServerSideEncryption='AES256', IfNoneMatch='*')
    return batch_id


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--split', choices=['dev'], required=True)
    parser.add_argument('--limit', type=int)
    parser.add_argument('--bucket', required=True)
    args = parser.parse_args(argv)
    if args.limit is not None and not 1 <= args.limit <= 40:
        parser.error('--limit must be 1–40')
    try:
        import boto3
        from botocore.config import Config
        s3 = boto3.client('s3', region_name='us-east-1', config=Config(retries={'total_max_attempts': 1}))
        batch_id = upload(args.data, split=args.split, limit=args.limit, bucket=args.bucket, s3=s3)
    except Exception as error:
        parser.exit(1, type(error).__name__ + '\n')
    print(batch_id)


if __name__ == '__main__':
    main()
