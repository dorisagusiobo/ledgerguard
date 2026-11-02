"""Verify deterministic packaging, entrypoint permissions and size enforcement."""
import importlib.util
from pathlib import Path
from zipfile import ZipFile

import pytest


def builder(monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / 'scripts'
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location('build_web_test', scripts / 'build_web.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_web_zip_layout_permissions_and_determinism(tmp_path, monkeypatch):
    module = builder(monkeypatch)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    web = tmp_path / 'web'
    (web / '.next/standalone').mkdir(parents=True)
    (web / '.next/standalone/server.js').write_text('// synthetic server')
    (web / '.next/static').mkdir()
    (web / '.next/static/app.js').write_text('// synthetic asset')
    commands = []
    monkeypatch.setattr(module.subprocess, 'run', lambda command, **kw: commands.append((command, kw)))
    module.build()
    output = tmp_path / 'dist/web.zip'
    before = output.read_bytes()
    with ZipFile(output) as archive:
        assert set(archive.namelist()) == {'server.js', '.next/static/app.js', 'run.sh'}
        assert archive.read('run.sh') == b'#!/bin/sh\nexec node server.js\n'
        assert (archive.getinfo('run.sh').external_attr >> 16) & 0o777 == 0o755
    assert commands[0][0] == ['npm', 'ci']
    assert commands[1][1]['env']['NEXT_OUTPUT'] == 'standalone'
    module.build()
    assert output.read_bytes() == before


def test_web_build_refuses_env_files_without_reading_them(tmp_path, monkeypatch):
    module = builder(monkeypatch)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    (tmp_path / 'web').mkdir()
    (tmp_path / 'web/.env.local').touch()
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: pytest.fail('Must not build'))
    with pytest.raises(SystemExit, match='runtime configuration'):
        module.build()


def test_oversized_web_zip_is_removed(tmp_path, monkeypatch):
    module = builder(monkeypatch)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    (tmp_path / 'web/.next/standalone').mkdir(parents=True)
    (tmp_path / 'web/.next/standalone/server.js').write_text('// synthetic')
    (tmp_path / 'web/.next/static').mkdir()
    monkeypatch.setattr(module.subprocess, 'run', lambda *a, **kw: None)
    def oversized(package, output):
        with output.open('wb') as stream:
            stream.truncate(46 * 1024 * 1024)
    monkeypatch.setattr(module, 'deterministic_zip', oversized)
    with pytest.raises(SystemExit, match='45 MB'):
        module.build()
    assert not (tmp_path / 'dist/web.zip').exists()


def test_mac_build_mounts_only_curated_sources(tmp_path, monkeypatch):
    module = builder(monkeypatch)
    monkeypatch.delenv('WEB_BUILD_IMAGE', raising=False)
    monkeypatch.setattr(module, 'ROOT', tmp_path)
    (tmp_path / 'web/app').mkdir(parents=True)
    (tmp_path / 'web/app/page.tsx').write_text('// synthetic page')
    for name in ('package.json', 'package-lock.json', 'tsconfig.json', 'next-env.d.ts',
                 'next.config.ts', 'proxy.ts', 'eslint.config.mjs', 'vitest.config.ts'):
        (tmp_path / 'web' / name).write_text('{}')
    (tmp_path / 'scripts').mkdir()
    for name in ('build_api.py', 'build_web.py'):
        (tmp_path / 'scripts' / name).write_text('# synthetic builder')
    (tmp_path / 'private-not-for-build.txt').write_text('synthetic private fixture')
    def run(command, **kwargs):
        assert command[:5] == ['docker', 'run', '--rm', '--platform', 'linux/amd64']
        assert '--env-file' not in command and '-e' not in command
        assert 'node:24-bookworm@sha256:e7add2a490bb17d97b485b8641bd88455fff7839ade0f5eba493c3508124d413' in command
        mount = command[command.index('--mount') + 1]
        workspace = Path(mount.split('source=')[1].split(',target=')[0])
        assert not (workspace / 'private-not-for-build.txt').exists()
        assert (workspace / 'web/app/page.tsx').exists()
        assert kwargs['check'] is True
        (workspace / 'dist').mkdir()
        (workspace / 'dist/web.zip').write_bytes(b'synthetic zip')
    monkeypatch.setattr(module.subprocess, 'run', run)
    module.build_in_linux()
    assert (tmp_path / 'dist/web.zip').read_bytes() == b'synthetic zip'
