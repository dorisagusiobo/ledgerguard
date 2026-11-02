// @vitest-environment node
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import {NextRequest} from 'next/server';
const injected = vi.hoisted(() => ({verify: vi.fn()}));
vi.mock('@/lib/auth', async importOriginal => {
  const original = await importOriginal<typeof import('@/lib/auth')>();
  return {...original, verifyIdToken: (token: string, nonce?: string) => original.verifyIdToken(token, nonce, injected)};
});
import {GET as login} from '@/app/auth/login/route';
import {GET as callback} from '@/app/auth/callback/route';
import {POST as logout} from '@/app/auth/logout/route';
import * as logoutRoute from '@/app/auth/logout/route';
import {GET as health} from '@/app/auth/health/route';
import {proxy} from '@/proxy';
import {ID_COOKIE, OAUTH_COOKIE, randomValue} from '@/lib/auth';

beforeEach(() => {
  vi.stubEnv('LEDGERGUARD_ENV', 'aws'); vi.stubEnv('REVIEW_API_TOKEN', undefined);
  vi.stubEnv('APP_ORIGIN', 'https://web.example.invalid'); vi.stubEnv('REVIEW_API_URL', 'https://api.example.invalid');
  vi.stubEnv('COGNITO_DOMAIN', 'https://login.example.invalid'); vi.stubEnv('COGNITO_USER_POOL_ID', 'us-east-1_Synthetic');
  vi.stubEnv('COGNITO_CLIENT_ID', 'syntheticclient');
  injected.verify.mockReset();
  injected.verify.mockResolvedValue({iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Synthetic',
    aud: 'syntheticclient', token_use: 'id', exp: Math.floor(Date.now() / 1000) + 3600,
    email_verified: true, email: 'reviewer@example.invalid'});
});
afterEach(() => {vi.unstubAllEnvs(); vi.unstubAllGlobals();});
function transaction() {return {state: randomValue(), nonce: randomValue(), verifier: randomValue(), createdAt: Date.now()};}
function callbackRequest(oauth: ReturnType<typeof transaction>, query = `state=${oauth.state}&code=synthetic-code`, cookie = true) {
  const request = new NextRequest(`https://web.example.invalid/auth/callback?${query}`);
  if (cookie) request.cookies.set(OAUTH_COOKIE, JSON.stringify(oauth));
  return request;
}
describe('OAuth routes', () => {
  it('uses configured origin despite a forged Host and sets the oauth cookie attributes', async () => {
    // The login handler accepts no request argument; neither Host nor forwarded headers can affect it.
    const request = new NextRequest('https://evil.example.invalid/auth/login', {headers: {host: 'evil.example.invalid'}});
    expect((await proxy(request)).status).toBe(200);
    const response = await login();
    expect(response.status).toBe(302);
    const url = new URL(response.headers.get('location')!);
    expect(url.origin).toBe('https://login.example.invalid');
    expect(url.searchParams.get('redirect_uri')).toBe('https://web.example.invalid/auth/callback');
    expect(url.searchParams.get('scope')).toBe('openid email');
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
    const cookie = response.cookies.get(OAUTH_COOKIE)!;
    expect(cookie).toMatchObject({httpOnly: true, secure: true, sameSite: 'lax', path: '/', maxAge: 600});
    expect(JSON.parse(cookie.value).verifier).toMatch(/^[\w-]{43}$/);
    expect(response.headers.get('cache-control')).toBe('no-store');
  });
  it.each(['missing-cookie', 'wrong-state', 'provider-error', 'exchange-error', 'wrong-nonce', 'unverified-email', 'access-token', 'wrong-audience', 'expired'])('rejects %s without leaking error details', async failure => {
    const oauth = transaction();
    const payload = {iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Synthetic', aud: 'syntheticclient', token_use: 'id',
      exp: Math.floor(Date.now() / 1000) + 3600, email_verified: true, email: 'reviewer@example.invalid', nonce: oauth.nonce};
    injected.verify.mockResolvedValue({...payload, ...(failure === 'wrong-nonce' ? {nonce: 'wrong'} : {}),
      ...(failure === 'unverified-email' ? {email_verified: false} : {}), ...(failure === 'access-token' ? {token_use: 'access'} : {}),
      ...(failure === 'wrong-audience' ? {aud: 'wrong'} : {}), ...(failure === 'expired' ? {exp: 1} : {})});
    vi.stubGlobal('fetch', vi.fn(async () => failure === 'exchange-error' ? new Response('PRIVATE provider error', {status: 400}) : Response.json({id_token: 'synthetic-id'})));
    const query = failure === 'provider-error' ? 'error=PRIVATE' : `state=${failure === 'wrong-state' ? 'wrong' : oauth.state}&code=synthetic-code`;
    const response = await callback(callbackRequest(oauth, query, failure !== 'missing-cookie'));
    expect(response.status).toBe(400); expect(await response.text()).toBe('Sign-in failed');
    expect(response.cookies.get(ID_COOKIE)?.maxAge).toBe(0); expect(response.cookies.get(OAUTH_COOKIE)?.maxAge).toBe(0);
  });
  it('exchanges PKCE, sets the ID cookie to token expiry and clears oauth state', async () => {
    const oauth = transaction();
    const exp = Math.floor(Date.now() / 1000) + 1800;
    injected.verify.mockResolvedValue({iss: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Synthetic', aud: 'syntheticclient',
      token_use: 'id', exp, email_verified: true, email: 'reviewer@example.invalid', nonce: oauth.nonce});
    const fetcher = vi.fn(async () => Response.json({id_token: 'synthetic-id', refresh_token: 'discarded'}));
    vi.stubGlobal('fetch', fetcher);
    const response = await callback(callbackRequest(oauth));
    expect(response.status).toBe(302); expect(response.headers.get('location')).toBe('https://web.example.invalid/');
    expect(response.cookies.get(ID_COOKIE)).toMatchObject({value: 'synthetic-id', httpOnly: true, secure: true, sameSite: 'lax', path: '/'});
    expect(response.cookies.get(ID_COOKIE)!.maxAge).toBeGreaterThanOrEqual(1799);
    expect(response.cookies.get(ID_COOKIE)!.maxAge).toBeLessThanOrEqual(1800);
    expect(response.cookies.get(OAUTH_COOKIE)!.maxAge).toBe(0);
    const options = (fetcher.mock.calls as unknown as [URL, RequestInit][])[0][1];
    expect(options.redirect).toBe('error'); expect(options.signal).toBeInstanceOf(AbortSignal);
    expect((options.body as URLSearchParams).get('code_verifier')).toBe(oauth.verifier);
    expect((options.body as URLSearchParams).get('redirect_uri')).toBe('https://web.example.invalid/auth/callback');
  });
  it('allows only same-origin POST logout', async () => {
    expect('GET' in logoutRoute).toBe(false);
    expect((await logout(new NextRequest('https://web.example.invalid/auth/logout', {method: 'POST', headers: {origin: 'https://foreign.example.invalid'}}))).status).toBe(403);
    const response = await logout(new NextRequest('https://web.example.invalid/auth/logout', {method: 'POST', headers: {origin: 'https://web.example.invalid'}}));
    expect(response.status).toBe(303); expect(response.cookies.get(ID_COOKIE)!.maxAge).toBe(0);
    const url = new URL(response.headers.get('location')!);
    expect(url.origin).toBe('https://login.example.invalid'); expect(url.pathname).toBe('/logout');
    expect(url.searchParams.get('logout_uri')).toBe('https://web.example.invalid/signed-out');
  });
  it.each([
    [{origin: 'null', 'sec-fetch-site': 'same-origin'}, 303],
    [{origin: 'null', 'sec-fetch-site': 'cross-site'}, 403],
    [{'sec-fetch-site': 'cross-site'}, 403],
    [{origin: 'null'}, 403],
  ])('checks browser logout provenance %j', async (headers, status) => {
    const response = await logout(new NextRequest('https://web.example.invalid/auth/logout', {method: 'POST', headers}));
    expect(response.status).toBe(status);
    if (status === 303) expect(response.cookies.get(ID_COOKIE)!.maxAge).toBe(0);
  });
});
describe('proxy boundary', () => {
  it('redirects absent and invalid cookies, but returns 401 to POST', async () => {
    expect((await proxy(new NextRequest('https://web.example.invalid/'))).status).toBe(302);
    const request = new NextRequest('https://web.example.invalid/'); request.cookies.set(ID_COOKIE, 'bad');
    injected.verify.mockRejectedValue(new Error('Synthetic invalid signature'));
    expect((await proxy(request)).status).toBe(302);
    expect((await proxy(new NextRequest('https://web.example.invalid/', {method: 'POST'}))).status).toBe(401);
  });
  it('lets action POSTs reach their session check and preserves the plain POST boundary', async () => {
    const action = new NextRequest('https://web.example.invalid/', {method: 'POST', headers: {'next-action': 'synthetic-action'}});
    expect((await proxy(action)).headers.get('x-middleware-next')).toBe('1');
    action.cookies.set(ID_COOKIE, 'expired');
    injected.verify.mockRejectedValue(new Error('Synthetic expired session'));
    expect((await proxy(action)).headers.get('x-middleware-next')).toBe('1');
    expect(injected.verify).not.toHaveBeenCalled();
    expect((await proxy(new NextRequest('https://web.example.invalid/', {method: 'POST'}))).status).toBe(401);
    expect((await proxy(new NextRequest('https://web.example.invalid/', {headers: {'next-action': 'synthetic-action'}}))).status).toBe(302);
  });
  it('passes health without auth/config and adds no-store', async () => {
    vi.stubEnv('COGNITO_CLIENT_ID', undefined);
    expect((await proxy(new NextRequest('https://web.example.invalid/auth/health'))).status).toBe(200);
    expect(health().status).toBe(200); expect(await health().text()).toBe('ok');
    expect(health().headers.get('cache-control')).toBe('no-store');
    expect((await proxy(new NextRequest('https://web.example.invalid/'))).status).toBe(503);
  });
  it('includes only the configured Cognito origin in form-action', async () => {
    const response = await proxy(new NextRequest('https://web.example.invalid/auth/login'));
    expect(response.headers.get('Content-Security-Policy')).toContain("form-action 'self' https://login.example.invalid;");
    expect(response.headers.get('Cache-Control')).toBe('no-store');
  });
});
