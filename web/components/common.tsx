import Link from 'next/link';
import type {Status} from '@/lib/types';
export const label = (value: string) => value.replaceAll('_', ' ');
export function Badge({status}: {status: string}) {return <span className={`badge ${status}`}>{label(status)}</span>;}
export function When({value}: {value: string}) {return <time dateTime={value}>{new Intl.DateTimeFormat('en', {dateStyle: 'medium', timeStyle: 'short', timeZone: 'UTC'}).format(new Date(value))} UTC</time>;}
export function ReviewError({message}: {message: string}) {return <div className="empty-state" role="alert"><span className="eyebrow">Unable to load</span><h1>Review data is unavailable</h1><p>{message}</p><Link href="/" className="text-link">Return to runs →</Link></div>;}
export function safeMessage(error: unknown) {return error instanceof Error && (error.name === 'Error' && error.message.startsWith('Review API response does not match') || 'status' in error) ? error.message : 'Please check the local API and try again.';}
export const tabs: {name: string; status?: Status}[] = [{name: 'Pending', status: 'pending'}, {name: 'Approved', status: 'approved'}, {name: 'Rejected', status: 'rejected'}, {name: 'All'}];
