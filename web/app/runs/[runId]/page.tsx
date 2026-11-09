import Link from 'next/link';
import {notFound} from 'next/navigation';
import {ApiError, getFindings} from '@/lib/api';
import {isStatus} from '@/lib/contracts';
import {Badge, findingTypes, ReviewError, safeMessage, tabs} from '@/components/common';
export default async function Queue({params, searchParams}: {params: Promise<{runId: string}>; searchParams: Promise<{status?: string}>}) {
  const {runId} = await params;
  const requested = (await searchParams).status ?? 'pending';
  if (requested !== 'all' && !isStatus(requested)) return <ReviewError message="Choose Pending, Approved, Rejected or All." />;
  let data;
  try {data = await getFindings(runId, requested === 'all' ? undefined : requested);} catch (error) {
    if (error instanceof ApiError && error.status === 404) notFound();
    return <ReviewError message={safeMessage(error)} />;
  }
  return <><nav className="breadcrumb" aria-label="Breadcrumb"><Link href="/">Runs</Link><span>/</span><span>Review queue</span></nav><header className="page-heading"><div><p className="eyebrow">Run {runId.slice(0, 12)}</p><h1>Review queue</h1><p className="muted">Focus on the evidence. Record a decision for each finding.</p></div><span className="count-tag">{data.findings.length} shown</span></header><p className="run-id small muted">Run ID <code>{runId}</code></p><nav className="tabs" aria-label="Finding status">{tabs.map(tab => <Link key={tab.name} href={`/runs/${encodeURIComponent(runId)}${tab.status === 'pending' ? '' : `?status=${tab.status ?? 'all'}`}`} aria-current={requested === (tab.status ?? 'all') ? 'page' : undefined}>{tab.name}</Link>)}</nav>{data.findings.length === 0 ? <div className="empty-state"><h2>No {requested === 'all' ? '' : requested} findings.</h2><p>{requested === 'pending' ? 'There is nothing waiting for a decision in this run.' : 'Choose another status to continue reviewing.'}</p></div> : <div className="table-scroll"><table className="queue-table"><thead><tr><th>Finding</th><th>Invoices</th><th>Amount at issue</th><th>Confidence</th><th>Status</th><th><span className="sr-only">Review</span></th></tr></thead><tbody>{data.findings.map(finding => <tr key={finding.finding_id}><td className="finding-cell"><Link href={`/findings/${finding.finding_id}`} className={`finding-link type-${finding.type}`}>{findingTypes[finding.type].name}</Link><p className="type-meaning">{findingTypes[finding.type].meaning}</p><div className="small muted">Finding {finding.finding_id}</div></td><td className="invoice-numbers">{finding.invoice_numbers.join(', ')}</td><td className="numeric amount">{finding.amount_at_issue}</td><td className="numeric">{(finding.confidence * 100).toFixed(0)}%</td><td><Badge status={finding.status} /></td><td><Link className="arrow-link" href={`/findings/${finding.finding_id}`} aria-label={`Review finding ${finding.finding_id}`}>↗</Link></td></tr>)}</tbody></table></div>}</>;
}
