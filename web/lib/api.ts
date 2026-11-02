import 'server-only';
import {parseDecision, parseDetail, parseFindings, parseRuns} from './contracts';
import type {Status} from './types';
import {cookies} from 'next/headers';
import {authConfig, environment, ID_COOKIE, verifyIdToken} from './auth';

export class ApiError extends Error {
  constructor(public status: number, message: string) {super(message);}
}
async function request(path: string, body?: {decision: 'approve' | 'reject'; note: string}): Promise<unknown> {
  let token = process.env.REVIEW_API_TOKEN;
  const configured = process.env.REVIEW_API_URL || 'http://127.0.0.1:8000';
  let base: URL;
  let aws = false;
  try {aws = environment() === 'aws';} catch {throw new ApiError(503, 'The review API server configuration is invalid.');}
  try {base = new URL(configured);} catch {throw new ApiError(503, 'The review API server configuration is invalid.');}
  if (aws) {
    try {
      const config = authConfig();
      if (base.origin !== config.apiOrigin || configured !== config.apiOrigin) throw new Error('Invalid API origin');
    } catch {throw new ApiError(503, 'The review API server configuration is invalid.');}
    token = (await cookies()).get(ID_COOKIE)?.value;
    try {if (!token) throw new Error('Missing session'); await verifyIdToken(token);} catch {throw new ApiError(401, 'Your session has expired. Sign in again.');}
  } else if (base.protocol !== 'http:' || !['127.0.0.1', 'localhost', '[::1]'].includes(base.hostname) || base.username || base.password || base.pathname !== '/' || base.search || base.hash || !token) {
    throw new ApiError(503, 'Configure the loopback review API and its token on the Next.js server.');
  }
  let response: Response;
  try {
    response = await fetch(new URL(path, base), {method: body ? 'POST' : 'GET', cache: 'no-store', redirect: 'error',
      headers: {Authorization: `Bearer ${token}`, ...(body ? {'Content-Type': 'application/json'} : {})},
      body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(10000)});
  } catch {throw new ApiError(503, aws ? 'Review API unavailable. Please try again.' : 'Review API unavailable. Check that the local API is running.');}
  if (aws && response.status === 401) throw new ApiError(401, 'Your session has expired. Sign in again.');
  let data: unknown;
  try {data = await response.json();} catch {throw new ApiError(response.status, 'Review API returned an invalid response.');}
  if (!response.ok) {
    let message = response.status === 503 ? 'Service unavailable' : 'The review API request failed.';
    if (response.status === 422 && typeof data === 'object' && data !== null && 'detail' in data && Array.isArray(data.detail)) {
      const messages = data.detail.flatMap(item => typeof item === 'object' && item !== null && typeof item.msg === 'string' ? [item.msg] : []);
      if (messages.length) message = messages.join('; ');
    } else if (response.status === 401) message = 'Review API authentication failed. Check the server configuration.';
    else if (response.status === 404) message = 'Not found';
    throw new ApiError(response.status, message);
  }
  return data;
}
export const getRuns = async () => parseRuns(await request('/runs'));
export const getFindings = async (runId: string, status?: Status) => parseFindings(await request(`/runs/${encodeURIComponent(runId)}/findings${status ? `?status=${status}` : ''}`));
export const getFinding = async (findingId: string) => parseDetail(await request(`/findings/${encodeURIComponent(findingId)}`));
export const postDecision = async (findingId: string, decision: 'approve' | 'reject', note: string) => parseDecision(await request(`/findings/${encodeURIComponent(findingId)}/decisions`, {decision, note}));
