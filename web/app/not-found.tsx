import Link from 'next/link';
export default function NotFound() {return <div className="empty-state"><p className="eyebrow">Not found</p><h1>This review isn’t available.</h1><p>The finding or run may not exist.</p><Link className="text-link" href="/">Return to runs →</Link></div>;}
