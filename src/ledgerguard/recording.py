"""Synthetic Bedrock response fixtures; requests are hashed, never persisted."""
from collections import defaultdict, deque
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path


class RecordingMismatch(BaseException):
    """Fatal fixture-integrity failure, never a recoverable provider referral.

    BaseException is deliberate: Gateway wraps ordinary provider exceptions in
    RuntimeError and stages turn those into referrals. Replay must stop at the
    first mismatch. CLI callers catch this specific error; no live fallback exists.
    """


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)


def request_stage(request: dict) -> str:
    if any('document' in block for message in request.get('messages', []) for block in message.get('content', [])):
        return 'extraction'
    names = {tool['toolSpec']['name'] for tool in request['toolConfig']['tools']}
    if 'record_policy_findings' in names:
        return 'policy'
    if 'submit_notes' in names:
        return 'investigation'
    raise ValueError('Unrecognized pipeline request type')


def request_key(request: dict) -> str:
    """Hash {modelId, request}; replace binary document values with SHA-256."""
    def hashed(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return sha256(value).hexdigest()
        if isinstance(value, dict):
            return {key: hashed(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [hashed(item) for item in value]
        return value
    payload = {'modelId': request['modelId'],
               'request': hashed({key: value for key, value in request.items() if key != 'modelId'})}
    return sha256(canonical(payload).encode('utf-8')).hexdigest()


def data_hash(data: Path) -> str:
    """Hash sorted relative-file-name/hash pairs for dev inputs only.

    Never opens invoices/*.json, answer_key/, or held-out PDFs. The split manifest
    is input selection metadata, not an answer key. Include generator metadata so
    the seed is part of the fingerprint too.
    """
    data = Path(data)
    splits = json.loads((data / 'splits.json').read_text())
    paths = [Path(name) for name in ('metadata.json', 'splits.json', 'contracts.json', 'history.json', 'policy.md')]
    paths += [Path('invoices_pdf') / f'{number}.pdf' for number, split in splits.items() if split == 'dev']
    hashes = []
    for relative in sorted(paths):
        path = data / relative
        if not path.resolve().is_relative_to(data.resolve()) or path.is_symlink():
            raise RecordingMismatch('Unsafe recording data path; re-record with generated synthetic data')
        hashes.append([relative.as_posix(), sha256(path.read_bytes()).hexdigest()])
    return sha256(canonical(hashes).encode('utf-8')).hexdigest()


def recording_header(data: Path, model_ids: dict, git_commit: str | None) -> dict:
    metadata = json.loads((Path(data) / 'metadata.json').read_text())
    return {'type': 'header', 'format_version': 1, 'split': 'dev', 'seed': metadata['seed'],
            'data_hash': data_hash(data), 'model_ids': dict(model_ids), 'git_commit': git_commit,
            'date': datetime.now(timezone.utc).isoformat()}


def mismatch(stage, model, reason):
    return RecordingMismatch(f'{reason}: stage={stage}, model={model}; re-record the dev pipeline')


def has_reasoning(value):
    if isinstance(value, dict):
        return 'reasoningContent' in value or any(has_reasoning(v) for v in value.values())
    return isinstance(value, list) and any(has_reasoning(v) for v in value)


class RecordingClient:
    def __init__(self, client, path: Path, header: dict):
        self.client = client
        self.meta = getattr(client, 'meta', None)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Refuse to overwrite an existing live recording.
        with self.path.open('x', encoding='utf-8') as file:
            file.write(canonical(header) + '\n')

    def converse(self, **request):
        stage, model = request_stage(request), request['modelId']
        key = request_key(request)
        response = self.client.converse(**request)
        saved = {field: deepcopy(response[field]) for field in ('output', 'stopReason', 'usage') if field in response}
        if has_reasoning(saved):
            raise mismatch(stage, model, 'Private reasoning blocks cannot be recorded')
        try:
            line = canonical({'key': key, 'stage': stage, 'model_id': model, 'response': saved})
            with self.path.open('a', encoding='utf-8') as file:
                file.write(line + '\n')
                file.flush()
        except (OSError, TypeError, ValueError):
            raise mismatch(stage, model, 'Could not persist recording') from None
        return response


class ReplayClient:
    """No SDK, credentials, retries, meta, or live-client reference."""
    def __init__(self, path: Path, *, data: Path, model_ids: dict):
        self._responses = defaultdict(deque)
        try:
            with Path(path).open(encoding='utf-8') as file:
                self.header = json.loads(next(file))
                if (self.header.get('type') != 'header' or self.header.get('format_version') != 1
                        or self.header.get('split') != 'dev'):
                    raise RecordingMismatch('Invalid recording header; re-record the dev pipeline')
                seed = json.loads((Path(data) / 'metadata.json').read_text())['seed']
                if self.header.get('seed') != seed:
                    raise RecordingMismatch('Recording seed mismatch; re-record the dev pipeline')
                if self.header.get('data_hash') != data_hash(data):
                    raise RecordingMismatch('Recording data-hash mismatch; re-record the dev pipeline')
                if self.header.get('model_ids') != model_ids:
                    raise RecordingMismatch('Recording model IDs mismatch; re-record the dev pipeline')
                for line in file:
                    row = json.loads(line)
                    if (set(row) != {'key', 'stage', 'model_id', 'response'} or not isinstance(row['key'], str)
                            or len(row['key']) != 64 or any(c not in '0123456789abcdef' for c in row['key'])
                            or row['stage'] not in {'extraction', 'policy', 'investigation'}
                            or not isinstance(row['model_id'], str) or not isinstance(row['response'], dict)
                            or set(row['response']) - {'output', 'stopReason', 'usage'} or has_reasoning(row['response'])):
                        raise RecordingMismatch('Invalid response recording; re-record the dev pipeline')
                    key = row['key']
                    self._responses[key].append(row)
        except (OSError, ValueError, StopIteration, TypeError, AttributeError, KeyError):
            raise RecordingMismatch('Unreadable recording; re-record the dev pipeline') from None

    def converse(self, **request):
        key, stage, model = request_key(request), request_stage(request), request['modelId']
        rows = self._responses.get(key)
        if not rows:
            raise mismatch(stage, model, 'Missing or exhausted recording key')
        row = rows[0]
        if row['stage'] != stage or row['model_id'] != model:
            raise mismatch(stage, model, 'Recording stage/model mismatch')
        rows.popleft()
        return deepcopy(row['response'])

    def assert_all_used(self):
        for rows in self._responses.values():
            if rows:
                row = rows[0]
                raise mismatch(row['stage'], row['model_id'], 'Unused response recording')
