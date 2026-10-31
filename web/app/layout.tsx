import Link from 'next/link';
import type {Metadata} from 'next';
import './globals.css';
export const metadata: Metadata = {title: 'LedgerGuard · Review', description: 'Evidence-backed invoice review. Synthetic data only.'};
export const dynamic = 'force-dynamic';
export default function Layout({children}: {children: React.ReactNode}) {
  return <html lang="en"><body><a className="skip-link" href="#main">Skip to content</a><header className="app-header"><Link href="/" className="brand"><span className="brand-mark" aria-hidden="true">L<span>G</span></span>LedgerGuard</Link><div className="header-divider" /><span className="workspace-label">Review workspace</span><span className="local-label"><span aria-hidden="true">●</span> Local only <i>/</i> Synthetic data</span></header><main id="main">{children}</main><footer className="app-footer"><span>LedgerGuard / Human review, supported by evidence.</span><span>Synthetic invoices only · Version 1</span></footer></body></html>;
}
