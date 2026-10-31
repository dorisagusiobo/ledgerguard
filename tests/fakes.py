"""Offline routing fake; all truth is preloaded before invoking the pipeline."""
from copy import deepcopy
import json


def response(name, payload, *, outputs=100):
    return {'stopReason': 'tool_use', 'usage': {'inputTokens': 100, 'outputTokens': outputs},
            'output': {'message': {'role': 'assistant', 'content': [{'toolUse': {
                'toolUseId': 'fake-use', 'name': name, 'input': payload}}]}}}


class FakeClient:
    def __init__(self, pdfs, invoices, answers, *, fail=None, repeat=None, expensive=None, tool=False):
        self.records = {pdfs[i.invoice_number]: i.model_dump(mode='json') for i in invoices}
        semantic = {n for a in answers if a['clause_id'] == 'POL-4' for n in a['invoice_numbers']}
        self.flagged = {i.line_items[0].description for i in invoices if i.invoice_number in semantic}
        self.calls = []
        self.fail, self.repeat, self.expensive, self.tool = fail, repeat, expensive, tool

    def converse(self, **request):
        content = request['messages'][0]['content']
        if 'document' in content[0]:
            record = deepcopy(self.records[content[0]['document']['source']['bytes']])
            number = record['invoice_number']
            self.calls.append(('extraction', number, request['modelId']))
            if number == self.fail:
                return {'stopReason': 'end_turn', 'usage': {'inputTokens': 100, 'outputTokens': 10}}
            if self.repeat and number == self.repeat[0]:
                record['invoice_number'] = self.repeat[1]
            return response('record_invoice', record)
        payload = json.loads(content[0]['text'])
        names = {t['toolSpec']['name'] for t in request['toolConfig']['tools']}
        if 'record_policy_findings' in names:
            self.calls.append(('policy', None, request['modelId']))
            return response('record_policy_findings', {'violations': [
                {'clause_id': 'POL-4', 'line_index': i['index'], 'confidence': 1.0}
                for i in payload['line_items'] if i['description'] in self.flagged]})
        assert 'submit_notes' in names
        number = payload['invoice']['invoice_number']
        self.calls.append(('investigation', number, request['modelId']))
        if (self.tool or number == self.expensive) and len(request['messages']) == 1:
            return response('related_invoices', {
                'vendor_name': payload['invoice']['vendor_name'],
                'start': payload['invoice']['invoice_date'], 'end': payload['invoice']['invoice_date']},
                outputs=32000 if number == self.expensive else 100)
        return response('submit_notes', {'notes': [
            {'finding_index': f['index'], 'summary': f"Evidence: {f['calculation']}. Clearly supported.",
             'related_invoices': f['invoice_numbers'], 'uncertainty': 'low'} for f in payload['findings']]})

