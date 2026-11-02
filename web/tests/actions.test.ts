import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
vi.mock('next/cache', () => ({revalidatePath: vi.fn()}));
vi.mock('next/navigation', () => ({redirect: vi.fn(() => {throw new Error('redirected');})}));
import {redirect} from 'next/navigation';
import {revalidatePath} from 'next/cache';
import {submitDecision} from '@/app/actions';
import {detail} from './fixture';
beforeEach(() => {vi.clearAllMocks(); vi.stubEnv('REVIEW_API_TOKEN', 'synthetic-server-token');});
afterEach(() => {vi.unstubAllGlobals(); vi.unstubAllEnvs();});
function form(note = '  Synthetic decision note  ') {
  const data = new FormData(); data.set('note', note); data.set('decision', 'reject'); return data;
}
describe('server decisions', () => {
  it.each([401, 422, 503])('returns the %i message without changing the submitted note', async status => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({detail: status === 503 ? 'Service unavailable' : [{loc: ['body', 'note'], msg: 'Note is required'}]}, {status})));
    const data = form();
    const state = await submitDecision('12', {message: ''}, data);
    expect(state.message).toBe(status === 503 ? 'Service unavailable' : status === 401 ? 'Review API authentication failed. Check the server configuration.' : 'Note is required');
    expect(data.get('note')).toBe('  Synthetic decision note  ');
    expect(redirect).not.toHaveBeenCalled();
    expect(revalidatePath).not.toHaveBeenCalled();
  });
  it('refreshes the queue and reloads the finding after a successful append', async () => {
    const fetcher = vi.fn(async () => Response.json({...detail.decisions[0], finding_id: 12, status: 'rejected'}));
    vi.stubGlobal('fetch', fetcher);
    await expect(submitDecision('12', {message: ''}, form())).rejects.toThrow('redirected');
    expect(revalidatePath).toHaveBeenCalledWith('/', 'layout');
    expect(redirect).toHaveBeenCalledWith('/findings/12');
  });
  it('rejects an invalid note before making an API request', async () => {
    const fetcher = vi.fn(); vi.stubGlobal('fetch', fetcher);
    expect((await submitDecision('12', {message: ''}, form('   '))).message).toContain('1–1000');
    expect(fetcher).not.toHaveBeenCalled();
  });
});
