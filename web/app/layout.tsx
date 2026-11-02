import Link from 'next/link';
import type {Metadata} from 'next';
import {cookies} from 'next/headers';
import {environment, ID_COOKIE, verifyIdToken} from '@/lib/auth';
import './globals.css';
export const metadata: Metadata = {title: 'LedgerGuard · Review', description: 'Evidence-backed invoice review. Synthetic data only.'};
export const dynamic = 'force-dynamic';
export default async function Layout({children}: {children: React.ReactNode}) {
  const aws = environment() === 'aws';
  let email: string | undefined;
  if (aws) {
    const token = (await cookies()).get(ID_COOKIE)?.value;
    if (token) {try {email = (await verifyIdToken(token)).email;} catch { /* Proxy and API enforce the session. */ }}
  }
  return <html lang="en"><body><a className="skip-link" href="#main">Skip to content</a><header className={`app-header${aws ? ' aws-header' : ''}`}><Link href="/" className="brand"><span className="brand-mark" aria-hidden="true">L<span>G</span></span>LedgerGuard</Link><div className="header-divider" /><span className="workspace-label">Review workspace</span><span className="local-label"><span aria-hidden="true">●</span> {aws ? 'AWS' : 'Local only'} <i>/</i> Synthetic data</span>{email && <div className="reviewer-session"><span>{email}</span><form action="/auth/logout" method="post"><button type="submit">Sign out</button></form></div>}</header><main id="main">{children}</main><footer className="app-footer"><span>LedgerGuard / Human review, supported by evidence.</span><span>Synthetic invoices only · Version 1</span></footer></body></html>;
}
