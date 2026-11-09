import Link from 'next/link';
import type {FindingType, Status} from '@/lib/types';
export const label = (value: string) => value.replaceAll('_', ' ');
// Plain-language meaning of each finding type, matching the checks in rules.py and policy.py.
export const findingTypes: Record<FindingType, {name: string; meaning: string}> = {
  duplicate: {name: 'Duplicate', meaning: 'Another invoice has the same vendor, date, purchase order and line items, so one purchase may be billed twice.'},
  contract_overcharge: {name: 'Contract overcharge', meaning: 'A line item is priced above the rate in the vendor’s contract.'},
  policy_violation: {name: 'Policy violation', meaning: 'The invoice breaks a clause of the spending policy. The rule section shows which one.'},
  split_purchase: {name: 'Split purchase', meaning: 'Invoices from one vendor within a few days each stay under the approval limit but go over it together.'},
  unusual_amount: {name: 'Unusual amount', meaning: 'The total is far above this vendor’s highest monthly spend on record. An unusual amount alone is not fraud.'},
  arithmetic_error: {name: 'Arithmetic error', meaning: 'The invoice doesn’t add up: a line amount, the subtotal or the total doesn’t match its parts.'},
  extraction_issue: {name: 'Extraction issue', meaning: 'The data read from the PDF may not match the document. Check it against the source text.'},
};
export function Badge({status}: {status: string}) {return <span className={`badge ${status}`}>{label(status)}</span>;}
export function When({value}: {value: string}) {return <time dateTime={value}>{new Intl.DateTimeFormat('en', {dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC'}).format(new Date(value))} UTC</time>;}
export function ReviewError({message}: {message: string}) {return <div className="empty-state" role="alert"><span className="eyebrow">Unable to load</span><h1>Review data is unavailable</h1><p>{message}</p><Link href="/" className="text-link">Return to runs →</Link></div>;}
export function safeMessage(error: unknown) {return error instanceof Error && (error.name === 'Error' && error.message.startsWith('Review API response does not match') || 'status' in error) ? error.message : 'Please check the local API and try again.';}
export const tabs: {name: string; status?: Status}[] = [{name: 'Pending', status: 'pending'}, {name: 'Approved', status: 'approved'}, {name: 'Rejected', status: 'rejected'}, {name: 'All'}];
