import type {Citation, Decision, DecisionCreated, Finding, FindingDetail, FindingsResponse, Invoice, LineItem, Note, Run, RunsResponse, SourceRef, Status} from './types';

type ObjectValue = Record<string, unknown>;
const object = (v: unknown): v is ObjectValue => typeof v === 'object' && v !== null && !Array.isArray(v);
const text = (v: unknown): v is string => typeof v === 'string';
const nullableText = (v: unknown) => v === null || text(v);
const count = (v: unknown): v is number => typeof v === 'number' && Number.isSafeInteger(v) && v >= 0;
const id = (v: unknown): v is number => count(v) && v > 0;
const strings = (v: unknown): v is string[] => Array.isArray(v) && v.every(text);
const money = (v: unknown) => text(v) && /^\d+\.\d{2}$/.test(v);
const price = (v: unknown) => text(v) && /^\d+\.\d{4}$/.test(v);
const day = (v: unknown) => text(v) && /^\d{4}-\d{2}-\d{2}$/.test(v);
const time = (v: unknown) => text(v) && /(Z|\+00:00)$/.test(v) && !Number.isNaN(Date.parse(v));
export const isStatus = (v: unknown): v is Status => ['pending', 'approved', 'rejected'].includes(v as string);
const list = <T>(v: unknown, check: (item: unknown) => item is T): v is T[] => Array.isArray(v) && v.every(check);
const source = (v: unknown): v is SourceRef => object(v) && id(v.page) && text(v.text);
const citation = (v: unknown): v is Citation => object(v) && text(v.document) && text(v.version) && text(v.clause_id) && text(v.quote);
const note = (v: unknown): v is Note => object(v) && count(v.finding_index) && text(v.summary) && strings(v.related_invoices) && ['low', 'medium', 'high'].includes(v.uncertainty as string);
const finding = (v: unknown): v is Finding => object(v) && id(v.finding_id) && text(v.run_id) && count(v.position)
  && ['duplicate', 'contract_overcharge', 'policy_violation', 'split_purchase', 'unusual_amount', 'arithmetic_error', 'extraction_issue'].includes(v.type as string)
  && strings(v.invoice_numbers) && v.invoice_numbers.length > 0 && money(v.amount_at_issue) && text(v.calculation)
  && list(v.evidence, source) && v.evidence.length > 0 && (v.citation === null || citation(v.citation))
  && (v.no_clause_reason === null || v.no_clause_reason === 'no applicable clause identified')
  && (v.citation === null) !== (v.no_clause_reason === null)
  && typeof v.confidence === 'number' && v.confidence >= 0 && v.confidence <= 1 && isStatus(v.status) && list(v.notes, note);
const run = (v: unknown): v is Run => object(v) && text(v.run_id) && time(v.started_at) && time(v.finished_at)
  && ['completed', 'needs_review', 'stopped', 'failed'].includes(v.status as string) && nullableText(v.stopped_reason)
  && strings(v.skipped_stages) && object(v.model_ids) && ['extract', 'policy', 'investigate'].every(k => text((v.model_ids as ObjectValue)[k]))
  && object(v.findings) && ['pending', 'approved', 'rejected'].every(k => count((v.findings as ObjectValue)[k]));
const line = (v: unknown): v is LineItem => object(v) && count(v.position) && text(v.description) && text(v.quantity)
  && /^\d+(\.\d+)?$/.test(v.quantity) && price(v.unit_price) && money(v.amount) && source(v.source);
const invoice = (v: unknown): v is Invoice => object(v) && text(v.document_id) && text(v.invoice_number) && text(v.vendor_name)
  && nullableText(v.po_number) && day(v.invoice_date) && day(v.due_date) && text(v.currency)
  && money(v.subtotal) && money(v.tax) && money(v.total) && list(v.line_items, line);
const decision = (v: unknown): v is Decision => object(v) && id(v.decision_id) && ['approve', 'reject'].includes(v.decision as string)
  && text(v.reviewer) && text(v.note) && time(v.time);

function checked<T>(value: unknown, guard: (v: unknown) => v is T, name: string): T {
  if (!guard(value)) throw new Error(`Review API response does not match the ${name} contract. Please check the API version.`);
  return value;
}
export const parseRuns = (v: unknown) => checked<RunsResponse>(v, (v): v is RunsResponse => object(v) && list(v.runs, run), 'runs');
export const parseFindings = (v: unknown) => checked<FindingsResponse>(v, (v): v is FindingsResponse => object(v) && text(v.run_id) && list(v.findings, finding), 'findings');
export const parseDetail = (v: unknown) => checked<FindingDetail>(v, (v): v is FindingDetail => object(v) && finding(v.finding) && list(v.invoices, invoice) && list(v.decisions, decision), 'finding detail');
export const parseDecision = (v: unknown) => checked<DecisionCreated>(v, (v): v is DecisionCreated => object(v) && id(v.finding_id) && isStatus(v.status) && decision(v), 'decision');
