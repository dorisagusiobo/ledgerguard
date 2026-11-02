import {NextRequest, NextResponse} from 'next/server';
import {authConfig, cookieOptions, ID_COOKIE, OAUTH_COOKIE} from '@/lib/auth';

export async function POST(request: NextRequest) {
  let config: ReturnType<typeof authConfig>;
  try {config = authConfig();} catch {return new NextResponse('Sign-in configuration unavailable.', {status: 503, headers: {'Cache-Control': 'no-store'}});}
  if (request.headers.get('sec-fetch-site') !== 'same-origin' && request.headers.get('origin') !== config.appOrigin) return new NextResponse('Forbidden', {status: 403, headers: {'Cache-Control': 'no-store'}});
  const url = new URL('/logout', config.cognitoOrigin);
  url.search = new URLSearchParams({client_id: config.clientId, logout_uri: `${config.appOrigin}/signed-out`}).toString();
  const response = NextResponse.redirect(url, 303);
  response.cookies.set(ID_COOKIE, '', {...cookieOptions, maxAge: 0});
  response.cookies.set(OAUTH_COOKIE, '', {...cookieOptions, maxAge: 0});
  response.headers.set('Cache-Control', 'no-store');
  return response;
}
