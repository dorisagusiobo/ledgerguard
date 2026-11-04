"""Deployment safety tests: no AWS, Neon, provider installation or paid calls."""
import json

import boto3
import psycopg
import pytest

import ledgerguard.api as api
import ledgerguard.db as db


@pytest.mark.parametrize('name', ['REVIEW_REVIEWER', 'REVIEW_API_TOKEN'])
@pytest.mark.parametrize('value', ['', 'PRIVATE-LOCAL-SETTING'])
def test_aws_startup_refuses_local_shortcuts(monkeypatch, name, value):
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.delenv('REVIEW_REVIEWER', raising=False)
    monkeypatch.delenv('REVIEW_API_TOKEN', raising=False)
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match='Local review settings are forbidden') as caught:
        api.create_app()
    assert 'PRIVATE' not in str(caught.value)


def test_invalid_environment_is_not_a_local_fallback(monkeypatch):
    monkeypatch.setenv('LEDGERGUARD_ENV', 'mistyped')
    with pytest.raises(RuntimeError, match='LEDGERGUARD_ENV'):
        api.create_app()


def test_pooled_connections_disable_prepared_statements(monkeypatch):
    calls = []
    connection = object()
    def connect(url, **kwargs):
        calls.append(kwargs)
        return connection
    monkeypatch.setattr(psycopg, 'connect', connect)
    assert db.pooled_connection('synthetic-test-config') is connection
    assert calls == [{'autocommit': True, 'connect_timeout': 5, 'prepare_threshold': None}]


def test_migrations_refuse_pooler_endpoint_before_connecting(monkeypatch):
    monkeypatch.setattr(psycopg, 'connect', lambda *a, **k: pytest.fail('Must not connect'))
    with pytest.raises(OSError, match='direct endpoint'):
        db.direct_connection('postgresql://synthetic@ep-synthetic-pooler.neon.tech/synthetic')


def test_runtime_parameter_fetch_is_private_and_cached(monkeypatch):
    db._parameter_value.cache_clear()
    calls = []
    class SSM:
        def get_parameter(self, **kwargs):
            calls.append(kwargs)
            return {'Parameter': {'Type': 'SecureString', 'Value': 'postgresql://synthetic@synthetic.neon.tech/synthetic?sslmode=require'}}
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.setenv('APP_DATABASE_PARAMETER', '/ledgerguard/test/app-database-url')
    monkeypatch.setattr(boto3, 'client', lambda service: SSM() if service == 'ssm' else pytest.fail('Unexpected service'))
    try:
        assert db.runtime_database_url() == db.runtime_database_url() == 'postgresql://synthetic@synthetic.neon.tech/synthetic?sslmode=require'
        assert calls == [{'Name': '/ledgerguard/test/app-database-url', 'WithDecryption': True}]
    finally:
        db._parameter_value.cache_clear()


def test_parameter_errors_never_echo_connection_data(monkeypatch):
    from botocore.exceptions import ClientError
    db._parameter_value.cache_clear()
    class SSM:
        def get_parameter(self, **kwargs):
            raise ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'PRIVATE-CONNECTION'}}, 'GetParameter')
    monkeypatch.setattr(boto3, 'client', lambda *a, **k: SSM())
    with pytest.raises(OSError) as caught:
        db.parameter_value('/ledgerguard/test/missing')
    assert 'PRIVATE' not in str(caught.value)


def lambda_event(path, claims=None):
    event = {'version': '2.0', 'routeKey': 'GET ' + path, 'rawPath': path, 'rawQueryString': '',
             'headers': {'host': 'synthetic.execute-api.us-east-1.amazonaws.com'},
             'requestContext': {'requestId': 'synthetic', 'stage': '$default',
                 'http': {'method': 'GET', 'path': path, 'protocol': 'HTTP/1.1',
                          'sourceIp': '127.0.0.1', 'userAgent': 'synthetic-test'}},
             'isBase64Encoded': False}
    if claims is not None:
        event['requestContext']['authorizer'] = {'jwt': {'claims': claims}}
    return event


def test_handler_uses_real_adapter_for_http_api_event():
    from mangum import Mangum
    assert isinstance(api.handler, Mangum)
    response = api.handler(lambda_event('/health'), object())
    assert response['statusCode'] == 200
    assert json.loads(response['body']) == {'status': 'ok'}


def test_real_adapter_carries_gateway_claims_to_aws_app(monkeypatch):
    from mangum import Mangum
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.delenv('REVIEW_API_TOKEN', raising=False)
    monkeypatch.delenv('REVIEW_REVIEWER', raising=False)
    app = api.create_app()
    observed = []
    async def capture(request, call_next):
        observed.append(request.scope['aws.event']['requestContext']['authorizer']['jwt']['claims']['email'])
        return await call_next(request)
    app.middleware('http')(capture)
    adapter = Mangum(app, lifespan='off')
    event = lambda_event('/runs', {'email': 'synthetic@example.invalid', 'email_verified': 'true', 'token_use': 'id'})
    # A valid identity reaches the DB boundary; a synthetic DB outage is 503.
    monkeypatch.setattr(api, 'runtime_database_url', lambda: (_ for _ in ()).throw(OSError('synthetic outage')))
    response = adapter(event, object())
    assert response['statusCode'] == 503
    assert observed == ['synthetic@example.invalid']
    event['requestContext']['authorizer']['jwt']['claims'] = {}
    observed.clear()
    # Remove the capture middleware for the missing-claim case.
    assert Mangum(api.create_app(), lifespan='off')(event, object())['statusCode'] == 401


def test_lambda_packaging_uses_locked_linux_wheels_without_secrets(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from zipfile import ZipFile
    spec = importlib.util.spec_from_file_location('build_api_test', Path(__file__).resolve().parents[1] / 'scripts/build_api.py')
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.setattr(builder, 'ROOT', tmp_path)
    (tmp_path / 'pyproject.toml').write_text('[project]\ndependencies=["mangum==0.22.0"]\n')
    source = tmp_path / 'src/ledgerguard'
    source.mkdir(parents=True)
    (source / 'api.py').write_text('def handler(event, context): return {}\n')
    (source / 'pipeline_lambda.py').write_text('def handler(event, context): return {}\n')
    monkeypatch.setattr(builder, 'git_revision', lambda: 'a' * 40 + '-dirty')
    # Deliberately outside src/: neither should ever enter the ZIP.
    (tmp_path / 'not-for-package.txt').write_text('synthetic private fixture')
    (tmp_path / 'synthetic.pdf').write_bytes(b'%PDF synthetic')
    commands = []
    def run(command, **kwargs):
        commands.append(command)
        if command[1] == 'export':
            Path(command[command.index('--output-file') + 1]).write_text('synthetic locked export\n')
        else:
            package = Path(command[command.index('--target') + 1])
            (package / 'mangum').mkdir()
            (package / 'mangum/__init__.py').write_text('# synthetic adapter wheel\n')
        assert kwargs['check'] is True
    monkeypatch.setattr(builder.subprocess, 'run', run)
    builder.build()
    assert '--no-dev' in commands[0] and '--locked' in commands[0]
    assert commands[1][commands[1].index('--python-platform') + 1] == 'x86_64-manylinux_2_28'
    assert commands[1][commands[1].index('--python-version') + 1] == '3.12'
    assert '--require-hashes' in commands[1]
    with ZipFile(tmp_path / 'dist/api.zip') as archive:
        assert set(archive.namelist()) == {'ledgerguard/api.py', 'ledgerguard/pipeline_lambda.py',
                                         'ledgerguard/build_revision.txt', 'mangum/__init__.py'}
        assert archive.read('ledgerguard/build_revision.txt') == ('a' * 40 + '-dirty\n').encode()


@pytest.mark.parametrize('sslmode', ['require', 'verify-ca', 'verify-full'])
def test_aws_database_requires_explicit_tls(monkeypatch, sslmode):
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.setenv('APP_DATABASE_PARAMETER', '/ledgerguard/test/app')
    url = f'postgresql://synthetic@synthetic.neon.tech/synthetic?sslmode={sslmode}'
    monkeypatch.setattr(db, 'parameter_value', lambda name: url)
    assert db.runtime_database_url() == url


@pytest.mark.parametrize('suffix', ['', '?sslmode=disable', '?sslmode=allow', '?sslmode=prefer', '?sslmode=invalid'])
def test_aws_database_refuses_missing_or_weak_tls_without_leaking_url(monkeypatch, suffix):
    monkeypatch.setenv('LEDGERGUARD_ENV', 'aws')
    monkeypatch.setenv('APP_DATABASE_PARAMETER', '/ledgerguard/test/app')
    monkeypatch.setattr(db, 'parameter_value', lambda name: 'postgresql://synthetic:PRIVATE@synthetic.neon.tech/synthetic' + suffix)
    with pytest.raises(OSError) as caught:
        db.runtime_database_url()
    assert 'PRIVATE' not in str(caught.value) and 'postgresql' not in str(caught.value)


def test_migrate_pooler_refusal_has_no_traceback_or_url(monkeypatch, capsys):
    monkeypatch.setenv('DATABASE_URL', 'postgresql://synthetic:PRIVATE@ep-synthetic-pooler.neon.tech/synthetic')
    with pytest.raises(SystemExit) as caught:
        db.main(['migrate'])
    assert caught.value.code == 1
    error = capsys.readouterr().err
    assert 'Traceback' not in error and 'PRIVATE' not in error and 'postgresql' not in error
