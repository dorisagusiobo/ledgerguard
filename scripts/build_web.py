"""Build the Next.js standalone Lambda artifact without deployment or secrets."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from build_api import deterministic_zip

ROOT = Path(__file__).resolve().parents[1]


def build_in_linux():
    """Mac builds use an isolated Linux workspace; no host credentials are mounted."""
    if any((ROOT / 'web').glob('.env*')):
        raise SystemExit('Move web .env files out before packaging; use runtime configuration')
    dist = ROOT / 'dist'
    if dist.is_symlink():
        raise SystemExit('Refusing a symlinked dist directory')
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='ledgerguard-web-') as directory:
        workspace = Path(directory)
        web = workspace / 'web'
        web.mkdir()
        for name in ('package.json', 'package-lock.json', 'tsconfig.json', 'next-env.d.ts',
                     'next.config.ts', 'proxy.ts', 'eslint.config.mjs', 'vitest.config.ts'):
            shutil.copy2(ROOT / 'web' / name, web / name)
        for name in ('app', 'lib', 'components', 'tests', 'public'):
            if (ROOT / 'web' / name).exists():
                shutil.copytree(ROOT / 'web' / name, web / name,
                                ignore=shutil.ignore_patterns('.env*', '*.tsbuildinfo'))
        (workspace / 'scripts').mkdir()
        for name in ('build_api.py', 'build_web.py'):
            shutil.copy2(ROOT / 'scripts' / name, workspace / 'scripts' / name)
        subprocess.run(['docker', 'run', '--rm', '--platform', 'linux/amd64',
                        '--mount', f'type=bind,source={workspace},target=/workspace',
                        '--workdir', '/workspace', os.environ.get('WEB_BUILD_IMAGE', 'node:24-bookworm@sha256:e7add2a490bb17d97b485b8641bd88455fff7839ade0f5eba493c3508124d413'),
                        'python3', 'scripts/build_web.py'], check=True)
        shutil.copy2(workspace / 'dist/web.zip', dist / 'web.zip')
    print('Copied Linux x86_64 artifact to dist/web.zip')


def build():
    web = ROOT / 'web'
    # Next loads .env files automatically: packaging must never embed local secrets.
    if any(web.glob('.env*')):
        raise SystemExit('Move web .env files out before packaging; use runtime configuration')
    dist = ROOT / 'dist'
    if dist.is_symlink():
        raise SystemExit('Refusing a symlinked dist directory')
    dist.mkdir(exist_ok=True)
    subprocess.run(['npm', 'ci'], cwd=web, check=True)
    subprocess.run(['npm', 'run', 'build'], cwd=web, check=True,
                   env={**os.environ, 'NEXT_OUTPUT': 'standalone', 'NEXT_TELEMETRY_DISABLED': '1'})
    package = dist / 'web-package'
    if package.exists():
        shutil.rmtree(package)
    shutil.copytree(web / '.next/standalone', package)
    shutil.copytree(web / '.next/static', package / '.next/static', dirs_exist_ok=True)
    if (web / 'public').is_dir():
        shutil.copytree(web / 'public', package / 'public', dirs_exist_ok=True)
    if not (package / 'server.js').is_file():
        raise SystemExit('Standalone server.js missing at artifact root')
    launcher = package / 'run.sh'
    launcher.write_text('#!/bin/sh\nexec node server.js\n')
    launcher.chmod(0o755)
    files = [p for p in package.rglob('*') if p.is_file()]
    if any(p.name.startswith('.env') or p.suffix == '.dylib' for p in files):
        raise SystemExit('Secret configuration or non-Linux library in web artifact')
    for path in files:
        if path.suffix == '.node':
            with path.open('rb') as native:
                header = native.read(20)
            if header[:5] != b'\x7fELF\x02' or header[5:6] != b'\x01' or header[18:20] != b'\x3e\x00':
                raise SystemExit('Native web libraries must target Linux x86_64; build on Linux')
    if sum(p.stat().st_size for p in files) > 250 * 1024 * 1024:
        raise SystemExit('Web package exceeds Lambda unzipped size limit')
    output = dist / 'web.zip'
    deterministic_zip(package, output)
    if output.stat().st_size > 45 * 1024 * 1024:
        output.unlink()
        raise SystemExit('Web ZIP exceeds 45 MB limit')
    print('Built dist/web.zip (Node.js 24, x86_64)')


if __name__ == '__main__':
    if sys.platform == 'linux':
        build()
    else:
        build_in_linux()
