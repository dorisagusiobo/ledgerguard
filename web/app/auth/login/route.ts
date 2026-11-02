import {NextResponse} from 'next/server';
import {AuthConfigurationError, OAUTH_COOKIE, cookieOptions, loginUrl, randomValue, environment} from '@/lib/auth';

export async function GET() {
  try {
    if (environment() === 'local') return new NextResponse('Sign-in is unavailable in local mode.', {status: 404, headers: {'Cache-Control': 'no-store'}});
    const oauth = {state: randomValue(), nonce: randomValue(), verifier: randomValue(), createdAt: Date.now()};
    const response = NextResponse.redirect(loginUrl(oauth), 302);
    response.cookies.set(OAUTH_COOKIE, JSON.stringify(oauth), {...cookieOptions, maxAge: 600});
    response.headers.set('Cache-Control', 'no-store');
    return response;
  } catch (error) {
    return new NextResponse(error instanceof AuthConfigurationError ? error.message : 'Sign-in unavailable.', {status: 503, headers: {'Cache-Control': 'no-store'}});
  }
}
