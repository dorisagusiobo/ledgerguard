"""Scripted graph tests: no network, model SDK, or changes to findings."""
from copy import deepcopy
import json

import pytest
from pydantic import ValidationError

from ledgerguard.gateway import Budget, Gateway, GatewayBlocked, MODEL_ID
from ledgerguard.investigator import Investigation, Note, investigate
from ledgerguard.models import Finding, Invoice
from ledgerguard.tools import ReadOnlyTools


@pytest.fixture
def context():
    first = Invoice(invoice_number='INV-2026-0001', vendor_name='Synthetic Vendor',
                    invoice_date='2026-01-01', due_date='2026-01-31', po_number='PO-2026-0001',
                    currency='USD', subtotal='100.00', tax='0.00', total='100.00', line_items=[
                        {'description': 'PRIVATE-LINE-DESCRIPTION', 'quantity': '1', 'unit_price': '100.00',
                         'amount': '100.00', 'source': {'page': 1, 'text': 'PRIVATE-SOURCE-TEXT'}}])
    second = first.model_copy(update={'invoice_number': 'INV-2026-0001-A'}, deep=True)
    finding = Finding(type='duplicate', invoice_numbers=[first.invoice_number, second.invoice_number],
                      amount_at_issue='100.00', calculation='Duplicate invoice total 100.00',
                      evidence=[first.line_items[0].source],
                      no_clause_reason='no applicable clause identified', confidence=1)
    return first, second, [finding], ReadOnlyTools([first, second], [])


def call(name, arguments, id='USE-001'):
    return {'stopReason': 'tool_use', 'usage': {'inputTokens': 100, 'outputTokens': 100},
            'output': {'message': {'role': 'assistant', 'content': [
                {'toolUse': {'toolUseId': id, 'name': name, 'input': arguments}}]}}}


def note(**changes):
    return {'finding_index': 0, 'summary': 'The matching record supports the existing finding.',
            'related_invoices': [], 'uncertainty': 'medium', **changes}


def submit(notes=None):
    return call('submit_notes', {'notes': [note()] if notes is None else notes}, 'SUBMIT-001')


class ScriptedClient:
    def __init__(self, replies, error=False):
        self.replies, self.calls, self.error = replies, [], error

    def converse(self, **request):
        self.calls.append(deepcopy(request))
        if self.error:
            raise RuntimeError('PRIVATE-PROVIDER-TEXT')
        return deepcopy(self.replies[min(len(self.calls) - 1, len(self.replies) - 1)])


def setup(tmp_path, replies, *, budget=None, error=False):
    client = ScriptedClient(replies, error=error)
    gateway = Gateway(client=client, budget=budget or Budget(), run_id='INVESTIGATE-001',
                      audit_path=tmp_path / 'audit.jsonl')
    return client, gateway


def run(context, gateway, **kwargs):
    first, second, findings, tools = context
    return investigate(first, findings, tools=tools, gateway=gateway, model_id=MODEL_ID, **kwargs)


def assert_review(result):
    assert result.notes is None and result.needs_review
    assert 'PRIVATE' not in result.needs_review.reason


def test_normal_tools_then_notes_keeps_findings_unchanged(context, tmp_path):
    first, second, findings, tools = context
    before = deepcopy((first, findings))
    client, gateway = setup(tmp_path, [call('duplicate_search', {'invoice_number': first.invoice_number}),
                                      submit([note(related_invoices=[second.invoice_number])])])
    result = run(context, gateway)
    assert result.needs_review is None
    assert result.notes[0].related_invoices == [second.invoice_number]
    assert (first, findings) == before
    assert len(client.calls) == 2 and gateway.budget.tool_calls == 1
    for request in client.calls:
        encoded = json.dumps(request)
        assert 'PRIVATE-LINE-DESCRIPTION' not in encoded and 'PRIVATE-SOURCE-TEXT' not in encoded
        assert request['toolConfig']['toolChoice'] == {'any': {}}
        assert {t['toolSpec']['name'] for t in request['toolConfig']['tools']} == {
            'duplicate_search', 'related_invoices', 'vendor_history', 'submit_notes'}
    records = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert len(records) == 4  # Two model calls, tool authorization and completion.
    tool_records = [r for r in records if 'tool' in r]
    assert [r['phase'] for r in tool_records] == ['authorization', 'completion']
    assert tool_records[0]['call_id'] == tool_records[1]['call_id']
    assert tool_records[1]['row_count'] == 1
    assert 'PRIVATE' not in (tmp_path / 'audit.jsonl').read_text()


def test_made_up_related_invoice_is_rejected(context, tmp_path):
    number = 'INV-MADE-UP'
    client, gateway = setup(tmp_path, [submit([note(related_invoices=[number])])])
    assert_review(run(context, gateway))
    assert len(client.calls) == 1


@pytest.mark.parametrize('number', ['INV-2026-0001', 'INV-2026-0001-A'])
def test_supplied_invoice_or_finding_reference_needs_no_tool(context, tmp_path, number):
    client, gateway = setup(tmp_path, [submit([note(related_invoices=[number])])])
    result = run(context, gateway)
    assert result.needs_review is None
    assert result.notes[0].related_invoices == [number]
    assert len(client.calls) == 1 and gateway.budget.tool_calls == 0


def test_real_but_unseen_invoice_is_rejected(context, tmp_path):
    unrelated = context[0].model_copy(update={'invoice_number': 'INV-2026-0099'}, deep=True)
    tools = ReadOnlyTools([context[0], context[1], unrelated], [])
    client, gateway = setup(tmp_path, [submit([note(related_invoices=[unrelated.invoice_number])])])
    result = investigate(context[0], context[2], tools=tools, gateway=gateway, model_id=MODEL_ID)
    assert_review(result)
    assert len(client.calls) == 1 and gateway.budget.tool_calls == 0


@pytest.mark.parametrize('notes', [[], [note(), note()], [note(finding_index=1)],
                                   [note(summary='x' * 601)], [note(summary='   ')],
                                   [note(uncertainty='certain')], [note(extra='PRIVATE')]])
def test_invalid_notes_become_safe_referral(context, tmp_path, notes):
    client, gateway = setup(tmp_path, [submit(notes)])
    assert_review(run(context, gateway))
    assert len(client.calls) == 1


def test_repeated_index_with_correct_count_is_rejected(context, tmp_path):
    context[2].append(context[2][0].model_copy(deep=True))
    client, gateway = setup(tmp_path, [submit([note(), note()])])
    assert_review(run(context, gateway))


@pytest.mark.parametrize('tool,args', [('unknown_tool', {}),
                                      ('duplicate_search', {'invoice_number': 'INV-2026-0001', 'extra': True})])
def test_rejected_tool_can_be_corrected(context, tmp_path, tool, args):
    client, gateway = setup(tmp_path, [call(tool, args), submit()])
    result = run(context, gateway)
    assert result.needs_review is None and result.notes
    error = client.calls[1]['messages'][-1]['content'][0]['toolResult']
    assert error == {'toolUseId': 'USE-001', 'status': 'error', 'content': [{'text': 'Tool call was rejected.'}]}
    assert gateway.budget.tool_calls == 1


def test_tool_cap_becomes_referral(context, tmp_path):
    client, gateway = setup(tmp_path, [call('duplicate_search', {'invoice_number': context[0].invoice_number})],
                            budget=Budget(max_tool_calls=0))
    assert_review(run(context, gateway))
    assert len(client.calls) == 1 and gateway.budget.tool_calls == 1


def test_turn_cap_exact_call_count(context, tmp_path):
    client, gateway = setup(tmp_path, [call('vendor_history', {'vendor_name': context[0].vendor_name})])
    result = run(context, gateway, max_turns=6)
    assert_review(result)
    assert result.needs_review.reason == 'investigation stopped: turn limit'
    assert len(client.calls) == 6
    assert gateway.budget.tool_calls == 5


def test_submit_on_final_turn_is_allowed(context, tmp_path):
    client, gateway = setup(tmp_path, [submit()])
    assert run(context, gateway, max_turns=1).notes
    assert len(client.calls) == 1


def test_budget_block_propagates(context, tmp_path):
    client, gateway = setup(tmp_path, [submit()], budget=Budget(max_calls=0))
    with pytest.raises(GatewayBlocked):
        run(context, gateway)
    assert not client.calls


def test_client_error_is_review(context, tmp_path):
    client, gateway = setup(tmp_path, [submit()], error=True)
    assert_review(run(context, gateway))
    assert len(client.calls) == 1


def test_audit_file_error_stops_graph(context, tmp_path):
    client, gateway = setup(tmp_path, [submit()])
    (tmp_path / 'audit.jsonl').mkdir()
    with pytest.raises(OSError):
        run(context, gateway)
    assert not client.calls


@pytest.mark.parametrize('response', [
    {'stopReason': 'max_tokens'},
    {'stopReason': 'tool_use', 'output': {'message': {'content': [{'text': 'PRIVATE-MODEL-TEXT'}]}}},
    {'stopReason': 'tool_use', 'output': None},
])
def test_malformed_response_is_not_retried(context, tmp_path, response):
    response['usage'] = {'inputTokens': 100, 'outputTokens': 100}
    client, gateway = setup(tmp_path, [response])
    assert_review(run(context, gateway))
    assert len(client.calls) == 1


def test_calculation_copy_redacts_line_text_without_changing_finding(context, tmp_path):
    context[2][0].calculation = 'PRIVATE-LINE-DESCRIPTION: (100.00 - 90.00) × 1; PRIVATE-SOURCE-TEXT'
    before = deepcopy(context[2])
    client, gateway = setup(tmp_path, [submit()])
    assert run(context, gateway).notes
    request = json.dumps(client.calls[0])
    assert 'PRIVATE-LINE-DESCRIPTION' not in request and 'PRIVATE-SOURCE-TEXT' not in request
    assert '[redacted]' in request
    assert context[2] == before


def test_tool_error_is_review_and_is_logged(context, tmp_path):
    class BrokenTools:
        def vendor_history(self, **arguments):
            raise RuntimeError('PRIVATE-TOOL-ERROR')

    client, gateway = setup(tmp_path, [call('vendor_history', {'vendor_name': 'Synthetic Vendor'})])
    result = investigate(context[0], context[2], tools=BrokenTools(), gateway=gateway, model_id=MODEL_ID)
    assert_review(result)
    records = [json.loads(line) for line in (tmp_path / 'audit.jsonl').read_text().splitlines()]
    assert records[-1]['outcome'] == 'tool_error'
    assert 'PRIVATE' not in (tmp_path / 'audit.jsonl').read_text()


def test_no_findings_needs_no_model(context, tmp_path):
    client, gateway = setup(tmp_path, [submit()])
    assert investigate(context[0], [], tools=context[3], gateway=gateway, model_id=MODEL_ID).notes == []
    assert not client.calls


def test_note_and_investigation_validation():
    with pytest.raises(ValidationError):
        Note.model_validate(note(finding_index=True))
    with pytest.raises(ValueError):
        Investigation()
    with pytest.raises(ValueError):
        Investigation(notes=[], needs_review=object())
