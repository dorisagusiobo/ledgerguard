import Link from 'next/link';
export default function SignedOut() {return <><h1>Signed out</h1><Link href="/auth/login">Sign in again</Link></>;}
