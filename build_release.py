"""Build and validate the Windows installer. Intended for CI, not end users."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def release_version(text: str) -> str:
    value = text.strip()
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('Release version must be three numeric components')
    return value


def checked(command, **kwargs):
    subprocess.run([str(x) for x in command], check=True, timeout=1200, **kwargs)


def self_test(executable: Path, report: Path):
    report.unlink(missing_ok=True)
    checked([executable, '--self-test', report])
    data = json.loads(report.read_text(encoding='utf-8'))
    if data.get('success') is not True or data.get('frozen') is not True:
        raise RuntimeError(f'Packaged self-test failed: {report}')


def helper_self_test(executable: Path, report: Path):
    report.unlink(missing_ok=True)
    checked([executable, '--self-test', report])
    if json.loads(report.read_text(encoding='utf-8')).get('success') is not True:
        raise RuntimeError(f'Updater self-test failed: {report}')


def compile_setup(compiler: Path, bundle: Path, output: Path, version: str) -> Path:
    version = release_version(version)
    checked([compiler, f'/DAppVersion={version}', f'/DBundleDir={bundle}',
             f'/DOutputDir={output}', ROOT / 'installer' / 'setup.iss'])
    setup = output / f'SNSMediaCollector-Setup-{version}.exe'
    if not setup.is_file():
        raise RuntimeError('Installer was not generated')
    return setup


def verify_installer(compiler: Path, bundle: Path, output: Path, setup: Path, version: str):
    # The fixed installer identity must never overwrite a developer's real install registration.
    if os.environ.get('GITHUB_ACTIONS') != 'true':
        raise RuntimeError('Install/upgrade regression requires a disposable GitHub Actions runner')
    # A baseline installer exercises versioned upgrade paths on an ephemeral runner.
    baseline_version = '0.0.0'
    baseline = compile_setup(compiler, bundle, output, baseline_version)
    data_dir = Path.home() / 'SNSMediaCollector'
    data_dir.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='smc-install-check-') as temporary:
        temp = Path(temporary)
        install = temp / 'installed 日本語'
        marker = data_dir / ('installer-test-' + temp.name + '.txt')
        marker.write_text('existing data must survive', encoding='utf-8')
        original = marker.read_bytes()
        uninstaller = install / 'unins000.exe'
        try:
            for candidate, label in ((baseline, 'baseline'), (setup, 'upgrade'), (setup, 'reinstall')):
                checked([candidate, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART',
                         f'/DIR={install}', '/TASKS=', f'/LOG={output / (label + ".log")}'])
                if marker.read_bytes() != original:
                    raise RuntimeError('Installer modified user data')
            self_test(install / 'versions' / version / 'SNSMediaCollector.exe', output / 'installed-self-test.json')
            if not (install / 'versions' / baseline_version / 'SNSMediaCollector.exe').exists():
                raise RuntimeError('Previous version was unexpectedly removed')
        finally:
            try:
                if uninstaller.exists():
                    checked([uninstaller, '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'])
                if marker.read_bytes() != original:
                    raise RuntimeError('Uninstall modified user data')
            finally:
                marker.unlink(missing_ok=True)
    baseline.unlink(missing_ok=True)


def main() -> int:
    if sys.platform != 'win32':
        raise RuntimeError('Windows is required to build a Windows setup executable')
    version = release_version((ROOT / 'RELEASE_VERSION.txt').read_text())
    output = ROOT / 'release-output'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='smc-release-build-') as temporary:
        work = Path(temporary)
        dist = work / 'dist'
        checked([sys.executable, ROOT / 'full_test.py'], cwd=ROOT)
        checked([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onedir',
                 '--windowed', '--name', 'SNSMediaCollector', '--distpath', dist,
                 '--hidden-import', 'PySide6.QtWebEngineCore',
                 '--hidden-import', 'PySide6.QtWebEngineWidgets',
                 '--workpath', work / 'app', '--specpath', work, ROOT / 'launcher.py'], cwd=ROOT)
        checked([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
                 '--console', '--name', 'gallery-dl', '--collect-all', 'gallery_dl',
                 '--distpath', work / 'engine', '--workpath', work / 'engine-work',
                 '--specpath', work, ROOT / 'gallery_dl_launcher.py'], cwd=ROOT)
        checked([sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile',
                 '--windowed', '--name', 'SNSMediaCollectorUpdater',
                 '--distpath', work / 'updater', '--workpath', work / 'updater-work',
                 '--specpath', work, ROOT / 'update_helper.py'], cwd=ROOT)
        bundle = dist / 'SNSMediaCollector'
        (bundle / 'bin').mkdir()
        shutil.copy2(work / 'engine' / 'gallery-dl.exe', bundle / 'bin' / 'gallery-dl.exe')
        updater = work / 'updater' / 'SNSMediaCollectorUpdater.exe'
        shutil.copy2(updater, bundle / 'bin' / updater.name)
        helper_self_test(bundle / 'bin' / updater.name, output / 'updater-self-test.json')
        self_test(bundle / 'SNSMediaCollector.exe', output / 'bundle-self-test.json')
        compiler = Path(os.environ.get('ISCC_PATH', r'C:\Program Files (x86)\Inno Setup 6\ISCC.exe'))
        if not compiler.is_file():
            raise RuntimeError('Inno Setup 6 is required; set ISCC_PATH if installed elsewhere')
        setup = compile_setup(compiler, bundle, output, version)
        verify_installer(compiler, bundle, output, setup, version)
        digest = hashlib.sha256(setup.read_bytes()).hexdigest()
        (output / 'SHA256SUMS.txt').write_text(f'{digest}  {setup.name}\n', encoding='ascii')
        (output / 'release-check.json').write_text(json.dumps(dict(
            success=True, version=version, setup=setup.name, sha256=digest,
            checks=['frozen app', 'updater helper', 'install', 'upgrade', 'reinstall', 'installed app',
                    'uninstall preserves user data']), indent=2), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
