import type {FindingDetail} from '@/lib/types';
export const detail: FindingDetail = {
  finding: {finding_id: 12, run_id: 'synthetic-run', position: 0, type: 'contract_overcharge', invoice_numbers: ['INV-SYN-1'],
    amount_at_issue: '900719925474.09', calculation: 'Synthetic price difference × 3 = 900719925474.09',
    evidence: [{page: 2, text: 'Synthetic item | 3 | 12.3456 | 37.04'}],
    citation: {document: 'contracts.json', version: 'synthetic-v1', clause_id: 'CON-1', quote: 'Synthetic contract rate is 12.3456.'},
    no_clause_reason: null, confidence: 0.9, status: 'pending',
    notes: [{finding_index: 0, summary: '<script>alert("vendor text")</script>', related_invoices: ['INV-SYN-1'], uncertainty: 'low'}]},
  invoices: [{document_id: 'synthetic-document', invoice_number: 'INV-SYN-1', vendor_name: 'Synthetic Vendor', po_number: 'PO-SYN',
    invoice_date: '2026-10-31', due_date: '2026-11-30', currency: 'USD', subtotal: '37.04', tax: '0.00', total: '37.04',
    line_items: [{position: 0, description: '<img src=x onerror=alert(1)>', quantity: '3', unit_price: '12.3456', amount: '37.04',
      source: {page: 2, text: 'Synthetic item | 3 | 12.3456 | 37.04'}}]}],
  decisions: [{decision_id: 1, reviewer: 'synthetic-reviewer', decision: 'reject', note: 'Synthetic prior decision.', time: '2026-10-31T12:00:00Z'}],
};
