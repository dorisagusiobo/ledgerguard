import {NextRequest, NextResponse} from 'next/server';
import {authConfig, environment, ID_COOKIE, verifyIdToken} from './lib/auth';

export async function proxy(request: NextRequest) {
  const path = request.nextUrl.pathname;
  if (path === '/auth/health') {
    const response = NextResponse.next();
    response.headers.set('Cache-Control', 'no-store');
    return response;
  }
  let cognitoOrigin = '';
  try {
    if (environment() === 'aws') {
      const config = authConfig();
      cognitoOrigin = config.cognitoOrigin;
      // Actions return authentication errors as form state so draft notes survive.
      // The API client verifies the session before making any upstream request.
      const serverAction = request.method === 'POST' && request.headers.has('next-action');
      if (!path.startsWith('/auth/') && path !== '/signed-out' && !serverAction) {
        try {
          const token = request.cookies.get(ID_COOKIE)?.value;
          if (!token) throw new Error('Missing session');
          await verifyIdToken(token);
        } catch {
          const response = request.method === 'POST'
            ? new NextResponse('Your session has expired. Sign in again.', {status: 401})
            : NextResponse.redirect(new URL('/auth/login', config.appOrigin), 302);
          response.headers.set('Cache-Control', 'no-store');
          return response;
        }
      }
    }
  } catch {return new NextResponse('Sign-in configuration unavailable.', {status: 503, headers: {'Cache-Control': 'no-store'}});}
  const nonce = Buffer.from(crypto.randomUUID()).toString('base64');
  const dev = process.env.NODE_ENV === 'development';
  const csp = ["default-src 'self'", `script-src 'self' 'nonce-${nonce}'${dev ? " 'unsafe-eval'" : ''}`,
    dev ? "style-src 'self' 'unsafe-inline'" : `style-src 'self' 'nonce-${nonce}'`,
    "connect-src 'self'" + (dev ? ' ws://127.0.0.1:3000' : ''), "img-src 'self'",
    "font-src 'self'", "object-src 'none'", "base-uri 'self'", `form-action 'self'${cognitoOrigin ? ` ${cognitoOrigin}` : ''}`, "frame-ancestors 'none'"].join('; ');
  const headers = new Headers(request.headers);
  headers.set('Content-Security-Policy', csp);
  headers.set('x-nonce', nonce);
  const response = NextResponse.next({request: {headers}});
  response.headers.set('Content-Security-Policy', csp);
  if (path.startsWith('/auth/')) response.headers.set('Cache-Control', 'no-store');
  return response;
}
export const config = {matcher: ['/((?!_next/static|_next/image|favicon.ico).*)']};
