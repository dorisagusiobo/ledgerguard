"""Exact review contract against isolated real Postgres; no live model calls."""
from collections import defaultdict
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import re
from uuid import uuid4

from fastapi.testclient import TestClient
import psycopg
import pytest

import ledgerguard.api as api
from ledgerguard.investigator import Note
from ledgerguard.pipeline import PipelineModels, PipelineResult
from ledgerguard.rules import load_reference, run_rules
from ledgerguard.store import RunMetadata, load_findings, save_run

TOKEN = 'synthetic-review-api-token'
HEADERS = {'Authorization': f'Bearer {TOKEN}'}
ROUTES = {('GET', '/health'), ('GET', '/runs'), ('GET', '/runs/{run_id}/findings'),
          ('GET', '/findings/{finding_id}'), ('POST', '/findings/{finding_id}/decisions')}


@pytest.fixture
def review_data(db_app, pipeline_case):
    root, pdfs, invoices, _, _ = pipeline_case
    reference = load_reference(root)
    findings = run_rules(invoices, reference.contracts, reference.history, reference.policy_clauses).findings
    groups = defaultdict(list)
    for finding in findings:
        groups[finding.invoice_numbers[0]].append(finding)
    result = PipelineResult(invoices={i.invoice_number: i for i in invoices}, findings=findings,
                            notes={number: [Note(finding_index=index, summary='Synthetic evidence note.',
                                               related_invoices=finding.invoice_numbers, uncertainty='low')
                                            for index, finding in enumerate(group)] for number, group in groups.items()})
    started = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)
    models = PipelineModels()
    metadata = RunMetadata(run_id=result.pipeline_run_id, started_at=started, finished_at=started + timedelta(seconds=5),
                           model_ids={'extract': models.extract, 'policy': models.policy, 'investigate': models.investigate},
                           document_hashes={n: sha256(pdf).hexdigest() for n, pdf in pdfs.items()})
    save_run(db_app, result, metadata)
    first = load_findings(db_app, metadata.run_id)[0].finding_id
    return result, metadata, first


@pytest.fixture
def client(app_conninfo, monkeypatch):
    monkeypatch.setenv('APP_DATABASE_URL', app_conninfo)
    monkeypatch.setenv('REVIEW_API_TOKEN', TOKEN)
    monkeypatch.setenv('REVIEW_REVIEWER', 'synthetic-reviewer')
    with TestClient(api.create_app()) as client:
        yield client


def test_exact_five_routes(client):
    assert {(method, route.path) for route in client.app.routes for method in route.methods} == ROUTES
    assert len(client.app.routes) == 5
    for path in ('/docs', '/redoc', '/openapi.json'):
        response = client.get(path, headers=HEADERS)
        assert response.status_code == 404 and response.json() == {'detail': 'Not found'}


def test_health_without_auth_or_database(client, monkeypatch):
    monkeypatch.delenv('APP_DATABASE_URL')
    monkeypatch.delenv('REVIEW_API_TOKEN')
    assert client.get('/health').json() == {'status': 'ok'}


@pytest.mark.parametrize('authorization', [None, 'Bearer wrong', 'Basic wrong', 'Bearer', 'Bearer wrong extra'])
def test_all_protected_endpoints_require_token(client, authorization):
    headers = {} if authorization is None else {'Authorization': authorization}
    for method, path in [('GET', '/runs'), ('GET', '/runs/missing/findings'), ('GET', '/findings/1'),
                         ('POST', '/findings/1/decisions')]:
        response = client.request(method, path, headers=headers)
        assert response.status_code == 401 and response.json() == {'detail': 'Not authenticated'}
    response = client.post('/findings/1/decisions', headers={**headers, 'Content-Type': 'application/json'},
                           content='{invalid json')
    assert response.status_code == 401


@pytest.mark.parametrize('method', ['PUT', 'PATCH', 'DELETE'])
def test_no_edit_delete_methods(client, method):
    for path in ('/health', '/runs', '/runs/a/findings', '/findings/1', '/findings/1/decisions'):
        assert client.request(method, path, headers=HEADERS).status_code == 405


def test_findings_exact_shapes_order_notes_and_decimal_strings(client, review_data):
    result, metadata, _ = review_data
    response = client.get(f'/runs/{metadata.run_id}/findings', headers=HEADERS)
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {'run_id', 'findings'} and payload['run_id'] == metadata.run_id
    assert len(payload['findings']) == len(result.findings)
    for position, (row, original) in enumerate(zip(payload['findings'], result.findings)):
        assert set(row) == set(api.FindingOutput.model_fields)
        assert row['position'] == position and row['run_id'] == metadata.run_id
        assert row['amount_at_issue'] == format(original.amount_at_issue, '.2f')
        assert re.fullmatch(r'\d+\.\d{2}', row['amount_at_issue'])
        assert row['evidence'] == [source.model_dump() for source in original.evidence]
        assert row['citation'] == (original.citation.model_dump() if original.citation else None)
        assert row['status'] == 'pending'
        assert row['notes'] == [n.model_dump() for n in result.notes[original.invoice_numbers[0]]]


def test_runs_newest_first_and_decision_counts(client, review_data, db_app):
    result, metadata, finding_id = review_data
    later = deepcopy(result)
    later.pipeline_run_id = uuid4().hex
    later_metadata = replace(metadata, run_id=later.pipeline_run_id,
                             started_at=metadata.started_at + timedelta(hours=1),
                             finished_at=metadata.finished_at + timedelta(hours=1))
    save_run(db_app, later, later_metadata)
    assert client.post(f'/findings/{finding_id}/decisions', headers=HEADERS,
                       json={'decision': 'approve', 'note': 'Synthetic approval'}).status_code == 201
    response = client.get('/runs', headers=HEADERS)
    assert response.status_code == 200
    runs = response.json()['runs']
    subset = [r for r in runs if r['run_id'] in (metadata.run_id, later_metadata.run_id)]
    assert [r['run_id'] for r in subset] == [later_metadata.run_id, metadata.run_id]
    assert subset[1]['findings'] == {'pending': len(result.findings) - 1, 'approved': 1, 'rejected': 0}
    assert subset[0]['findings'] == {'pending': len(result.findings), 'approved': 0, 'rejected': 0}
    assert set(subset[0]) == set(api.RunOutput.model_fields)
    assert subset[1]['model_ids'] == metadata.model_ids
    assert datetime.fromisoformat(subset[1]['started_at']).utcoffset() == timedelta(0)


def test_detail_invoices_scoped_to_run_and_preserve_sources(client, review_data, db_app):
    result, metadata, finding_id = review_data
    other = deepcopy(result)
    other.pipeline_run_id = uuid4().hex
    for number, invoice in other.invoices.items():
        other.invoices[number] = invoice.model_copy(update={'vendor_name': 'Other synthetic run vendor'})
    save_run(db_app, other, replace(metadata, run_id=other.pipeline_run_id))
    payload = client.get(f'/findings/{finding_id}', headers=HEADERS).json()
    assert set(payload) == {'finding', 'invoices', 'decisions'} and payload['decisions'] == []
    assert {i['invoice_number'] for i in payload['invoices']} == set(result.findings[0].invoice_numbers)
    for invoice in payload['invoices']:
        original = result.invoices[invoice['document_id']]
        assert invoice['vendor_name'] == original.vendor_name
        assert set(invoice) == set(api.InvoiceOutput.model_fields)
        for name in ('subtotal', 'tax', 'total'):
            assert invoice[name] == format(getattr(original, name), '.2f')
        for index, (line, original_line) in enumerate(zip(invoice['line_items'], original.line_items)):
            assert line == {'position': index, 'description': original_line.description,
                            'quantity': format(original_line.quantity, 'f'),
                            'unit_price': format(original_line.unit_price, '.4f'),
                            'amount': format(original_line.amount, '.2f'), 'source': original_line.source.model_dump()}


def test_append_decisions_uses_environment_identity_and_updates_views(client, review_data, db_app):
    _, metadata, finding_id = review_data
    records = []
    for decision, expected in [('approve', 'approved'), ('reject', 'rejected'), ('approve', 'approved')]:
        response = client.post(f'/findings/{finding_id}/decisions', headers={**HEADERS, 'X-Reviewer': 'forged'},
                               json={'decision': decision, 'note': '  Synthetic decision note  '})
        assert response.status_code == 201
        data = response.json()
        assert set(data) == set(api.DecisionCreated.model_fields)
        assert data['reviewer'] == 'synthetic-reviewer' and data['note'] == 'Synthetic decision note'
        assert data['status'] == expected and data['finding_id'] == finding_id
        assert datetime.fromisoformat(data['time']).utcoffset() == timedelta(0)
        records.append(data)
    assert [r['decision_id'] for r in records] == sorted(r['decision_id'] for r in records)
    detail = client.get(f'/findings/{finding_id}', headers=HEADERS).json()
    assert detail['finding']['status'] == 'approved'
    assert [row['decision'] for row in detail['decisions']] == ['approve', 'reject', 'approve']
    assert all(set(row) == set(api.DecisionOutput.model_fields) for row in detail['decisions'])
    approved = client.get(f'/runs/{metadata.run_id}/findings?status=approved', headers=HEADERS).json()['findings']
    assert [row['finding_id'] for row in approved] == [finding_id]
    rejected = client.get(f'/runs/{metadata.run_id}/findings?status=rejected', headers=HEADERS).json()['findings']
    assert rejected == []
    # The committed row is visible on a separate app connection after the response.
    assert db_app.execute('SELECT count(*) FROM decisions WHERE finding_id=%s', (finding_id,)).fetchone()[0] == 3


@pytest.mark.parametrize('body', [
    {'decision': 'pay', 'note': 'PRIVATE-SUBMITTED-CONTENT'},
    {'decision': 'approve', 'note': ''}, {'decision': 'approve', 'note': '   '},
    {'decision': 'approve', 'note': 'x' * 1001}, {'decision': 'approve', 'note': 42},
    {'decision': 'approve'}, {}, [],
    {'decision': 'approve', 'note': 'PRIVATE-SUBMITTED-CONTENT', 'reviewer': 'forged'},
    {'decision': 'approve', 'note': 'PRIVATE-SUBMITTED-CONTENT', 'time': '2020-01-01'},
    {'decision': 'approve', 'note': 'PRIVATE-SUBMITTED-CONTENT', 'status': 'approved'},
])
def test_invalid_bodies_are_sanitized_and_never_insert(client, review_data, db_app, body):
    finding_id = review_data[2]
    response = client.post(f'/findings/{finding_id}/decisions', headers=HEADERS, json=body)
    assert response.status_code == 422
    assert all(set(error) == {'loc', 'msg'} for error in response.json()['detail'])
    assert 'PRIVATE-SUBMITTED-CONTENT' not in response.text
    assert db_app.execute('SELECT count(*) FROM decisions WHERE finding_id=%s', (finding_id,)).fetchone()[0] == 0


def test_trimmed_note_boundaries(client, review_data):
    for note in (' x ', '  ' + 'x' * 1000 + '  '):
        response = client.post(f'/findings/{review_data[2]}/decisions', headers=HEADERS,
                               json={'decision': 'approve', 'note': note})
        assert response.status_code == 201 and response.json()['note'] == note.strip()


def test_unknown_ids_and_validation_errors(client, review_data):
    for method, path, kwargs in [('GET', '/runs/missing/findings', {}), ('GET', '/findings/-1', {}),
                                 ('POST', '/findings/-1/decisions', {'json': {'decision': 'approve', 'note': 'Synthetic'}})]:
        response = client.request(method, path, headers=HEADERS, **kwargs)
        assert response.status_code == 404 and response.json() == {'detail': 'Not found'}
    for path in (f'/runs/{review_data[1].run_id}/findings?status=PRIVATE-INVALID', '/findings/PRIVATE-INVALID'):
        response = client.get(path, headers=HEADERS)
        assert response.status_code == 422 and 'PRIVATE-INVALID' not in response.text
        assert all(set(item) == {'loc', 'msg'} for item in response.json()['detail'])
    response = client.post(f'/findings/{review_data[2]}/decisions', headers={**HEADERS, 'Content-Type': 'application/json'},
                           content='{"note": "PRIVATE-SUBMITTED-CONTENT"')
    assert response.status_code == 422 and 'PRIVATE-SUBMITTED-CONTENT' not in response.text


@pytest.mark.parametrize('failure', [psycopg.OperationalError, psycopg.ProgrammingError, OSError])
def test_database_errors_are_sanitized_on_every_protected_route(client, monkeypatch, failure):
    def broken(url):
        raise failure('PRIVATE-CONNECTION-DETAILS')
    monkeypatch.setattr(api, 'app_connection', broken)
    for method, path, kwargs in [('GET', '/runs', {}), ('GET', '/runs/test/findings', {}),
                                 ('GET', '/findings/1', {}), ('POST', '/findings/1/decisions',
                                 {'json': {'decision': 'approve', 'note': 'Synthetic note'}})]:
        response = client.request(method, path, headers=HEADERS, **kwargs)
        assert response.status_code == 503 and response.json() == {'detail': 'Service unavailable'}
        assert 'PRIVATE-CONNECTION-DETAILS' not in response.text


def test_missing_configuration_fails_closed(client, monkeypatch):
    monkeypatch.delenv('REVIEW_API_TOKEN')
    assert client.get('/runs', headers=HEADERS).status_code == 401
    monkeypatch.setenv('REVIEW_API_TOKEN', TOKEN)
    monkeypatch.delenv('APP_DATABASE_URL')
    response = client.get('/runs', headers=HEADERS)
    assert response.status_code == 503 and response.json() == {'detail': 'Service unavailable'}


def test_missing_reviewer_never_accepts_request_identity(client, review_data, monkeypatch):
    monkeypatch.delenv('REVIEW_REVIEWER')
    response = client.post(f'/findings/{review_data[2]}/decisions', headers={**HEADERS, 'X-Reviewer': 'forged'},
                           json={'decision': 'approve', 'note': 'Synthetic note'})
    assert response.status_code == 503 and response.json() == {'detail': 'Service unavailable'}


def test_commit_failure_returns_503_and_does_not_save_decision(client, review_data, db_owner, db_app):
    finding_id = review_data[2]
    db_owner.execute('''CREATE FUNCTION api_test_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'PRIVATE-COMMIT-DETAILS'; END $$''')
    db_owner.execute('''CREATE CONSTRAINT TRIGGER api_test_commit_failure AFTER INSERT ON decisions
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION api_test_commit_failure()''')
    try:
        response = client.post(f'/findings/{finding_id}/decisions', headers=HEADERS,
                               json={'decision': 'approve', 'note': 'Synthetic decision'})
        assert response.status_code == 503 and response.json() == {'detail': 'Service unavailable'}
        assert db_app.execute('SELECT count(*) FROM decisions WHERE finding_id=%s', (finding_id,)).fetchone()[0] == 0
    finally:
        db_owner.execute('DROP TRIGGER api_test_commit_failure ON decisions')
        db_owner.execute('DROP FUNCTION api_test_commit_failure()')


def test_unknown_out_of_range_integer_ids_are_not_database_errors(client):
    finding_id = 2 ** 100
    for method, path, kwargs in [('GET', f'/findings/{finding_id}', {}),
                                ('POST', f'/findings/{finding_id}/decisions',
                                 {'json': {'decision': 'approve', 'note': 'Synthetic'}})]:
        response = client.request(method, path, headers=HEADERS, **kwargs)
        assert response.status_code == 404 and response.json() == {'detail': 'Not found'}


@pytest.mark.parametrize('migration_key', [17492103, 17492104])
def test_decision_does_not_wait_for_migration_locks(client, review_data, db_owner, app_conninfo, monkeypatch, migration_key):
    from psycopg.conninfo import make_conninfo

    # A short DB timeout turns the old accidental lock collision into a 503,
    # rather than allowing this regression test to hang.
    monkeypatch.setenv('APP_DATABASE_URL', make_conninfo(
        app_conninfo, options='-c role=ledgerguard_app -c lock_timeout=1000'))
    result, _, source_id = review_data
    db_owner.execute('''INSERT INTO findings (finding_id, run_id, position, type, invoice_numbers,
        amount_at_issue, calculation, evidence, citation, no_clause_reason, confidence)
        OVERRIDING SYSTEM VALUE
        SELECT %s, run_id, %s, type, invoice_numbers, amount_at_issue, calculation,
               evidence, citation, no_clause_reason, confidence
        FROM findings WHERE finding_id=%s''', (migration_key, len(result.findings), source_id))
    with db_owner.transaction():
        db_owner.execute('SELECT pg_advisory_xact_lock(%s)', (migration_key,))
        response = client.post(f'/findings/{migration_key}/decisions', headers=HEADERS,
                               json={'decision': 'approve', 'note': 'Synthetic decision during migration lock'})
        assert response.status_code == 201
        assert response.json()['status'] == 'approved'


def with_lambda_event(app, event):
    async def wrapped(scope, receive, send):
        if scope['type'] == 'http':
            scope['aws.event'] = event
        await app(scope, receive, send)
    return wrapped


def test_aws_identity_comes_only_from_gateway_event(review_data, app_conninfo, monkeypatch):
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.delenv('REVIEW_REVIEWER', raising=False)
    monkeypatch.delenv('REVIEW_API_TOKEN', raising=False)
    monkeypatch.setattr(api, 'runtime_database_url', lambda: app_conninfo)
    claims = {'email': 'synthetic-reviewer@example.invalid', 'email_verified': 'true', 'token_use': 'id'}
    event = {'requestContext': {'authorizer': {'jwt': {'claims': claims}}}}
    with TestClient(with_lambda_event(api.create_app(), event)) as aws:
        response = aws.post(f'/findings/{review_data[2]}/decisions',
                            headers={'X-Reviewer': 'forged', 'Authorization': 'Bearer untrusted-header'},
                            json={'decision': 'approve', 'note': 'Synthetic AWS decision'})
    assert response.status_code == 201
    assert response.json()['reviewer'] == claims['email']


@pytest.mark.parametrize('claims', [{}, {'email': 'forged@example.invalid'},
    {'email': 'synthetic@example.invalid', 'email_verified': 'false', 'token_use': 'id'},
    {'email': 'synthetic@example.invalid', 'email_verified': 'true', 'token_use': 'access'}])
def test_aws_refuses_missing_or_unverified_event_claims(client, monkeypatch, claims):
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.delenv('REVIEW_REVIEWER', raising=False)
    monkeypatch.delenv('REVIEW_API_TOKEN', raising=False)
    event = {'requestContext': {'authorizer': {'jwt': {'claims': claims}}}}
    with TestClient(with_lambda_event(api.create_app(), event)) as aws:
        response = aws.get('/runs', headers={'Authorization': f'Bearer {TOKEN}', 'X-Reviewer': 'forged'})
        assert response.status_code == 401 and response.json() == {'detail': 'Not authenticated'}
        assert aws.get('/health').json() == {'status': 'ok'}


def test_aws_headers_cannot_forge_authorizer_context(client, monkeypatch):
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.delenv('REVIEW_REVIEWER', raising=False)
    monkeypatch.delenv('REVIEW_API_TOKEN', raising=False)
    with TestClient(api.create_app()) as aws:
        response = aws.get('/runs', headers={'Authorization': f'Bearer {TOKEN}',
            'X-Reviewer': 'forged', 'X-Amzn-Oidc-Identity': 'forged',
            'aws.event': '{"requestContext":{"authorizer":{"jwt":{"claims":{"email":"forged"}}}}}'})
    assert response.status_code == 401
