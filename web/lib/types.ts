export type Status = 'pending' | 'approved' | 'rejected';
export type FindingType = 'duplicate' | 'contract_overcharge' | 'policy_violation' | 'split_purchase' | 'unusual_amount' | 'arithmetic_error' | 'extraction_issue';
export type SourceRef = {page: number; text: string};
export type Citation = {document: string; version: string; clause_id: string; quote: string};
export type Note = {finding_index: number; summary: string; related_invoices: string[]; uncertainty: 'low' | 'medium' | 'high'};
export type Finding = {
  finding_id: number; run_id: string; position: number; type: FindingType; invoice_numbers: string[];
  amount_at_issue: string; calculation: string; evidence: SourceRef[]; citation: Citation | null;
  no_clause_reason: 'no applicable clause identified' | null; confidence: number; status: Status; notes: Note[];
};
export type Run = {
  run_id: string; started_at: string; finished_at: string;
  status: 'completed' | 'needs_review' | 'stopped' | 'failed'; stopped_reason: string | null; skipped_stages: string[];
  model_ids: {extract: string; policy: string; investigate: string}; findings: Record<Status, number>;
};
export type LineItem = {position: number; description: string; quantity: string; unit_price: string; amount: string; source: SourceRef};
export type Invoice = {document_id: string; invoice_number: string; vendor_name: string; po_number: string | null;
  invoice_date: string; due_date: string; currency: string; subtotal: string; tax: string; total: string; line_items: LineItem[]};
export type Decision = {decision_id: number; decision: 'approve' | 'reject'; reviewer: string; note: string; time: string};
export type FindingDetail = {finding: Finding; invoices: Invoice[]; decisions: Decision[]};
export type DecisionCreated = Decision & {finding_id: number; status: Status};
export type RunsResponse = {runs: Run[]};
export type FindingsResponse = {run_id: string; findings: Finding[]};
