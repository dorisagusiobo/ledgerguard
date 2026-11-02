// @vitest-environment node
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
const session = vi.hoisted(() => ({token: 'synthetic-id-token', valid: true}));
vi.mock('next/headers', () => ({cookies: async () => ({get: () => session.token ? {value: session.token} : undefined})}));
vi.mock('next/cache', () => ({revalidatePath: vi.fn()}));
vi.mock('next/navigation', () => ({redirect: vi.fn()}));
vi.mock('@/lib/auth', async importOriginal => ({...await importOriginal<typeof import('@/lib/auth')>(),
  verifyIdToken: async () => {if (!session.valid) throw new Error('Invalid token'); return {email: 'reviewer@example.invalid', exp: 9999999999};}}));
import {ApiError, getFinding} from '@/lib/api';
import {detail} from './fixture';
import {submitDecision} from '@/app/actions';
import {redirect} from 'next/navigation';
beforeEach(() => {
  session.token = 'synthetic-id-token'; session.valid = true;
  vi.stubEnv('LEDGERGUARD_ENV', 'aws'); vi.stubEnv('REVIEW_API_TOKEN', undefined);
  vi.stubEnv('APP_ORIGIN', 'https://web.example.invalid'); vi.stubEnv('REVIEW_API_URL', 'https://api.example.invalid');
  vi.stubEnv('COGNITO_DOMAIN', 'https://login.example.invalid'); vi.stubEnv('COGNITO_USER_POOL_ID', 'us-east-1_Synthetic');
  vi.stubEnv('COGNITO_CLIENT_ID', 'syntheticclient');
});
afterEach(() => {vi.unstubAllEnvs(); vi.unstubAllGlobals();});
describe('AWS API client', () => {
  it('sends only the verified session ID token to the configured HTTPS API', async () => {
    const fetcher = vi.fn(async () => Response.json(detail)); vi.stubGlobal('fetch', fetcher);
    expect(await getFinding('12')).toEqual(detail);
    expect(fetcher).toHaveBeenCalledWith(new URL('https://api.example.invalid/findings/12'), expect.objectContaining({
      headers: {Authorization: 'Bearer synthetic-id-token'}, cache: 'no-store', redirect: 'error'}));
  });
  it.each(['http://127.0.0.1:8000', 'https://api.example.invalid/path', 'https://user@api.example.invalid'])('refuses unsafe base %s before fetching', async base => {
    vi.stubEnv('REVIEW_API_URL', base); const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(getFinding('12')).rejects.toMatchObject({status: 503}); expect(fetcher).not.toHaveBeenCalled();
  });
  it('refuses the local token setting even when empty', async () => {
    vi.stubEnv('REVIEW_API_TOKEN', ''); const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(getFinding('12')).rejects.toMatchObject({status: 503}); expect(fetcher).not.toHaveBeenCalled();
  });
  it('turns even a non-JSON upstream 401 into the session-expired error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Unauthorized', {status: 401})));
    await expect(getFinding('12')).rejects.toEqual(new ApiError(401, 'Your session has expired. Sign in again.'));
  });
  it('returns session expiry as form state while preserving the note', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Unauthorized', {status: 401})));
    const form = new FormData(); form.set('note', 'My unfinished synthetic review note'); form.set('decision', 'reject');
    expect(await submitDecision('12', {message: ''}, form)).toEqual({message: 'Your session has expired. Sign in again.'});
    expect(form.get('note')).toBe('My unfinished synthetic review note'); expect(redirect).not.toHaveBeenCalled();
  });
  it('does not fetch for a missing or invalid session', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    session.token = ''; await expect(getFinding('12')).rejects.toMatchObject({status: 401});
    session.token = 'bad'; session.valid = false;
    await expect(getFinding('12')).rejects.toMatchObject({status: 401}); expect(fetcher).not.toHaveBeenCalled();
  });
});
