"""Offline regressions using real Qt processes and persistent SQLite."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QTWEBENGINE_DISABLE_SANDBOX', '1')
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import shiboken6
from PySide6.QtCore import QCoreApplication, QEvent, QProcess, QUrl
from PySide6.QtWidgets import QApplication
from app import MainWindow, DownloadJob
from core import Catalog, LikeSeenRecord, atomic_write_json, partition_likes_records, import_x_likes_seen_archive, archive_path_for, snapshot_media_files
APP = QApplication.instance() or QApplication([])
OLD = LikeSeenRecord('2086000000000000000', 1)
NEW = LikeSeenRecord('2086100000000000000', 1, '123', 'someone', '2026-08-09T09:00:00+0000', 'jpg')
SECOND = LikeSeenRecord(NEW.post_id, 2, NEW.author_id, NEW.author_name, NEW.post_date, 'jpg')
def line(r):
    return f'SMC_LIKE_SEEN\t{r.post_id}\t{r.media_num}\t{r.author_id}\t{r.author_name}\t{r.post_date}\t{r.extension}'
class Regressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='smc-regression-')
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'SMC_TEST_DATA_DIR': str(self.root)}); self.env.start()
        self.w = MainWindow(); self.w.start_next_job = lambda: None
        self.ctx = dict(job_kind='likes_probe', platform='x', target_type='likes', target_key='x:someone', login_name='test', target_name='someone', anchor_ids=[OLD.post_id], anchor_posts=50, archive_scope='x:someone:likes', destination=str(self.root / 'media'), hitomi_compat=True, direct_folder=True, use_archive=True, extensions=['jpg'])
        self.arc = archive_path_for(self.root / 'archives', platform='x', account_name='test', archive_scope=self.ctx['archive_scope'])
        self.w.catalog.replace_likes_anchors([OLD], target_key='x:someone', login_profile='test')
        self.w.target_store.ensure('x:someone', 'x', 'someone')
    def tearDown(self):
        self.w.close(); APP.processEvents(); self.env.stop(); self.temp.cleanup()
    def anchors(self):
        return self.w.catalog.likes_anchor_ids(target_key='x:someone', login_profile='test')
    def probe(self, records, cancelled=False):
        j = DownloadJob('probe', [sys.executable, '-c', 'pass']); j.smc_context = dict(self.ctx)
        j.machine_lines = [line(r) for r in records]; j.anchor_hit = OLD.post_id; j.cancelled = cancelled
        self.w.job_finished(j, -3 if cancelled else 0)
        return j
    def download(self, records, code=0):
        j = DownloadJob('download', [sys.executable, '-c', 'pass'])
        j.smc_context = dict(self.ctx, job_kind='likes_download', probe_new_records=records)
        self.w.job_finished(j, code)
        return j
    def test_archived_probe_repairs_boundary_and_second_probe_after_restart_is_empty(self):
        import_x_likes_seen_archive([NEW, SECOND], self.arc)
        j = self.probe([NEW, SECOND, OLD]); self.assertEqual(len(self.w.jobs), 0); self.assertIn('新規なし', j.status.text())
        self.w.catalog.close(); self.w.catalog = Catalog(self.root / 'catalog.sqlite3')
        self.assertEqual(self.anchors()[0], NEW.post_id)
        self.ctx['anchor_ids'] = self.anchors(); self.probe([NEW, SECOND, OLD]); self.assertEqual(len(self.w.jobs), 0)
    def test_one_archived_image_does_not_skip_second_image(self):
        import_x_likes_seen_archive([NEW], self.arc); self.probe([NEW, SECOND, OLD])
        self.assertEqual(len(self.w.jobs), 1)
        self.assertEqual(self.w.jobs[0].smc_context['probe_new_records'], [NEW, SECOND])
        self.assertEqual(self.anchors(), [OLD.post_id])
    def test_partial_success_holds_boundary_even_with_exit_zero(self):
        import_x_likes_seen_archive([NEW], self.arc); j = self.download([NEW, SECOND])
        self.assertEqual(self.anchors(), [OLD.post_id]); self.assertIn('未確認 1', j.status.text())
        self.assertFalse(self.w.target_store.get('x:someone').last_success_at)
    def test_retry_completes_without_metadata_capture(self):
        import_x_likes_seen_archive([NEW], self.arc); self.download([NEW, SECOND], code=1)
        self.assertEqual(self.anchors(), [OLD.post_id])
        import_x_likes_seen_archive([SECOND], self.arc); self.download([NEW, SECOND])
        self.assertEqual(self.anchors()[0], NEW.post_id)
        self.ctx['anchor_ids'] = self.anchors(); self.probe([NEW, SECOND, OLD]); self.assertEqual(len(self.w.jobs), 0)
    def test_cancelled_probe_never_queues_even_when_anchor_was_seen(self):
        self.probe([NEW, OLD], cancelled=True); self.assertEqual(len(self.w.jobs), 0); self.assertEqual(self.anchors(), [OLD.post_id])
    def test_missing_report_blocks_even_with_archive_entry(self):
        import_x_likes_seen_archive([NEW], self.arc)
        j = DownloadJob('missing', [sys.executable]); j.smc_context = dict(self.ctx, job_kind='likes_download', probe_new_records=[NEW])
        j.reported_file_paths = ['missing.jpg']; j.missing_reported_paths = ['missing.jpg']
        self.w.job_finished(j, 0); self.assertEqual(self.anchors(), [OLD.post_id])
    def test_corrupt_archive_holds_boundary(self):
        self.arc.parent.mkdir(parents=True); self.arc.write_bytes(b'broken db')
        self.probe([NEW, OLD]); self.assertEqual(len(self.w.jobs), 0); self.assertEqual(self.anchors(), [OLD.post_id])
    def test_zero_length_hitomi_file_is_not_acquired(self):
        root = Path(self.ctx['destination']); root.mkdir(); file = root / f'[26-08-09] {NEW.post_id}_p0.jpg'; file.touch()
        self.assertEqual(partition_likes_records([NEW], None, destination=root, hitomi_compat=True)[1], [NEW])
        file.write_bytes(b'valid placeholder')
        self.assertEqual(partition_likes_records([NEW], None, destination=root, hitomi_compat=True)[0], [NEW])
    def test_unrelated_and_outside_files_never_count(self):
        root = Path(self.ctx['destination']); root.mkdir(); j = DownloadJob('verify', [sys.executable]); j.verification_root = root
        j.preexisting_media = snapshot_media_files(root); (root / 'unrelated.jpg').write_bytes(b'other app')
        outside = self.root / 'outside.jpg'; outside.write_bytes(b'outside'); j.reported_file_paths = [str(outside)]
        j._reconcile_actual_files(); self.assertEqual(j.files_saved_count, 0); self.assertEqual(j.missing_reported_paths, [str(outside)])

    def test_likes_recovers_mojibaked_report_paths_from_tweet_ids(self):
        root = self.root / '珈琲砂糖 (@KoffeeS_08) - Likes'; root.mkdir()
        j = DownloadJob('verify', [sys.executable])
        j.verification_root = root
        j.preexisting_media = snapshot_media_files(root, recursive=False)
        j.verification_recursive = False
        j.smc_context = dict(
            self.ctx,
            job_kind='likes_download',
            probe_new_records=[NEW, SECOND],
        )
        first = root / f'[26-09-16] {NEW.post_id}_p0.jpg'
        second = root / f'[26-09-16] {NEW.post_id}_p1.jpg'
        first.write_bytes(b'first'); second.write_bytes(b'second')
        j.reported_file_paths = [
            rf'D:\\hitomi\\���荻�� (@KoffeeS_08) - Likes\\{first.name}',
            rf'D:\\hitomi\\���荻�� (@KoffeeS_08) - Likes\\{second.name}',
        ]
        j._reconcile_actual_files()
        self.assertEqual(set(j.verified_file_paths), {str(first), str(second)})
        self.assertEqual(j.missing_reported_paths, [])
    def test_atomic_json_failure_preserves_settings(self):
        p = self.root / 'accounts.json'; atomic_write_json(p, {'name': 'original'})
        with patch('core.os.replace', side_effect=OSError('simulated')):
            with self.assertRaises(OSError): atomic_write_json(p, {'name': 'changed'})
        self.assertEqual(json.loads(p.read_text()), {'name': 'original'}); self.assertEqual(list(self.root.glob('accounts.json.*.tmp')), [])
    def run_process(self, j):
        events = []; j.finished.connect(lambda _, rc: events.append(rc)); j.start(); limit = time.monotonic() + 8
        while not events and time.monotonic() < limit: APP.processEvents(); time.sleep(.005)
        self.assertTrue(events, 'Process did not finish'); return events
    def test_failed_engine_start_finishes_once(self):
        j = DownloadJob('missing-engine', [str(self.root / 'missing-executable')])
        self.assertEqual(self.run_process(j), [-1]); self.assertTrue(j.completed)
    def test_real_process_final_output_without_newline_and_unicode_path(self):
        root = Path(self.ctx['destination']); root.mkdir(); out = root / '日本語 test.jpg'
        source = f"from pathlib import Path; import sys; p=Path({str(out)!r}); p.write_bytes(b'image'); sys.stdout.buffer.write(('SMC_FILE\\t'+str(p)).encode('utf-8'))"
        j = DownloadJob('real-process', [sys.executable, '-c', source]); j.smc_context = dict(self.ctx, job_kind='likes_download')
        self.assertEqual(self.run_process(j), [0]); self.assertEqual(j.files_saved_count, 1); self.assertEqual(j.verified_file_paths, [str(out)])
    def test_utf8_split_across_process_reads_keeps_japanese_path(self):
        root = Path(self.ctx['destination']); root.mkdir(); out = root / '日本語.jpg'
        source = (f"from pathlib import Path; import sys,time; p=Path({str(out)!r}); p.write_bytes(b'image'); "
                  "data=('SMC_FILE\\t'+str(p)+'\\n').encode('utf-8'); "
                  "i=data.index('日'.encode('utf-8'))+1; "
                  "sys.stdout.buffer.write(data[:i]); sys.stdout.buffer.flush(); time.sleep(.1); "
                  "sys.stdout.buffer.write(data[i:]); sys.stdout.buffer.flush()")
        j = DownloadJob('split-utf8', [sys.executable, '-c', source])
        j.smc_context = dict(self.ctx, job_kind='likes_download')
        self.assertEqual(self.run_process(j), [0])
        self.assertEqual(j.verified_file_paths, [str(out)])

    def test_stop_does_not_advance(self):
        j = DownloadJob('stop', [sys.executable, '-c', 'import time; time.sleep(20)'])
        j.smc_context = dict(self.ctx, job_kind='likes_download', probe_new_records=[NEW]); events = []
        j.finished.connect(lambda _, rc: events.append(rc)); j.start(); j.process.waitForStarted(2000); j.stop()
        limit = time.monotonic() + 5
        while not events and time.monotonic() < limit: APP.processEvents(); time.sleep(.005)
        self.assertEqual(events, [-3]); self.w.job_finished(j, events[0]); self.assertEqual(self.anchors(), [OLD.post_id])
    def test_real_gallery_cli_saves_then_skips_using_archive(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from threading import Thread
        from core import build_command
        requests = []
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                payload = b"local media test fixture"
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers(); self.wfile.write(payload)
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            dest = self.root / 'real-gallery'
            cmd = build_command([sys.executable, '-m', 'gallery_dl', '--ignore-config'],
                platform='directlink', url=f'http://127.0.0.1:{server.server_port}/fixture.jpg',
                destination=dest, account_name='test', auth_mode='none', auth_value='',
                archive_scope='local', extensions=['jpg'], capture_internal_metadata=False,
                use_archive=True, archive_dir=self.root / 'real-archive', direct_folder=True,
                range_mode='all', profile=None)
            for count in (1, 0):
                j = DownloadJob('gallery', cmd)
                j.smc_context = {'destination': str(dest), 'direct_folder': True}
                self.assertEqual(self.run_process(j), [0], j.output_lines)
                self.assertEqual(j.files_saved_count, count, j.machine_lines)
                self.assertEqual(j.missing_reported_paths, [])
            self.assertEqual(len(requests), 1)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_folder_preview_does_not_pollute_download_history(self):
        root = Path(self.ctx['destination']); root.mkdir()
        (root / 'old.jpg').write_bytes(b'old')
        self.w.dest_edit.setText(str(root))
        with patch('app.QDialog.exec', return_value=0):
            self.w.seed_recent_from_current_destination()
        self.assertEqual(self.w.catalog.recent_file_count(), 0)

    def test_restart_restores_selected_account_target_and_destination(self):
        from app import AccountProfile
        self.w.accounts = [AccountProfile('same', 'x'), AccountProfile('same', 'x', 'browser', 'firefox')]
        chosen = self.w.accounts[1].profile_id
        self.w.save_accounts(); self.w.refresh_accounts(chosen)
        self.w.url_edit.setText('@remember_me')
        for button in self.w.target_buttons:
            if button.property('key') == 'likes': button.setChecked(True)
        self.w.sync_target_ui()
        self.w.dest_edit.setText(str(self.root / 'my images'))
        self.w.chk_video.setChecked(False)
        self.w.close(); self.w = MainWindow(); self.w.start_next_job = lambda: None
        self.assertEqual(self.w.accounts[self.w.account_combo.currentData()].profile_id, chosen)
        self.assertEqual(self.w.account_list.currentRow(), 1)
        self.assertEqual(self.w.url_edit.text(), '@remember_me')
        self.assertEqual(self.w.selected_target(), 'likes')
        self.assertEqual(self.w.dest_edit.text(), str(self.root / 'my images'))
        self.assertFalse(self.w.chk_video.isChecked())
        self.assertNotIn('firefox', str(self.w.settings.value('last_session')))

    def test_account_switch_restores_each_target_and_destination(self):
        from app import AccountProfile
        first = AccountProfile('first', 'x')
        second = AccountProfile('second', 'x')
        self.w.accounts = [first, second]
        self.w.save_accounts(); self.w.refresh_accounts(first.profile_id)

        self.w.url_edit.setText('@first_target')
        self.w.dest_edit.setText(str(self.root / 'first images'))
        self.w.account_combo.setCurrentIndex(self.w.account_combo.findData(1))
        self.assertEqual(self.w.url_edit.text(), '')
        self.assertEqual(self.w.dest_edit.text(), '')

        self.w.url_edit.setText('@second_target')
        self.w.dest_edit.setText(str(self.root / 'second images'))
        self.w.account_combo.setCurrentIndex(self.w.account_combo.findData(0))
        self.assertEqual(self.w.url_edit.text(), '@first_target')
        self.assertEqual(self.w.dest_edit.text(), str(self.root / 'first images'))

        self.w.account_combo.setCurrentIndex(self.w.account_combo.findData(1))
        self.assertEqual(self.w.url_edit.text(), '@second_target')
        self.assertEqual(self.w.dest_edit.text(), str(self.root / 'second images'))
        self.w.close(); self.w = MainWindow(); self.w.start_next_job = lambda: None
        self.assertEqual(self.w.accounts[self.w.account_combo.currentData()].profile_id, second.profile_id)
        self.assertEqual(self.w.url_edit.text(), '@second_target')
        self.assertEqual(self.w.dest_edit.text(), str(self.root / 'second images'))

    def test_legacy_accounts_migrate_and_refresh_preserves_selection(self):
        self.w.close()
        (self.root / 'accounts.json').write_text(json.dumps([
            dict(name='one', platform='x', auth_mode='cookies_file', auth_value='local-cookies.txt'),
            dict(name='two', platform='x', auth_mode='browser', auth_value='edge')]))
        self.w = MainWindow(); self.w.start_next_job = lambda: None
        self.w.account_combo.setCurrentIndex(1)
        selected = self.w.accounts[1].profile_id
        self.w.refresh_accounts()
        self.assertEqual(self.w.account_combo.currentData(), 1)
        from app import AccountDialog
        dialog = AccountDialog(self.w, self.w.accounts[1])
        dialog.name_edit.setText('renamed')
        self.assertEqual(dialog.result_profile().profile_id, selected)
        stored = json.loads((self.root / 'accounts.json').read_text())
        self.assertEqual(stored[1]['profile_id'], selected)
        self.assertEqual(stored[0]['auth_value'], 'local-cookies.txt')

    def test_managed_x_account_uses_private_data_path_and_survives_restart(self):
        from app import AccountDialog, AccountProfile
        profile = AccountProfile('managed', 'x', 'managed_x', 'obsolete-path.txt')
        dialog = AccountDialog(self.w, profile, data_dir=self.root, engine=[sys.executable])
        result = dialog.result_profile()
        expected = self.root / 'auth' / 'cookies' / f'x-{profile.profile_id}.txt'
        self.assertEqual(result.auth_mode, 'managed_x')
        self.assertEqual(Path(result.auth_value), expected)
        self.assertIn('未ログイン', dialog.auth_status.text())
        dialog.close()
        self.w.accounts = [result]
        self.w.save_accounts()
        self.w.close(); self.w = MainWindow(); self.w.start_next_job = lambda: None
        self.assertEqual(Path(self.w.accounts[0].auth_value), expected)

    def test_new_x_account_defaults_to_isolated_login(self):
        from app import AccountDialog
        dialog = AccountDialog(self.w, data_dir=self.root, engine=[sys.executable])
        self.assertEqual(dialog.platform.currentData(), 'x')
        self.assertEqual(dialog.auth_mode.currentData(), 'managed_x')
        self.assertTrue(dialog.auth_value.isReadOnly())
        dialog.platform.setCurrentIndex(dialog.platform.findData('pixiv'))
        self.assertEqual(dialog.auth_mode.currentData(), 'managed_pixiv')
        self.assertTrue(dialog.auth_value.isReadOnly())
        dialog.platform.setCurrentIndex(dialog.platform.findData('x'))
        self.assertEqual(dialog.auth_mode.currentData(), 'managed_x')
        dialog.close()

    def test_account_list_shows_managed_login_readiness(self):
        from app import AccountProfile
        from auth_store import BrowserCookie, managed_x_cookie_path, write_netscape_cookie_file
        ready = AccountProfile('ready', 'x', 'managed_x', '', 'ready-id')
        missing = AccountProfile('missing', 'x', 'managed_x', '', 'missing-id')
        ready.auth_value = str(managed_x_cookie_path(self.root, ready.profile_id))
        missing.auth_value = str(managed_x_cookie_path(self.root, missing.profile_id))
        write_netscape_cookie_file(Path(ready.auth_value), [
            BrowserCookie('.x.com', '/', True, 0, 'auth_token', 'secret'),
        ])
        self.w.accounts = [ready, missing]
        self.w.refresh_accounts(ready.profile_id)
        self.assertIn('✓', self.w.account_list.item(0).text())
        self.assertIn('⚠', self.w.account_list.item(1).text())

    def test_managed_pixiv_account_path_survives_restart(self):
        from app import AccountProfile
        from auth_store import managed_pixiv_cache_path
        profile = AccountProfile('pixiv-main', 'pixiv', 'managed_pixiv', 'obsolete.sqlite3')
        self.w.accounts = [profile]
        self.w.save_accounts()
        self.w.close(); self.w = MainWindow(); self.w.start_next_job = lambda: None
        self.assertEqual(
            Path(self.w.accounts[0].auth_value),
            managed_pixiv_cache_path(self.root, profile.profile_id),
        )

    def test_managed_pixiv_missing_cache_is_blocked_before_gallery_dl(self):
        with self.assertRaisesRegex(ValueError, '未連携'):
            self.w.ensure_auth_ready('managed_pixiv', str(self.root / 'missing.sqlite3'))

    def test_pixiv_login_dialog_captures_callback_and_uses_isolated_cache(self):
        try:
            from PySide6.QtWebEngineCore import QWebEngineProfile  # noqa: F401
        except ImportError as exc:
            self.skipTest(f'QtWebEngine runtime unavailable: {exc}')
        from app import PixivLoginDialog
        from auth_store import inspect_pixiv_cache, managed_pixiv_cache_path
        cache = managed_pixiv_cache_path(self.root, 'oauth-flow')
        source = (
            "import sys,sqlite3,pickle,pathlib; p=pathlib.Path(sys.argv[1]); "
            "print('https://app-api.pixiv.net/web/v1/login?client=pixiv-android&code_challenge=test',flush=True); "
            "line=sys.stdin.readline(); assert line.strip() == 'sample-code', line; p.parent.mkdir(parents=True,exist_ok=True); "
            "c=sqlite3.connect(p); c.execute('CREATE TABLE data (key TEXT PRIMARY KEY,value TEXT,expires INTEGER)'); "
            "c.execute('INSERT INTO data VALUES (?,?,?)',('gallery_dl.extractor.pixiv._refresh_token_cache-None',pickle.dumps('secret'),0)); "
            "c.commit(); c.close()"
        )
        dialog = PixivLoginDialog(
            self.root, 'oauth-flow', cache,
            [sys.executable, '-c', source, str(cache)], self.w,
            persistent_web_profile=False,
        )
        accepted = []
        dialog.accepted.connect(lambda: accepted.append(True))
        limit = time.monotonic() + 5
        while not dialog._login_url_loaded and time.monotonic() < limit:
            APP.processEvents(); time.sleep(.01)
        self.assertTrue(dialog._login_url_loaded)
        with patch('app.QMessageBox.information', return_value=0):
            dialog._url_changed(QUrl(
                'https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?code=sample-code&state=after-code'
            ))
            limit = time.monotonic() + 5
            while dialog.process.state() != QProcess.NotRunning and time.monotonic() < limit:
                APP.processEvents(); time.sleep(.01)
            APP.processEvents()
        self.assertEqual(accepted, [True])
        self.assertTrue(inspect_pixiv_cache(cache)['authenticated'])
        if shiboken6.isValid(dialog):
            dialog.deleteLater()
        del dialog
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        APP.processEvents()

    def test_managed_x_missing_cookie_is_blocked_before_gallery_dl(self):
        with self.assertRaisesRegex(ValueError, '未ログイン'):
            self.w.ensure_auth_ready('managed_x', str(self.root / 'missing.txt'))

    def test_deleted_saved_account_does_not_select_another_login(self):
        from app import AccountProfile
        self.w.accounts = [AccountProfile('one', 'x'), AccountProfile('two', 'x')]
        self.w.save_accounts(); self.w.refresh_accounts(self.w.accounts[1].profile_id)
        self.w.save_session()
        self.w.close()
        data=json.loads((self.root / 'accounts.json').read_text())
        (self.root / 'accounts.json').write_text(json.dumps(data[:1]))
        self.w = MainWindow(); self.w.start_next_job = lambda: None
        self.assertEqual(self.w.account_combo.currentData(), -1)

    def test_test_settings_do_not_use_user_registry(self):
        self.assertEqual(Path(self.w.settings.fileName()), self.root / 'settings.ini')
if __name__ == '__main__': unittest.main(verbosity=2)
