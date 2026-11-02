import 'server-only';
import {createHash, randomBytes, timingSafeEqual} from 'node:crypto';
import {CognitoJwtVerifier} from 'aws-jwt-verify';

export const ID_COOKIE = '__Host-lg-id';
export const OAUTH_COOKIE = '__Host-lg-oauth';
export const cookieOptions = {httpOnly: true, secure: true, sameSite: 'lax' as const, path: '/'};
export class AuthConfigurationError extends Error {
  constructor() {super('Sign-in configuration unavailable.');}
}
export function environment(): 'local' | 'aws' {
  const mode = process.env.LEDGERGUARD_ENV ?? 'local';
  if (mode !== 'local' && mode !== 'aws') throw new AuthConfigurationError();
  return mode;
}
function httpsOrigin(value: string | undefined): string {
  if (!value) throw new AuthConfigurationError();
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash || !url.hostname || value !== url.origin) throw new AuthConfigurationError();
    return url.origin;
  } catch {throw new AuthConfigurationError();}
}
export function authConfig() {
  if (environment() !== 'aws' || process.env.REVIEW_API_TOKEN !== undefined) throw new AuthConfigurationError();
  const appOrigin = httpsOrigin(process.env.APP_ORIGIN);
  const apiOrigin = httpsOrigin(process.env.REVIEW_API_URL);
  const cognitoOrigin = httpsOrigin(process.env.COGNITO_DOMAIN);
  const poolId = process.env.COGNITO_USER_POOL_ID ?? '';
  const clientId = process.env.COGNITO_CLIENT_ID ?? '';
  if (!/^[a-z]{2}(?:-[a-z]+)+-\d_[A-Za-z0-9]+$/.test(poolId) || !/^[a-z0-9]{1,128}$/.test(clientId)) throw new AuthConfigurationError();
  const region = poolId.split('_')[0];
  return {appOrigin, apiOrigin, cognitoOrigin, poolId, clientId,
    issuer: `https://cognito-idp.${region}.amazonaws.com/${poolId}`, callback: `${appOrigin}/auth/callback`};
}
export function randomValue() {return randomBytes(32).toString('base64url');}
export function pkceChallenge(verifier: string) {return createHash('sha256').update(verifier).digest('base64url');}
export function equalState(actual: string, expected: string) {
  const a = Buffer.from(actual), b = Buffer.from(expected);
  return a.length === b.length && timingSafeEqual(a, b);
}
export type OAuthState = {state: string; nonce: string; verifier: string; createdAt: number};
export function parseOAuthCookie(raw: string | undefined): OAuthState {
  if (!raw) throw new Error('Invalid OAuth state');
  const value = JSON.parse(raw);
  if (!value || !['state', 'nonce', 'verifier'].every(key => typeof value[key] === 'string' && /^[A-Za-z0-9_-]{43}$/.test(value[key])) ||
      !Number.isSafeInteger(value.createdAt) || value.createdAt > Date.now() || Date.now() - value.createdAt > 600000) throw new Error('Invalid OAuth state');
  return value;
}
export function loginUrl(oauth: OAuthState) {
  const config = authConfig();
  const url = new URL('/oauth2/authorize', config.cognitoOrigin);
  url.search = new URLSearchParams({response_type: 'code', client_id: config.clientId,
    redirect_uri: config.callback, scope: 'openid email', state: oauth.state, nonce: oauth.nonce,
    code_challenge: pkceChallenge(oauth.verifier), code_challenge_method: 'S256'}).toString();
  return url;
}
export async function exchangeCode(code: string, verifier: string): Promise<string> {
  const config = authConfig();
  const response = await fetch(new URL('/oauth2/token', config.cognitoOrigin), {
    method: 'POST', cache: 'no-store', redirect: 'error', signal: AbortSignal.timeout(10000),
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams({grant_type: 'authorization_code', client_id: config.clientId,
      redirect_uri: config.callback, code, code_verifier: verifier}),
  });
  if (!response.ok) throw new Error('Token exchange failed');
  const tokens: unknown = await response.json();
  if (!tokens || typeof tokens !== 'object' || !('id_token' in tokens) || typeof tokens.id_token !== 'string') throw new Error('Missing ID token');
  // Access/refresh tokens are deliberately neither returned nor retained.
  return tokens.id_token;
}
export type TokenVerifier = {verify: (token: string) => Promise<Record<string, unknown>>};
let cached: {key: string; verifier: TokenVerifier} | undefined;
function defaultVerifier(config: ReturnType<typeof authConfig>): TokenVerifier {
  const key = `${config.poolId}/${config.clientId}`;
  if (cached?.key !== key) cached = {key, verifier: CognitoJwtVerifier.create({userPoolId: config.poolId, clientId: config.clientId, tokenUse: 'id'})};
  return cached.verifier;
}
export async function verifyIdToken(token: string, expectedNonce?: string, verifier?: TokenVerifier) {
  const config = authConfig();
  const claims = await (verifier ?? defaultVerifier(config)).verify(token);
  if (claims.iss !== config.issuer || claims.aud !== config.clientId || claims.token_use !== 'id' ||
      typeof claims.exp !== 'number' || !Number.isSafeInteger(claims.exp) || claims.exp <= Math.floor(Date.now() / 1000) ||
      claims.email_verified !== true || typeof claims.email !== 'string' || !claims.email.trim() ||
      (expectedNonce !== undefined && (typeof claims.nonce !== 'string' || !equalState(claims.nonce, expectedNonce)))) throw new Error('Invalid ID token');
  return {email: claims.email, exp: claims.exp};
}
