import {afterEach, describe, it, expect, vi} from 'vitest';
import {getFinding, postDecision} from '@/lib/api';
import {detail} from './fixture';
afterEach(() => {vi.unstubAllEnvs(); vi.unstubAllGlobals();});
describe('server API boundary', () => {
  it('adds the server token and disables caching', async () => {
    vi.stubEnv('REVIEW_API_TOKEN', 'synthetic-private-token');
    vi.stubEnv('REVIEW_API_URL', 'http://127.0.0.1:8000');
    const fetcher = vi.fn<(url: URL, options: RequestInit) => Promise<Response>>(async () => Response.json(detail));
    vi.stubGlobal('fetch', fetcher);
    expect(await getFinding('12')).toEqual(detail);
    expect(fetcher.mock.calls[0]).toEqual([new URL('http://127.0.0.1:8000/findings/12'), expect.objectContaining({
      cache: 'no-store', redirect: 'error', headers: {Authorization: 'Bearer synthetic-private-token'}, method: 'GET'})]);
  });
  it.each([422, 503])('exposes only the API %i message', async status => {
    vi.stubEnv('REVIEW_API_TOKEN', 'synthetic-private-token');
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({detail: status === 503 ? 'Service unavailable' : [{loc: ['body', 'note'], msg: 'Note is required'}]}, {status})));
    await expect(postDecision('12', 'approve', 'Synthetic note')).rejects.toThrow(status === 503 ? 'Service unavailable' : 'Note is required');
  });
  it('rejects external API origins before attaching credentials', async () => {
    vi.stubEnv('REVIEW_API_TOKEN', 'synthetic-private-token');
    vi.stubEnv('REVIEW_API_URL', 'https://example.com');
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    await expect(getFinding('12')).rejects.toThrow('loopback');
    expect(fetcher).not.toHaveBeenCalled();
  });
});
