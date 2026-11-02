import {NextRequest, NextResponse} from 'next/server';
import {authConfig, cookieOptions, equalState, exchangeCode, ID_COOKIE, OAUTH_COOKIE, parseOAuthCookie, verifyIdToken} from '@/lib/auth';

export async function GET(request: NextRequest) {
  try {
    const config = authConfig();
    const params = request.nextUrl.searchParams;
    if (params.has('error') || params.getAll('state').length !== 1 || params.getAll('code').length !== 1) throw new Error('Invalid callback');
    const oauth = parseOAuthCookie(request.cookies.get(OAUTH_COOKIE)?.value);
    const state = params.get('state'), code = params.get('code');
    if (!state || !code || !equalState(state, oauth.state)) throw new Error('Invalid callback');
    const token = await exchangeCode(code, oauth.verifier);
    const claims = await verifyIdToken(token, oauth.nonce);
    const response = NextResponse.redirect(new URL('/', config.appOrigin), 302);
    response.cookies.set(ID_COOKIE, token, {...cookieOptions, maxAge: claims.exp - Math.floor(Date.now() / 1000)});
    response.cookies.set(OAUTH_COOKIE, '', {...cookieOptions, maxAge: 0});
    response.headers.set('Cache-Control', 'no-store');
    return response;
  } catch {
    const response = new NextResponse('Sign-in failed', {status: 400, headers: {'Cache-Control': 'no-store', 'Content-Type': 'text/plain; charset=utf-8'}});
    response.cookies.set(ID_COOKIE, '', {...cookieOptions, maxAge: 0});
    response.cookies.set(OAUTH_COOKIE, '', {...cookieOptions, maxAge: 0});
    return response;
  }
}
