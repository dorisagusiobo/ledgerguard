"""Shared, bounded validation for synthetic S3 pipeline batches."""
from hashlib import sha256
import json
import re

REFERENCE_FILES = ('policy.md', 'contracts.json', 'history.json')
MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_REFERENCE_BYTES = 5 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024
DOCUMENT_ID = re.compile(r'[A-Za-z0-9-]{1,64}')
MANIFEST_KEY = re.compile(r'batches/([0-9a-f]{32})/manifest\.json')


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field')
        result[key] = value
    return result


def parse_manifest(raw: bytes) -> list[dict[str, str]]:
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ValueError('Manifest too large')
    value = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(value, dict) or set(value) != {'documents'}:
        raise ValueError('Unexpected manifest fields')
    docs = value['documents']
    if not isinstance(docs, list) or not 1 <= len(docs) <= 40:
        raise ValueError('Expected 1–40 documents')
    seen = set()
    for doc in docs:
        if not isinstance(doc, dict) or set(doc) != {'document_id', 'sha256'}:
            raise ValueError('Unexpected document fields')
        name, digest = doc['document_id'], doc['sha256']
        if not isinstance(name, str) or not DOCUMENT_ID.fullmatch(name) or name in seen:
            raise ValueError('Invalid or duplicate document ID')
        if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid document hash')
        seen.add(name)
    return docs


def validate_pdf(data: bytes, digest: str):
    if len(data) > MAX_PDF_BYTES or not data.startswith(b'%PDF') or sha256(data).hexdigest() != digest:
        raise ValueError('Invalid PDF')
