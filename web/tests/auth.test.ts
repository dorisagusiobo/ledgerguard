// @vitest-environment node
import {afterEach, beforeEach, describe, expect, it} from 'vitest';
import {generateKeyPairSync, sign} from 'node:crypto';
import {CognitoJwtVerifier} from 'aws-jwt-verify';
import {authConfig, equalState, parseOAuthCookie, pkceChallenge, randomValue, verifyIdToken} from '@/lib/auth';
import {vi} from 'vitest';

beforeEach(() => {
  vi.stubEnv('LEDGERGUARD_ENV', 'aws'); vi.stubEnv('REVIEW_API_TOKEN', undefined);
  vi.stubEnv('APP_ORIGIN', 'https://web.example.invalid'); vi.stubEnv('REVIEW_API_URL', 'https://api.example.invalid');
  vi.stubEnv('COGNITO_DOMAIN', 'https://login.example.invalid'); vi.stubEnv('COGNITO_USER_POOL_ID', 'us-east-1_Synthetic');
  vi.stubEnv('COGNITO_CLIENT_ID', 'syntheticclient');
});
afterEach(() => vi.unstubAllEnvs());
const {privateKey, publicKey} = generateKeyPairSync('rsa', {modulusLength: 2048});
const jwk = {...publicKey.export({format: 'jwk'}), kty: 'RSA', kid: 'synthetic-key', alg: 'RS256', use: 'sig'};
function signed(claims: Record<string, unknown>) {
  const head = Buffer.from(JSON.stringify({alg: 'RS256', kid: jwk.kid})).toString('base64url');
  const body = Buffer.from(JSON.stringify(claims)).toString('base64url');
  return `${head}.${body}.${sign('RSA-SHA256', Buffer.from(`${head}.${body}`), privateKey).toString('base64url')}`;
}
function claims() {return {iss: authConfig().issuer, aud: 'syntheticclient', token_use: 'id', exp: Math.floor(Date.now() / 1000) + 3600,
  email: 'reviewer@example.invalid', email_verified: true, nonce: 'synthetic-nonce', sub: 'synthetic-user', iat: Math.floor(Date.now() / 1000)};}
function verifier() {
  const result = CognitoJwtVerifier.create({userPoolId: 'us-east-1_Synthetic', clientId: 'syntheticclient', tokenUse: 'id'});
  result.cacheJwks({keys: [jwk]});
  return result;
}
describe('Cognito verification without network', () => {
  it('checks a real RSA signature with locally cached synthetic signing keys', async () => {
    expect(await verifyIdToken(signed(claims()), 'synthetic-nonce', verifier())).toMatchObject({email: 'reviewer@example.invalid'});
  });
  it.each([{aud: 'wrong'}, {iss: 'https://wrong.example.invalid'}, {token_use: 'access'}, {exp: 1},
    {email_verified: false}, {email_verified: 'true'}, {email: ''}, {nonce: 'wrong'}])('rejects wrong claims %j', async change => {
    await expect(verifyIdToken(signed({...claims(), ...change}), 'synthetic-nonce', verifier())).rejects.toThrow();
  });
  it('rejects a tampered signature', async () => {
    const token = signed(claims());
    const parts = token.split('.');
    parts[2] = (parts[2][0] === 'A' ? 'B' : 'A') + parts[2].slice(1);
    await expect(verifyIdToken(parts.join('.'), undefined, verifier())).rejects.toThrow();
  });
});
describe('auth configuration and PKCE', () => {
  it('matches RFC 7636 S256', () => {
    expect(pkceChallenge('dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk')).toBe('E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM');
    expect(randomValue()).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(equalState('abc', 'abc')).toBe(true); expect(equalState('abc', 'x')).toBe(false);
  });
  it.each(['APP_ORIGIN', 'REVIEW_API_URL', 'COGNITO_DOMAIN', 'COGNITO_USER_POOL_ID', 'COGNITO_CLIENT_ID'])('requires %s', key => {
    vi.stubEnv(key, undefined); expect(authConfig).toThrow('configuration');
  });
  it.each(['http://web.example.invalid', 'https://u:p@web.example.invalid', 'https://web.example.invalid/path',
    'https://web.example.invalid?x=1', 'https://web.example.invalid#x'])('rejects non-origin %s', value => {
    vi.stubEnv('APP_ORIGIN', value); expect(authConfig).toThrow('configuration');
  });
  it('refuses even an empty local token in aws mode', () => {
    vi.stubEnv('REVIEW_API_TOKEN', ''); expect(authConfig).toThrow('configuration');
  });
  it('rejects expired and malformed oauth cookies', () => {
    expect(() => parseOAuthCookie('{}')).toThrow();
    expect(() => parseOAuthCookie(JSON.stringify({state: randomValue(), nonce: randomValue(), verifier: randomValue(), createdAt: Date.now() - 601000}))).toThrow();
  });
});
