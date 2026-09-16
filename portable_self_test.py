"""Offline startup gate for the actual packaged app, without touching user data."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import traceback


def run(output: str) -> int:
    report = {'success': False, 'frozen': bool(getattr(sys, 'frozen', False)), 'checks': []}
    old_data = os.environ.get('SMC_TEST_DATA_DIR')
    old_qt = os.environ.get('QT_QPA_PLATFORM')
    window = None
    try:
        os.environ['QT_QPA_PLATFORM'] = 'offscreen'
        with tempfile.TemporaryDirectory(prefix='smc-portable-check-') as temporary:
            os.environ['SMC_TEST_DATA_DIR'] = temporary
            from PySide6.QtWidgets import QApplication
            from PySide6.QtGui import QPixmap
            from app import MainWindow, APP_VERSION
            from core import LikeSeenRecord, Catalog
            application = QApplication.instance() or QApplication([])
            window = MainWindow()
            try:
                report['version'] = APP_VERSION
                assert window.data_dir.resolve() == Path(temporary).resolve()
                report['checks'].append('isolated Qt MainWindow')
                path = Path(temporary) / 'sample.png'
                pix = QPixmap(40, 40); pix.fill()
                assert pix.save(str(path))
                window.catalog.record_recent_file(path)
                window.refresh_recent_downloads()
                assert window.recent_grid.count() == 1
                report['checks'].append('PNG plugin and thumbnail widget')
                window.catalog.replace_likes_anchors([LikeSeenRecord('2086000000000000000', 1)],
                    target_key='test', login_profile='test')
                window.catalog.close()
                window.catalog = Catalog(Path(temporary) / 'catalog.sqlite3')
                assert window.catalog.likes_anchor_ids(target_key='test', login_profile='test') == ['2086000000000000000']
                report['checks'].append('SQLite anchor reopen')
                assert window.engine_available(), 'Bundled gallery-dl engine missing'
                command = window.engine_command()
                result = subprocess.run(command + ['--ignore-config', '--version'], capture_output=True,
                    text=True, encoding='utf-8', errors='replace', timeout=30)
                assert result.returncode == 0, result.stderr
                report['engine_version'] = result.stdout.strip()
                # Listing modules exercises the sidecar's dynamically imported extractors.
                result = subprocess.run(command + ['--ignore-config', '--list-modules'], capture_output=True,
                    text=True, encoding='utf-8', errors='replace', timeout=30)
                assert result.returncode == 0, result.stderr
                assert 'twitter' in result.stdout and 'pixiv' in result.stdout, result.stdout[-1000:]
                report['checks'].append('bundled engine and X/pixiv extractor modules')
                application.processEvents()
                report['success'] = True
            finally:
                window.close()
                window = None
                application.processEvents()
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        for key, previous in [('SMC_TEST_DATA_DIR', old_data), ('QT_QPA_PLATFORM', old_qt)]:
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
    try:
        Path(output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    except OSError:
        return 2
    return 0 if report['success'] else 1
