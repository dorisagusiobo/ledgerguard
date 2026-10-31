import {describe, it, expect} from 'vitest';
import {parseDetail, parseRuns, parseFindings, parseDecision} from '@/lib/contracts';
import {detail} from './fixture';
describe('API runtime contracts', () => {
  it('accepts the contract without changing money', () => {
    expect(parseDetail(detail)).toEqual(detail);
    expect(parseDetail(detail).finding.amount_at_issue).toBe('900719925474.09');
    expect(parseFindings({run_id: 'synthetic-run', findings: [detail.finding]}).findings).toHaveLength(1);
    expect(parseDecision({...detail.decisions[0], finding_id: 12, status: 'approved'}).finding_id).toBe(12);
  });
  it.each([
    {...detail, finding: {...detail.finding, amount_at_issue: 260}},
    {...detail, invoices: [{...detail.invoices[0], line_items: [{...detail.invoices[0].line_items[0], unit_price: '12.34'}]}]},
    {...detail, finding: {...detail.finding, notes: [{summary: 'Missing fields'}]}},
    {finding: detail.finding, invoices: []},
  ])('rejects malformed response fields clearly', bad => {
    expect(() => parseDetail(bad)).toThrow('Review API response does not match the finding detail contract');
  });
  it('validates run models and review counts', () => {
    const run = {run_id: 'r', started_at: '2026-10-31T12:00:00Z', finished_at: '2026-10-31T12:01:00Z', status: 'completed', stopped_reason: null,
      skipped_stages: [], model_ids: {extract: 'nova', policy: 'nova', investigate: 'sonnet'}, findings: {pending: 2, approved: 0, rejected: 0}};
    expect(parseRuns({runs: [run]}).runs).toHaveLength(1);
    expect(() => parseRuns({runs: [{...run, findings: {pending: 1}}]})).toThrow('runs contract');
  });
});
