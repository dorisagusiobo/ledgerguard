"""Recording/replay tests use temporary fake responses, never paid services."""
from copy import deepcopy
from decimal import Decimal
import os
import json
from pathlib import Path

import boto3
import pytest

from fakes import FakeClient
from ledgerguard.extractor import build_request
from ledgerguard.gateway import NOVA_MODEL_ID
from ledgerguard.pipeline import PipelineModels
import ledgerguard.pipeline_eval as module
from ledgerguard.recording import RecordingClient, RecordingMismatch, ReplayClient, data_hash, recording_header, request_key

MODELS = PipelineModels()
IDS = {'extract': MODELS.extract, 'policy': MODELS.policy, 'investigate': MODELS.investigate}


def record(case, tmp_path, *, tool=True):
    root, pdfs, invoices, answers, _ = case
    path = tmp_path / 'responses.jsonl'
    client = RecordingClient(FakeClient(pdfs, invoices, answers, tool=tool), path,
                             recording_header(root, IDS, 'synthetic-unit-test'))
    report = module.compare(root, client=client, max_cost=Decimal('0.50'),
                            audit_path=tmp_path / 'record-audit.jsonl')
    return path, report, client


def replay(case, path, tmp_path, *, limit=None, max_cost=Decimal('0.50')):
    client = ReplayClient(path, data=case[0], model_ids=IDS)
    report = module.compare(case[0], client=client, max_cost=max_cost,
                            audit_path=tmp_path / 'replay-audit.jsonl', limit=limit)
    return report, client


def test_canonical_request_key_and_pdf_digest():
    a = {'modelId': NOVA_MODEL_ID, 'messages': [{'content': [{'document': {'source': {'bytes': b'%PDF synthetic bytes'}}}]}],
         'inferenceConfig': {'maxTokens': 100, 'temperature': 0}}
    b = {key: deepcopy(a[key]) for key in reversed(a)}
    b['inferenceConfig'] = {'temperature': 0, 'maxTokens': 100}
    assert request_key(a) == request_key(b)
    assert request_key(a) == request_key(deepcopy(a))
    b['messages'][0]['content'][0]['document']['source']['bytes'] = b'%PDF different bytes'
    assert request_key(a) != request_key(b)


def assert_dev_acceptance(report):
    # Fixed code assertions: editing a recording cannot relax the acceptance bar.
    assert report['mode'] == 'replayed'
    assert report['end_to_end']['overall']['caught'] == report['end_to_end']['overall']['expected'] == 14
    assert report['end_to_end']['overall']['recall'] == report['end_to_end']['overall']['precision'] == 1
    assert report['end_to_end']['clean_invoices'] == {'wrongly_flagged': 0, 'total': 20}
    assert report['extraction']['exact_matches'] == report['extraction']['selected'] == 38
    assert report['extraction']['accuracy'] == 1
    assert report['investigation'] == {'groups': 14, 'completed': 14, 'completion_rate': 1}
    assert report['referrals']['count'] == 0 and report['stopped'] is None
    assert Decimal(report['cost']['total']) <= Decimal('0.50')
    assert report['largest_run']['within_limits']
    assert report['cost']['stages_reconcile']


def test_full_dev_fake_round_trip_and_acceptance(pipeline_case, tmp_path, monkeypatch):
    path, original, _ = record(pipeline_case, tmp_path)
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('Replay must never create an AWS client'))
    replayed, client = replay(pipeline_case, path, tmp_path)
    assert not hasattr(client, 'meta')
    assert_dev_acceptance(replayed)
    for key in ('end_to_end', 'baseline', 'extraction', 'investigation', 'referrals', 'findings', 'notes', 'budgets', 'cost'):
        assert replayed[key] == original[key]
    assert replayed['mode'] in module.render(replayed)
    # Returned responses are independent copies, not mutable fixture state.
    assert all(not rows for rows in client._responses.values())


def test_recording_contains_only_header_keys_and_responses(pipeline_case, tmp_path):
    path, _, _ = record(pipeline_case, tmp_path)
    raw = path.read_bytes()
    for pdf in pipeline_case[1].values():
        assert pdf not in raw
    rows = [json.loads(line) for line in raw.splitlines()]
    assert rows[0]['seed'] == 7 and rows[0]['split'] == 'dev'
    assert rows[0]['model_ids'] == IDS and rows[0]['data_hash'] == data_hash(pipeline_case[0])
    assert set(rows[0]) == {'type', 'format_version', 'split', 'seed', 'data_hash', 'model_ids', 'git_commit', 'date'}
    for row in rows[1:]:
        assert set(row) == {'key', 'stage', 'model_id', 'response'}
        assert set(row['response']) <= {'output', 'stopReason', 'usage'}
    assert b'Extract the invoice using record_invoice' not in raw
    assert b'inputSchema' not in raw


def test_missing_key_is_fatal_at_first_call(pipeline_case, tmp_path):
    path, _, _ = record(pipeline_case, tmp_path)
    lines = path.read_text().splitlines()
    path.write_text('\n'.join([lines[0], *lines[2:]]) + '\n')
    client = ReplayClient(path, data=pipeline_case[0], model_ids=IDS)
    with pytest.raises(RecordingMismatch, match='stage=extraction.*model=.*re-record'):
        module.compare(pipeline_case[0], client=client, max_cost=Decimal('0.50'),
                       audit_path=tmp_path / 'missing-audit.jsonl')
    assert sum(len(rows) for rows in client._responses.values()) == len(lines) - 2
    logs = [json.loads(line) for line in (tmp_path / 'missing-audit.jsonl').read_text().splitlines()]
    assert len(logs) == 1  # No subsequent provider-error referrals/calls.
    assert logs[0]['outcome'] == 'aborted' and logs[0]['reason'] == 'reservation_retained'


def test_unused_recording_fails(pipeline_case, tmp_path):
    path, _, _ = record(pipeline_case, tmp_path)
    with path.open('a') as file:
        file.write(path.read_text().splitlines()[1] + '\n')
    with pytest.raises(RecordingMismatch, match='Unused.*stage=.*model=.*re-record'):
        replay(pipeline_case, path, tmp_path)


def test_data_hash_mismatch_fails_before_replay(pipeline_case, tmp_path):
    path, _, _ = record(pipeline_case, tmp_path)
    lines = path.read_text().splitlines()
    header = json.loads(lines[0]); header['data_hash'] = '0' * 64
    path.write_text(json.dumps(header) + '\n' + '\n'.join(lines[1:]) + '\n')
    with pytest.raises(RecordingMismatch, match='data-hash mismatch.*re-record'):
        ReplayClient(path, data=pipeline_case[0], model_ids=IDS)


def test_hash_never_reads_truth_or_holdout(pipeline_case, monkeypatch):
    root, _, _, _, splits = pipeline_case
    read = Path.read_bytes
    def guarded(path):
        assert 'invoices' not in path.parts and 'answer_key' not in path.parts
        if path.suffix == '.pdf':
            assert splits[path.stem] == 'dev'
        return read(path)
    monkeypatch.setattr(Path, 'read_bytes', guarded)
    assert len(data_hash(root)) == 64


@pytest.fixture
def repeated_responses(pipeline_case, tmp_path):
    root, pdfs, invoices, answers, _ = pipeline_case
    fake = FakeClient(pdfs, invoices, answers)
    path = tmp_path / 'repeated.jsonl'
    recorder = RecordingClient(fake, path, recording_header(root, IDS, 'synthetic-test'))
    pdf = next(iter(pdfs.values()))
    request = {'modelId': NOVA_MODEL_ID, **build_request(pdf, NOVA_MODEL_ID)}
    first = recorder.converse(**request)
    fake.records[pdf]['vendor_name'] = 'Different synthetic response'
    second = recorder.converse(**request)
    return ReplayClient(path, data=root, model_ids=IDS), request, [first, second], path


def test_different_responses_under_one_key_replay_in_recorded_order(repeated_responses):
    client, request, responses, path = repeated_responses
    assert responses[0] != responses[1]
    rows = [json.loads(line) for line in path.read_text().splitlines()][1:]
    assert len(rows) == 2 and rows[0]['key'] == rows[1]['key']
    for expected in responses:
        assert client.converse(**request) == expected
    client.assert_all_used()


def test_third_request_for_repeated_key_is_exhausted(repeated_responses):
    client, request, _, _ = repeated_responses
    client.converse(**request)
    client.converse(**request)
    with pytest.raises(RecordingMismatch, match='exhausted'):
        client.converse(**request)


def test_identical_duplicates_are_counted_and_required(pipeline_case, tmp_path):
    path, _, recorder = record(pipeline_case, tmp_path)
    pdf = pipeline_case[1][sorted(pipeline_case[1])[0]]
    request = {'modelId': NOVA_MODEL_ID, **build_request(pdf, NOVA_MODEL_ID)}
    recorder.converse(**request)
    client = ReplayClient(path, data=pipeline_case[0], model_ids=IDS)
    client.converse(**request)
    assert len(client._responses[request_key(request)]) == 1
    with pytest.raises(RecordingMismatch, match='Unused'):
        client.assert_all_used()
    client.converse(**request)
    with pytest.raises(RecordingMismatch, match='exhausted'):
        client.converse(**request)


def test_replay_budget_is_still_enforced(pipeline_case, tmp_path):
    path, _, _ = record(pipeline_case, tmp_path)
    # A zero cap blocks before a response is consumed; leftover checks then fail.
    with pytest.raises(RecordingMismatch, match='Unused'):
        replay(pipeline_case, path, tmp_path, max_cost=Decimal(0))


@pytest.mark.parametrize('flags', [
    ['--live', '--replay', 'missing'], ['--record', 'new'],
    ['--replay', 'missing', '--record', 'new'], ['--replay', 'missing', '--split', 'test'],
    ['--live', '--record', 'new', '--split', 'test'],
    ['--live', '--record', 'new', '--limit', '4'], ['--replay', 'missing', '--limit', '4'],
])
def test_invalid_cli_modes_never_create_sdk(monkeypatch, flags):
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('No AWS client allowed'))
    with pytest.raises(SystemExit) as caught:
        module.main(['--data', '/missing', '--max-cost', '0.50', *flags])
    assert caught.value.code == 2


def test_cli_replay_has_no_sdk_or_credentials(pipeline_case, tmp_path, monkeypatch, capsys):
    path, _, _ = record(pipeline_case, tmp_path)
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('No AWS client allowed'))
    for name in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN', 'AWS_PROFILE'):
        monkeypatch.delenv(name, raising=False)
    output = tmp_path / 'replayed.json'
    module.main(['--data', str(pipeline_case[0]), '--max-cost', '0.50',
                 '--replay', str(path), '--json', str(output), '--audit', str(tmp_path / 'cli-audit.jsonl')])
    assert json.loads(output.read_text())['mode'] == 'replayed'
    assert 'replayed' in capsys.readouterr().out


def test_changed_input_file_invalidates_hash(pipeline_case, tmp_path):
    from shutil import copytree
    path, _, _ = record(pipeline_case, tmp_path)
    copy = tmp_path / 'changed-inputs'
    copytree(pipeline_case[0], copy)
    policy = copy / 'policy.md'
    policy.write_text(policy.read_text() + '\nSynthetic policy changed.\n')
    with pytest.raises(RecordingMismatch, match='data-hash mismatch.*re-record'):
        ReplayClient(path, data=copy, model_ids=IDS)


def test_cli_record_then_replay(pipeline_case, tmp_path, monkeypatch):
    root, pdfs, invoices, answers, _ = pipeline_case
    fake = FakeClient(pdfs, invoices, answers)
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: fake)
    path = tmp_path / 'cli-recording.jsonl'
    module.main(['--data', str(root), '--max-cost', '0.50', '--live', '--record', str(path),
                 '--audit', str(tmp_path / 'recorded-audit.jsonl')])
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('Replay cannot create SDK'))
    module.main(['--data', str(root), '--max-cost', '0.50', '--replay', str(path),
                 '--audit', str(tmp_path / 'replayed-audit.jsonl')])


def test_refuses_private_reasoning_response(pipeline_case, tmp_path):
    class ReasoningClient:
        def converse(self, **kwargs):
            return {'output': {'message': {'content': [{'reasoningContent': {'reasoningText': {'text': 'PRIVATE'}}}]}},
                    'stopReason': 'end_turn', 'usage': {'inputTokens': 1, 'outputTokens': 1}}
    root, pdfs, *_ = pipeline_case
    path = tmp_path / 'private.jsonl'
    client = RecordingClient(ReasoningClient(), path, recording_header(root, IDS, 'synthetic-test'))
    with pytest.raises(RecordingMismatch, match='Private reasoning'):
        client.converse(modelId=NOVA_MODEL_ID, **build_request(next(iter(pdfs.values())), NOVA_MODEL_ID))
    assert 'PRIVATE' not in path.read_text()


@pytest.mark.parametrize('mode', ['record', 'replay'])
def test_compare_refuses_limited_recording_and_replay(pipeline_case, tmp_path, mode):
    path, _, recorder = record(pipeline_case, tmp_path)
    client = recorder if mode == 'record' else ReplayClient(path, data=pipeline_case[0], model_ids=IDS)
    with pytest.raises(ValueError, match='limit is not allowed'):
        module.compare(pipeline_case[0], client=client, max_cost=Decimal('0.50'),
                       audit_path=tmp_path / 'limited-audit.jsonl', limit=4)
    assert not (tmp_path / 'limited-audit.jsonl').exists()


def test_committed_dev_recording_replays(pipeline_case, tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / 'evals/recordings/pipeline-dev.jsonl'
    if not path.is_file():
        message = 'Missing evals/recordings/pipeline-dev.jsonl; capture and commit the genuine dev recording with this test'
        if os.environ.get('CI', '').lower() == 'true':
            pytest.fail(message, pytrace=False)
        pytest.skip(message)
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: pytest.fail('Committed replay must never create an AWS client'))
    report, _ = replay(pipeline_case, path, tmp_path)
    assert_dev_acceptance(report)


@pytest.mark.parametrize('ci', ['', 'true'])
def test_missing_committed_recording_skip_or_fail(pipeline_case, tmp_path, monkeypatch, ci):
    path = Path(__file__).resolve().parents[1] / 'evals/recordings/pipeline-dev.jsonl'
    original = Path.is_file
    monkeypatch.setattr(Path, 'is_file', lambda item: False if item == path else original(item))
    monkeypatch.setenv('CI', ci)
    expected = pytest.fail.Exception if ci == 'true' else pytest.skip.Exception
    with pytest.raises(expected, match='Missing evals/recordings/pipeline-dev.jsonl'):
        test_committed_dev_recording_replays(pipeline_case, tmp_path, monkeypatch)


@pytest.mark.parametrize('field,value', [('stage', 'policy'), ('model_id', 'different-synthetic-model')])
def test_repeated_key_checks_each_queued_rows_stage_and_model(repeated_responses, pipeline_case, field, value):
    _, request, _, path = repeated_responses
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[2][field] = value
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    client = ReplayClient(path, data=pipeline_case[0], model_ids=IDS)
    client.converse(**request)
    with pytest.raises(RecordingMismatch, match='stage/model mismatch'):
        client.converse(**request)
    assert len(client._responses[request_key(request)]) == 1
    with pytest.raises(RecordingMismatch, match='Unused'):
        client.assert_all_used()
