// Scan maintained source, not installed packages or generated output.
import {readdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {describe, it, expect} from 'vitest';
function files(directory: string): string[] {
  return readdirSync(directory, {withFileTypes: true}).flatMap(entry =>
    ['node_modules', '.next', '.git', 'coverage'].includes(entry.name) ? [] : entry.isDirectory() ? files(join(directory, entry.name)) : [join(directory, entry.name)]);
}
describe('source safety', () => {
  it('keeps auth tokens on the server and uses configured callback origins', () => {
    const browserStorage = new RegExp(['local', 'Storage'].join('') + '|' + ['session', 'Storage'].join(''));
    for (const directory of ['app', 'lib', 'components']) for (const path of files(join(process.cwd(), directory))) {
      const source = readFileSync(path, 'utf8');
      expect(browserStorage.test(source), path).toBe(false);
      expect(source.includes(['NEXT', 'PUBLIC', 'COGNITO'].join('_')), path).toBe(false);
      if (path.endsWith('/lib/auth.ts') || path.includes('/app/auth/')) {
        expect(/headers\.get\(['"]host['"]\)/i.test(source), path).toBe(false);
      }
    }
  });
  it('forbids raw HTML, money parsing, public tokens, and client API access', () => {
    const forbidden = [['dangerously', 'SetInnerHTML'].join(''), ['parse', 'Float'].join('')];
    const publicPrefix = ['NEXT', 'PUBLIC', ''].join('_');
    for (const path of files(process.cwd()).filter(path => !path.endsWith('package-lock.json') && !path.endsWith('.tsbuildinfo'))) {
      const source = readFileSync(path, 'utf8');
      for (const word of forbidden) expect(source.includes(word), path).toBe(false);
      expect(source.includes(publicPrefix) && /token/i.test(source), path).toBe(false);
      expect(/\bNumber\s*\(/.test(source), path).toBe(false);
      expect(/next\/font|fonts\.google|src=["']https?:\/\//i.test(source), path).toBe(false);
      if (path.endsWith('.css')) expect(/url\(["']?https?:\/\//i.test(source), path).toBe(false);
      if (/^['"]use client['"]/.test(source.trimStart())) {
        expect(/REVIEW_API_TOKEN|REVIEW_API_URL|\bfetch\s*\(|from ['"].*lib\/api/.test(source), path).toBe(false);
      }
    }
  });
});
