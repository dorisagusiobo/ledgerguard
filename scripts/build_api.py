"""Build a Linux x86_64/Python 3.12 Lambda ZIP from locked runtime wheels only."""
from pathlib import Path
import shutil
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def deterministic_zip(package: Path, output: Path):
    """Stable paths, timestamps and Unix permissions for Lambda artifacts."""
    with ZipFile(output, 'w', compression=ZIP_DEFLATED) as archive:
        for path in sorted(p for p in package.rglob('*') if p.is_file()):
            info = ZipInfo(path.relative_to(package).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.compress_type = ZIP_DEFLATED
            info.external_attr = (0o100755 if path.stat().st_mode & 0o111 else 0o100644) << 16
            archive.writestr(info, path.read_bytes())


def build():
    import tomllib
    project = tomllib.loads((ROOT / 'pyproject.toml').read_text())
    if not any(item.split('=')[0].split('>')[0].split('<')[0].strip() == 'mangum'
               for item in project['project']['dependencies']):
        raise SystemExit("Mangum must be a locked runtime dependency before packaging")
    dist = ROOT / 'dist'
    if dist.is_symlink():
        raise SystemExit('Refusing a symlinked dist directory')
    dist.mkdir(exist_ok=True)
    package = dist / 'package'
    if package.exists():
        shutil.rmtree(package)
    package.mkdir()
    requirements = dist / 'requirements-api.txt'
    subprocess.run(['uv', 'export', '--locked', '--no-dev', '--no-emit-project', '--no-annotate',
                    '--format', 'requirements.txt', '--output-file', str(requirements)], cwd=ROOT, check=True)
    subprocess.run(['uv', 'pip', 'install', '--python-version', '3.12', '--python-platform', 'x86_64-manylinux_2_28',
                    '--only-binary', ':all:', '--require-hashes', '--no-deps', '--target', str(package),
                    '-r', str(requirements)], cwd=ROOT, check=True)
    shutil.copytree(ROOT / 'src/ledgerguard', package / 'ledgerguard',
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    if not (package / 'mangum/__init__.py').is_file():
        raise SystemExit('Mangum is missing from Lambda package')
    files = sorted(p for p in package.rglob('*') if p.is_file())
    if any(p.suffix == '.dylib' for p in files):
        raise SystemExit('Non-Linux native dependency in Lambda package')
    if sum(p.stat().st_size for p in files) > 250 * 1024 * 1024:
        raise SystemExit('Package exceeds Lambda unzipped size limit')
    output = dist / 'api.zip'
    deterministic_zip(package, output)
    print('Built dist/api.zip (Linux x86_64, Python 3.12)')


if __name__ == '__main__':
    build()
