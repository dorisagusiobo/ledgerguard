import {NextRequest, NextResponse} from 'next/server';

export function proxy(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString('base64');
  const dev = process.env.NODE_ENV === 'development';
  const csp = ["default-src 'self'", `script-src 'self' 'nonce-${nonce}'${dev ? " 'unsafe-eval'" : ''}`,
    dev ? "style-src 'self' 'unsafe-inline'" : `style-src 'self' 'nonce-${nonce}'`,
    "connect-src 'self'" + (dev ? ' ws://127.0.0.1:3000' : ''), "img-src 'self'",
    "font-src 'self'", "object-src 'none'", "base-uri 'self'", "form-action 'self'", "frame-ancestors 'none'"].join('; ');
  const headers = new Headers(request.headers);
  headers.set('Content-Security-Policy', csp);
  headers.set('x-nonce', nonce);
  const response = NextResponse.next({request: {headers}});
  response.headers.set('Content-Security-Policy', csp);
  return response;
}
export const config = {matcher: ['/((?!_next/static|_next/image|favicon.ico).*)']};
