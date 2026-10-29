"""Offline checks using deliberately invalid identifiers, never real financial data."""
import importlib.util
from pathlib import Path
import subprocess

spec = importlib.util.spec_from_file_location(
    'hooks', Path(__file__).resolve().parents[1] / '.claude/hooks/checks.py')
hooks = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hooks)


def test_placeholders_and_invoice_amounts_allowed():
    assert not hooks.suspected_financial_data('TEST-CARD-001 TEST-BANK-001 USD 1234.56')


def test_synthetic_label_does_not_bypass_guard():
    assert hooks.suspected_financial_data('synthetic SSN: ' + '-'.join(['000', '00', '0000']))


def test_labeled_account_blocked():
    assert hooks.suspected_financial_data('bank account: ' + '0' * 8)


def test_card_checksum_detector():
    # All zeros are invalid financial data but exercise the checksum path.
    assert hooks.suspected_financial_data('0' * 16)
    assert not hooks.luhn('0' * 15 + '1')


def test_edit_checks_entire_result(tmp_path):
    path = tmp_path / 'fixture.txt'
    path.write_text('bank account: OLD')
    event = {'tool_name': 'Edit', 'tool_input': {
        'file_path': str(path), 'old_string': 'OLD', 'new_string': '0' * 8}}
    assert hooks.guard(event) == 2
    assert path.read_text() == 'bank account: OLD'


def test_sensitive_path_blocked_without_reading():
    assert hooks.guard({'tool_name': 'Edit', 'tool_input': {'file_path': '/missing/.env'}}) == 2


def test_denial_does_not_echo_identifier(capsys):
    value = '-'.join(['000', '00', '0000'])
    assert hooks.guard({'tool_name': 'Write', 'tool_input': {
        'file_path': 'fake.txt', 'content': value}}) == 2
    assert value not in capsys.readouterr().err


def test_pytest_result_states():
    for code, expected in [(0, 0), (5, 0), (1, 2), (3, 2)]:
        message, status = hooks.test_feedback(subprocess.CompletedProcess([], code, '', ''))
        assert status == expected
        if code == 5:
            assert 'no tests' in message


def test_non_python_edit_does_not_run_tests(monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError('must not run pytest for Markdown')
    monkeypatch.setattr(hooks.subprocess, 'run', unexpected)
    assert hooks.after_edit({'tool_input': {'file_path': 'README.md'}}) == 0


def test_purchase_orders_and_account_id_allowed():
    for text in ['PO20261029000001', 'PO-2026-0001', 'account_id = 1042']:
        assert not hooks.suspected_financial_data(text)
    for label in ['account number', 'account no']:
        assert hooks.suspected_financial_data(label + ': ' + '0' * 8)


def test_iban_checksum_and_guard():
    # Fabricated ZZ country code: exercises checksum without a real account.
    body = 'TEST' + '0' * 10
    numeric = ''.join(str(ord(c) - 55) if c.isalpha() else c
                      for c in body + 'ZZ00')
    candidate = 'ZZ' + f'{98 - int(numeric) % 97:02d}' + body
    assert hooks.iban_checksum(candidate)
    assert hooks.suspected_financial_data(candidate)
    spaced = ' '.join(candidate[i:i + 4] for i in range(0, len(candidate), 4))
    assert hooks.iban_checksum(spaced)
    assert hooks.suspected_financial_data(spaced)
    damaged = candidate[:2] + '00' + candidate[4:]
    assert not hooks.iban_checksum(damaged)
    assert not hooks.suspected_financial_data(damaged)
    assert not hooks.iban_checksum('PO20261029000001')


def test_configured_guard_without_project_venv(tmp_path):
    import json
    import os
    import shutil

    # Execute the actual configured command in a fresh-clone-shaped directory.
    root = Path(__file__).resolve().parents[1]
    target = tmp_path / 'fresh clone'
    (target / '.claude/hooks').mkdir(parents=True)
    shutil.copy(root / '.claude/hooks/checks.py', target / '.claude/hooks/checks.py')
    settings = json.loads((root / '.claude/settings.json').read_text())
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(target))
    event = {'tool_name': 'Write', 'tool_input': {
        'file_path': 'fake.txt', 'content': 'bank account: ' + '0' * 8}}
    command = settings['hooks']['PreToolUse'][0]['hooks'][0]['command']
    result = subprocess.run(command, shell=True, env=env, input=json.dumps(event),
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'suspected financial identifier' in result.stderr
    command = settings['hooks']['PostToolUse'][0]['hooks'][0]['command']
    event['tool_input']['file_path'] = 'example.py'
    result = subprocess.run(command, shell=True, env=env, input=json.dumps(event),
                            capture_output=True, text=True)
    assert result.returncode == 2
    assert 'run uv sync' in result.stderr
