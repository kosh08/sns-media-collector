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
from PySide6.QtCore import QCoreApplication, QEvent, QProcess, QSize, QUrl
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
from app import DARK_QSS, ElidedLabel, MainWindow, DownloadJob, is_ephemeral_test_path, pixiv_callback_code, pixiv_oauth_command, resolved_application_data_dir, resolved_test_data_dir, sanitized_pixiv_oauth_diagnostic, scaled_media_pixmap
from core import Catalog, LikeSeenRecord, PostRecord, atomic_write_json, partition_likes_records, import_x_likes_seen_archive, archive_path_for, snapshot_media_files
from collection_profiles import CollectionProfile
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

    def test_collection_download_recovers_mojibaked_report_path(self):
        root = self.root / '日本語の確認箱'; root.mkdir()
        j = DownloadJob('review verify', [sys.executable])
        j.verification_root = root
        j.preexisting_media = snapshot_media_files(root, recursive=False)
        j.verification_recursive = False
        j.smc_context = {
            'job_kind': 'collection_download', 'platform': 'x',
            'collection_post_ids': [NEW.post_id],
        }
        saved = root / f'[26-09-16] {NEW.post_id}_p0.jpg'
        saved.write_bytes(b'image')
        j.reported_file_paths = [rf'D:\hitomi\���{saved.name}']
        j._reconcile_actual_files()
        self.assertEqual(j.verified_file_paths, [str(saved)])
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

    def test_sidebar_cross_platform_switch_preserves_outgoing_workspace(self):
        from app import AccountProfile
        x_account = AccountProfile('x-account', 'x')
        pixiv_account = AccountProfile('pixiv-account', 'pixiv')
        self.w.accounts = [x_account, pixiv_account]
        self.w.save_accounts(); self.w.refresh_accounts(x_account.profile_id)
        self.w.url_edit.setText('@x_target')
        self.w.dest_edit.setText(str(self.root / 'x images'))

        self.w.account_list.setCurrentRow(1)
        self.assertEqual(self.w.platform_combo.currentData(), 'pixiv')
        self.assertFalse(self.w.platform_combo.isEnabled())
        self.assertEqual(self.w.url_edit.text(), '')
        self.w.url_edit.setText('123456')
        self.w.dest_edit.setText(str(self.root / 'pixiv images'))

        self.w.account_list.setCurrentRow(0)
        self.assertEqual(self.w.platform_combo.currentData(), 'x')
        self.assertEqual(self.w.url_edit.text(), '@x_target')
        self.assertEqual(self.w.dest_edit.text(), str(self.root / 'x images'))
        self.w.account_list.setCurrentRow(1)
        self.assertEqual(self.w.url_edit.text(), '123456')
        self.assertEqual(self.w.dest_edit.text(), str(self.root / 'pixiv images'))

    def test_pixiv_bookmark_range_explains_archive_based_incremental_mode(self):
        self.w.platform_combo.setCurrentIndex(self.w.platform_combo.findData('pixiv'))
        likes = next(
            button for button in self.w.target_buttons
            if button.property('key') == 'likes'
        )
        likes.setChecked(True)
        self.w.sync_target_ui()
        self.assertEqual(
            self.w.range_buttons['incremental'].text(),
            '新しいブックマークだけ',
        )
        self.assertIn('作品の投稿日に関係なく', self.w.range_note.text())
        self.assertIn('古い作品を最近追加した場合も対象', self.w.range_status.text())

    def test_download_form_only_shows_controls_relevant_to_the_selected_target(self):
        self.assertTrue(self.w.likes_controls.isHidden())
        self.assertTrue(self.w.date_after_edit.isHidden())
        self.assertTrue(self.w.advanced_panel.isHidden())

        likes = next(button for button in self.w.target_buttons if button.property('key') == 'likes')
        likes.setChecked(True)
        self.w.sync_target_ui()
        self.assertFalse(self.w.likes_controls.isHidden())
        self.assertEqual(self.w.start_btn.text(), '⬇ 新しいいいねを確認')

        self.w.platform_combo.setCurrentIndex(self.w.platform_combo.findData('pixiv'))
        likes.setChecked(True)
        self.w.sync_target_ui()
        self.assertTrue(self.w.target_buttons[1].isHidden())
        self.assertTrue(self.w.likes_controls.isHidden())
        self.assertFalse(self.w.range_buttons['date'].isEnabled())
        self.assertEqual(self.w.start_btn.text(), '⬇ ブックマークを取得')
        self.assertEqual(self.w.import_btn.text(), '既存の保存フォルダをこの対象に登録')

        self.w.advanced_toggle.setChecked(True)
        self.assertFalse(self.w.advanced_panel.isHidden())
        self.assertEqual(self.w.advanced_toggle.text(), '▼ 詳細設定')

    def test_selected_controls_have_high_contrast_styles(self):
        self.assertIn("QRadioButton:checked", DARK_QSS)
        self.assertIn("background: #285aa8", DARK_QSS)
        self.assertIn("QListWidget::item:selected", DARK_QSS)
        self.assertIn("border: 1px solid #79a9ff", DARK_QSS)
        self.assertIn("QCheckBox:checked", DARK_QSS)
        self.assertIn("QToolButton:checked", DARK_QSS)
        self.assertIn("QComboBox QAbstractItemView", DARK_QSS)

    def test_visual_hierarchy_keeps_login_management_collapsible(self):
        self.assertFalse(self.w.auth_panel.isHidden())
        self.w.auth_toggle.setChecked(False)
        self.assertTrue(self.w.auth_panel.isHidden())
        self.assertIn('▶', self.w.auth_toggle.text())
        self.w.auth_toggle.setChecked(True)
        self.assertFalse(self.w.auth_panel.isHidden())
        self.assertIn('▼', self.w.auth_toggle.text())

    def test_selected_collection_is_named_in_editor_heading(self):
        from app import AccountProfile
        account = AccountProfile('x-main', 'x')
        self.w.accounts = [account]
        self.w.refresh_accounts(account.profile_id)
        collection = CollectionProfile(
            name='資料用ブックマーク', account_id=account.profile_id, platform='x',
            source='bookmarks', target_scope='self', target_value='',
            content_mode='text_images', review_mode='inbox',
            destination=str(self.root / 'media'), text_destination=str(self.root / 'text'),
        )
        self.w.collection_store.items = [collection]
        self.w.refresh_collections(collection.collection_id)
        self.assertEqual(self.w.editor_title.text(), '資料用ブックマーク')
        self.assertIn('x-main', self.w.editor_subtitle.text())
        self.assertIn('確認箱へ', self.w.editor_subtitle.text())
        self.assertEqual(self.w.editor_state.text(), '選択中')

    def test_queue_empty_state_is_replaced_when_job_is_added(self):
        self.assertFalse(self.w.queue_empty_label.isHidden())
        self.assertTrue(self.w.queue_scroll.isHidden())
        self.w.connect_job(DownloadJob('queued', [sys.executable, '-c', 'pass']))
        self.assertTrue(self.w.queue_empty_label.isHidden())
        self.assertFalse(self.w.queue_scroll.isHidden())

    def test_long_filename_is_elided_without_losing_full_tooltip(self):
        name = 'とても長い日本語ファイル名_' + ('1234567890' * 8) + '_p0.jpg'
        label = ElidedLabel(name)
        label.resize(150, 24)
        APP.processEvents()
        self.assertIn('…', label.text())
        self.assertNotEqual(label.text(), name)
        self.assertEqual(label.toolTip(), name)
        label.close()

    def test_ephemeral_smoke_destination_is_repaired_but_user_temp_path_is_kept(self):
        bad = r'C:\Users\SampleUser\AppData\Local\Temp\smc-smoke-example\Library'
        good = r'C:\Users\SampleUser\AppData\Local\Temp\personal-downloads'
        self.assertTrue(is_ephemeral_test_path(bad))
        self.assertFalse(is_ephemeral_test_path(good))

        profile = self.w.target_store.ensure('x:test', 'x', 'test')
        profile.destination = bad
        self.w.collection_store.items = [CollectionProfile(
            name='test', account_id='account', platform='x', destination=bad,
            text_destination=bad + r'\text',
        )]
        self.w.account_sessions = {'account': {'destination': bad}}
        self.w.settings.setValue('last_session', json.dumps({'destination': bad}))

        self.assertEqual(self.w.repair_ephemeral_test_paths(), 5)
        expected = str(self.root / 'Library')
        self.assertEqual(profile.destination, expected)
        self.assertEqual(self.w.collection_store.items[0].destination, expected)
        self.assertEqual(self.w.collection_store.items[0].text_destination, str(self.root / 'Library' / 'text'))
        self.assertEqual(self.w.account_sessions['account']['destination'], expected)
        self.assertEqual(json.loads(str(self.w.settings.value('last_session')))['destination'], expected)

    def test_packaged_normal_launch_ignores_test_data_override(self):
        with patch.object(sys, 'frozen', True, create=True):
            with patch.dict(os.environ, {
                'SMC_TEST_DATA_DIR': str(self.root),
                'SMC_PACKAGED_SELF_TEST': '',
            }):
                self.assertEqual(resolved_test_data_dir(), '')

            with patch.dict(os.environ, {
                'SMC_TEST_DATA_DIR': str(self.root),
                'SMC_PACKAGED_SELF_TEST': '1',
            }):
                self.assertEqual(resolved_test_data_dir(), str(self.root))

        self.assertEqual(resolved_test_data_dir(), str(self.root))

    def test_persisted_smoke_data_root_returns_to_canonical_folder(self):
        leaked = r'C:\Users\SampleUser\AppData\Local\Temp\smc-smoke-example'
        data_dir, repaired = resolved_application_data_dir(
            '', leaked, Path('C:/Users/SampleUser'),
        )
        self.assertEqual(data_dir, Path('C:/Users/SampleUser/SNSMediaCollector'))
        self.assertEqual(repaired, leaked)

        explicit = Path('/opt/sample-user/CustomData')
        data_dir, repaired = resolved_application_data_dir('', str(explicit), Path('/opt/sample-user'))
        self.assertEqual(data_dir, explicit)
        self.assertEqual(repaired, '')

    def test_pixiv_self_bookmarks_need_no_user_id(self):
        from app import AccountProfile
        account = AccountProfile('ksk', 'pixiv', 'none', '')
        self.w.accounts = [account]
        self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        self.w.scope_combo.setCurrentIndex(self.w.scope_combo.findData('self'))
        for button in self.w.target_buttons:
            if button.property('key') == 'likes':
                button.setChecked(True)
                break
        self.w.url_edit.clear()
        self.w.sync_target_ui()

        url, key, name = self.w.current_target()
        self.assertEqual(url, 'https://www.pixiv.net/bookmark.php')
        self.assertEqual(key, 'pixiv:self:bookmarks')
        self.assertEqual(name, '自分のブックマーク')
        command, _title = self.w.build_command()
        self.assertEqual(command[-1], url)

    def test_collection_destination_is_shown_as_registered(self):
        from app import AccountProfile
        account = AccountProfile('ksk', 'pixiv')
        self.w.accounts = [account]
        self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        collection = CollectionProfile(
            name='pixiv bookmarks', account_id=account.profile_id, platform='pixiv',
            source='likes', destination=str(self.root / 'pixiv-downloads'),
        )
        self.w.collection_store.items = [collection]
        self.w.collection_store.save()
        self.w.refresh_collections(collection.collection_id)
        self.assertEqual(self.w.dest_edit.text(), collection.destination)
        self.assertIn('登録済み', self.w.target_status.text())

    def test_sidebar_only_shows_login_action_for_selected_service(self):
        from app import AccountProfile
        self.w.accounts = [AccountProfile('x-main', 'x'), AccountProfile('pixiv-main', 'pixiv')]
        self.w.save_accounts(); self.w.refresh_accounts(self.w.accounts[0].profile_id)
        self.assertFalse(self.w.quick_x_login_btn.isHidden())
        self.assertTrue(self.w.pixiv_login_btn.isHidden())

        self.w.account_list.setCurrentRow(1)
        self.assertTrue(self.w.quick_x_login_btn.isHidden())
        self.assertFalse(self.w.pixiv_login_btn.isHidden())

    def test_same_target_can_keep_a_different_destination_per_account(self):
        from app import AccountProfile
        first = AccountProfile('first', 'x')
        second = AccountProfile('second', 'x')
        self.w.accounts = [first, second]
        self.w.save_accounts(); self.w.refresh_accounts(first.profile_id)
        self.w.url_edit.setText('@shared_target')
        self.w.dest_edit.setText(str(self.root / 'first destination'))
        self.w.account_combo.setCurrentIndex(self.w.account_combo.findData(1))
        self.w.url_edit.setText('@shared_target')
        self.w.dest_edit.setText(str(self.root / 'second destination'))

        self.w.account_combo.setCurrentIndex(self.w.account_combo.findData(0))
        self.assertEqual(self.w.url_edit.text(), '@shared_target')
        self.assertEqual(self.w.dest_edit.text(), str(self.root / 'first destination'))
        self.w.account_combo.setCurrentIndex(self.w.account_combo.findData(1))
        self.assertEqual(self.w.url_edit.text(), '@shared_target')
        self.assertEqual(self.w.dest_edit.text(), str(self.root / 'second destination'))

        self.w.account_combo.setCurrentIndex(self.w.account_combo.findData(-1))
        self.assertTrue(self.w.platform_combo.isEnabled())

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
        self.assertIn('未認証', dialog.auth_status.text())
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
        self.assertIn('ブラウザでXを開く', dialog.browser_button.text())
        self.assertIn('cookies.txtを取り込む', dialog.import_button.text())
        self.assertIn('作り方', dialog.cookie_help_button.text())
        self.assertIn('予備', dialog.login_button.text())
        self.assertTrue(dialog.acceptDrops())
        self.assertTrue(dialog.auth_value.isReadOnly())
        dialog.platform.setCurrentIndex(dialog.platform.findData('pixiv'))
        self.assertEqual(dialog.auth_mode.currentData(), 'managed_pixiv')
        self.assertTrue(dialog.auth_value.isReadOnly())
        dialog.platform.setCurrentIndex(dialog.platform.findData('x'))
        self.assertEqual(dialog.auth_mode.currentData(), 'managed_x')
        dialog.close()

    def test_account_edit_preserves_detected_identity(self):
        from app import AccountDialog, AccountProfile
        profile = AccountProfile(
            'identity', 'x', 'managed_x', '', user_id='123456789',
            username='example_user', profile_id='identity-profile',
        )
        dialog = AccountDialog(self.w, profile, data_dir=self.root, engine=[sys.executable])
        dialog.name_edit.setText('renamed')
        result = dialog.result_profile()
        self.assertEqual(result.user_id, '123456789')
        self.assertEqual(result.username, 'example_user')
        dialog.close()

    def test_cookie_import_detects_identity_and_blocks_cross_account_overwrite(self):
        from app import AccountDialog, AccountProfile
        from auth_store import inspect_netscape_cookie_file, managed_x_cookie_path

        def exported(path, user_id, token):
            path.write_text(
                '# Netscape HTTP Cookie File\n'
                f'#HttpOnly_.x.com\tTRUE\t/\tTRUE\t2000000000\tauth_token\t{token}\n'
                f'.x.com\tTRUE\t/\tTRUE\t2000000000\ttwid\tu%3D{user_id}\n',
                encoding='utf-8',
            )

        profile = AccountProfile(
            'main', 'x', 'managed_x', '', user_id='111', profile_id='cookie-profile',
        )
        dialog = AccountDialog(self.w, profile, data_dir=self.root, engine=[sys.executable])
        dialog.test_x_auth = lambda: None
        matching = self.root / 'matching.txt'; exported(matching, '111', 'first-token')
        with patch('app.QMessageBox.information'):
            self.assertTrue(dialog._import_cookie_path(matching))
        destination = managed_x_cookie_path(self.root, profile.profile_id)
        self.assertEqual(inspect_netscape_cookie_file(destination)['x_user_id'], '111')
        before = destination.read_bytes()

        wrong = self.root / 'wrong.txt'; exported(wrong, '222', 'second-token')
        with patch('app.QMessageBox.warning') as warning:
            self.assertFalse(dialog._import_cookie_path(wrong))
        self.assertEqual(destination.read_bytes(), before)
        self.assertIn('一致しません', warning.call_args.args[2])
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

    def test_pixiv_callback_accepts_https_and_app_scheme(self):
        self.assertEqual(pixiv_callback_code(QUrl(
            'https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?code=https-code&state=tail'
        )), 'https-code')
        self.assertEqual(pixiv_callback_code(QUrl(
            'pixiv://account/login?code=app-code&via=login'
        )), 'app-code')
        self.assertEqual(pixiv_callback_code(QUrl('https://www.pixiv.net/login')), '')

    def test_pixiv_oauth_diagnostic_redacts_credentials(self):
        text = sanitized_pixiv_oauth_diagnostic(
            'invalid_grant code=SECRET&via=login refresh_token: TOKENVALUE', 1
        )
        self.assertIn('invalid_grant', text)
        self.assertNotIn('SECRET', text)
        self.assertNotIn('TOKENVALUE', text)

    def test_frozen_pixiv_oauth_uses_private_pipe_input_mode(self):
        cache = self.root / 'pixiv.sqlite3'
        frozen = pixiv_oauth_command([r'C:\Program Files\SNSMediaCollector\gallery-dl.exe'], cache)
        source = pixiv_oauth_command([sys.executable, '-m', 'gallery_dl'], cache)
        self.assertIn('--smc-pixiv-stdin', frozen)
        self.assertNotIn('--smc-pixiv-stdin', source)
        self.assertEqual(frozen[-1], 'oauth:pixiv')

    def test_thumbnail_decode_is_limited_to_display_size(self):
        path = self.root / 'large-preview.png'
        self.assertTrue(QImage(2400, 1600, QImage.Format_RGB32).save(str(path)))
        pixmap = scaled_media_pixmap(path, QSize(150, 120))
        self.assertFalse(pixmap.isNull())
        self.assertLessEqual(pixmap.width(), 150)
        self.assertLessEqual(pixmap.height(), 120)

    def test_recent_thumbnail_rebuilds_are_debounced(self):
        with patch.object(self.w, 'clear_recent_grid') as clear:
            for _ in range(20):
                self.w.schedule_recent_downloads_refresh()
            limit = time.monotonic() + 1.5
            while clear.call_count == 0 and time.monotonic() < limit:
                APP.processEvents(); time.sleep(.01)
        self.assertEqual(clear.call_count, 1)

    def test_pixiv_login_dialog_captures_callback_and_uses_isolated_cache(self):
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile  # noqa: F401
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
            accepted_navigation = dialog.page.acceptNavigationRequest(
                QUrl('pixiv://account/login?code=sample-code&via=login'),
                QWebEnginePage.NavigationType.NavigationTypeOther, True,
            )
            self.assertFalse(accepted_navigation)
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

    def test_self_collection_uses_cookie_identity_without_duplicate_handle(self):
        from app import AccountProfile
        account = AccountProfile('main', 'x', user_id='123456789')
        self.w.accounts = [account]; self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        collection = self.w.collection_store.upsert(CollectionProfile(
            '自分のメディア', account.profile_id, 'x', source='media', target_scope='self',
            destination=str(self.root / 'media'),
        ))
        self.w.refresh_collections(collection.collection_id)
        url, key, _name = self.w.current_target()
        self.assertEqual(url, 'https://x.com/id:123456789/media')
        self.assertEqual(key, 'x:id:123456789')
        self.assertFalse(self.w.url_edit.isVisible())

    def test_bookmark_scan_enters_persistent_review_inbox(self):
        from app import AccountProfile
        account = AccountProfile('main', 'x')
        self.w.accounts = [account]; self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        collection = self.w.collection_store.upsert(CollectionProfile(
            '資料', account.profile_id, 'x', source='bookmarks', review_mode='inbox',
            destination=str(self.root / 'media'),
        ))
        self.w.refresh_collections(collection.collection_id)
        job = DownloadJob('bookmark', [sys.executable])
        job.smc_context = {'job_kind': 'bookmark_scan', 'collection_id': collection.collection_id}
        job.machine_lines = [
            'SMC_POST\t1234567890123456789\t42\t"artist"\t2026-01-02T03:04:05+0000\t'
            '2026-02-03T04:05:06+0000\t"memo"\t1'
        ]
        self.w.job_finished(job, 0)
        self.assertEqual(self.w.catalog.pending_collection_post_count(collection.collection_id), 1)
        self.assertIn('1件', self.w.inbox_btn.text())

    def test_likes_content_controls_are_available(self):
        from app import AccountProfile
        account = AccountProfile('main', 'x', user_id='123456789')
        self.w.accounts = [account]; self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        collection = self.w.collection_store.upsert(CollectionProfile(
            'いいね整理', account.profile_id, 'x', source='likes', target_scope='self',
            content_mode='text_images', review_mode='inbox',
            destination=str(self.root / 'media'), text_destination=str(self.root / 'text'),
        ))
        self.w.refresh_collections(collection.collection_id)
        self.assertTrue(self.w.content_mode_combo.isEnabled())
        self.assertTrue(self.w.review_checkbox.isEnabled())
        self.assertFalse(self.w.text_dest_row.isHidden())
        self.assertEqual(self.w.content_mode_combo.currentData(), 'text_images')
        self.assertTrue(self.w.review_checkbox.isChecked())
        self.w.range_buttons['all'].setChecked(True)
        self.w.sync_target_ui()
        command, _title, context = self.w.build_likes_probe(scan_all=True)
        self.assertTrue(context['scan_all'])
        self.assertEqual(context['anchor_ids'], [])
        self.assertIn('SMC_POST', '\n'.join(command))
        self.assertEqual(self.w.start_btn.text(), '⬇ いいねを確認')

    def test_likes_probe_enters_inbox_with_text_and_advances_durable_boundary(self):
        from app import AccountProfile
        account = AccountProfile('main', 'x', user_id='123456789')
        self.w.accounts = [account]; self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        collection = self.w.collection_store.upsert(CollectionProfile(
            'いいね確認', account.profile_id, 'x', source='likes', target_scope='self',
            content_mode='images', review_mode='inbox', destination=str(self.root / 'media'),
        ))
        job = DownloadJob('likes inbox', [sys.executable])
        job.smc_context = dict(
            self.ctx, collection_id=collection.collection_id,
            content_mode='images', review_mode='inbox', use_archive=False,
        )
        job.anchor_hit = OLD.post_id
        job.machine_lines = [
            f'SMC_POST\t{NEW.post_id}\t123\t"artist"\t2026-09-20T01:02:03+0000\t\t"本文メモ"\t1',
            line(NEW),
            f'SMC_POST\t{OLD.post_id}\t999\t"old"\t2026-09-01T00:00:00+0000\t\t"old"\t1',
        ]
        self.w.job_finished(job, -15)
        pending = self.w.catalog.collection_posts(collection.collection_id, state='pending')
        self.assertEqual([rec.post_id for rec in pending], [NEW.post_id])
        self.assertEqual(pending[0].content, '本文メモ')
        self.assertEqual(self.anchors()[0], NEW.post_id)
        self.assertIn('確認箱へ', job.status.text())

    def test_text_only_like_auto_saves_markdown_without_media_job(self):
        from app import AccountProfile
        account = AccountProfile('main', 'x', user_id='123456789')
        self.w.accounts = [account]; self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        text_dir = self.root / 'likes text'
        collection = self.w.collection_store.upsert(CollectionProfile(
            '文章いいね', account.profile_id, 'x', source='likes', target_scope='self',
            content_mode='text', review_mode='auto', destination=str(self.root / 'media'),
            text_destination=str(text_dir),
        ))
        job = DownloadJob('likes text', [sys.executable])
        job.smc_context = dict(
            self.ctx, collection_id=collection.collection_id,
            content_mode='text', review_mode='auto', use_archive=False,
        )
        job.anchor_hit = OLD.post_id
        job.machine_lines = [
            f'SMC_POST\t{NEW.post_id}\t123\t"writer"\t2026-09-20T01:02:03+0000\t\t"保存したい本文"\t0',
            f'SMC_POST\t{OLD.post_id}\t999\t"old"\t2026-09-01T00:00:00+0000\t\t"old"\t1',
        ]
        before_jobs = len(self.w.jobs)
        self.w.job_finished(job, -15)
        self.assertEqual(len(self.w.jobs), before_jobs)
        row = self.w.catalog.conn.execute(
            'SELECT state,choice,markdown_path FROM collection_posts WHERE collection_id=? AND post_id=?',
            (collection.collection_id, NEW.post_id),
        ).fetchone()
        self.assertEqual(row[:2], ('processed', 'text'))
        self.assertTrue(Path(row[2]).is_file())
        body = Path(row[2]).read_text(encoding='utf-8')
        self.assertIn('保存したい本文', body)
        self.assertIn('いいね取得日時', body)

    def test_likes_post_metadata_can_stop_probe_at_anchor(self):
        job = DownloadJob('likes probe', [sys.executable])
        job.smc_context = {'job_kind': 'likes_probe'}
        job.stop_on_post_ids = {OLD.post_id}
        job._consume_output_line(
            f'SMC_POST\t{OLD.post_id}\t1\t"old"\t2026-09-01T00:00:00+0000\t\t"body"\t0'
        )
        self.assertEqual(job.anchor_hit, OLD.post_id)

    def test_all_likes_review_scan_deduplicates_without_moving_incremental_anchor(self):
        from app import AccountProfile
        account = AccountProfile('main', 'x', user_id='123456789')
        self.w.accounts = [account]; self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        collection = self.w.collection_store.upsert(CollectionProfile(
            '全いいね確認', account.profile_id, 'x', source='likes', target_scope='self',
            content_mode='images', review_mode='inbox', destination=str(self.root / 'media'),
        ))
        already = PostRecord('2086200000000000000', content='already', media_count=0)
        self.w.catalog.upsert_collection_posts(collection.collection_id, [already])
        job = DownloadJob('all likes', [sys.executable])
        job.smc_context = dict(
            self.ctx, collection_id=collection.collection_id, scan_all=True,
            content_mode='images', review_mode='inbox', use_archive=False,
            date_after='2026-09-20',
        )
        older_id = '2085000000000000000'
        job.machine_lines = [
            f'SMC_POST\t{already.post_id}\t1\t"old"\t2026-09-21T00:00:00+0000\t\t"already"\t0',
            f'SMC_POST\t{NEW.post_id}\t2\t"new"\t2026-09-20T00:00:00+0000\t\t"new text"\t0',
            f'SMC_POST\t{older_id}\t3\t"older"\t2026-09-19T23:59:59+0000\t\t"too old"\t0',
        ]
        before_anchor = self.anchors()
        self.w.job_finished(job, 0)
        pending = self.w.catalog.collection_posts(collection.collection_id, state='pending')
        self.assertEqual({rec.post_id for rec in pending}, {already.post_id, NEW.post_id})
        self.assertEqual(self.anchors(), before_anchor)
        self.assertIn('確認箱へ 1投稿', job.status.text())

    def test_likes_collection_media_failure_returns_post_to_inbox(self):
        from app import AccountProfile
        account = AccountProfile('main', 'x', user_id='123456789')
        self.w.accounts = [account]; self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        collection = self.w.collection_store.upsert(CollectionProfile(
            '画像いいね', account.profile_id, 'x', source='likes', target_scope='self',
            content_mode='images', review_mode='inbox', destination=str(self.root / 'media'),
        ))
        post = PostRecord(
            NEW.post_id, NEW.author_id, NEW.author_name, NEW.post_date,
            '2026-09-22T00:00:00Z', 'body', 1,
        )
        self.w.catalog.upsert_collection_posts(collection.collection_id, [post])
        self.w.queue_collection_posts(collection, [post], {post.post_id: 'images'})
        queued = self.w.jobs[-1]
        self.assertEqual(queued.smc_context['job_kind'], 'collection_download')
        self.assertEqual(queued.smc_context['target_type'], 'likes')
        self.assertEqual(queued.smc_context['target_key'], 'x:id:123456789')
        self.w.job_finished(queued, 1)
        state = self.w.catalog.conn.execute(
            'SELECT state FROM collection_posts WHERE collection_id=? AND post_id=?',
            (collection.collection_id, post.post_id),
        ).fetchone()[0]
        self.assertEqual(state, 'pending')

    def test_likes_media_setup_error_returns_post_to_inbox(self):
        from app import AccountProfile
        account = AccountProfile('missing identity', 'x')
        self.w.accounts = [account]; self.w.save_accounts(); self.w.refresh_accounts(account.profile_id)
        collection = self.w.collection_store.upsert(CollectionProfile(
            '復旧確認', account.profile_id, 'x', source='likes', target_scope='self',
            content_mode='images', review_mode='inbox', destination=str(self.root / 'media'),
        ))
        post = PostRecord(NEW.post_id, media_count=1)
        self.w.catalog.upsert_collection_posts(collection.collection_id, [post])
        with self.assertRaisesRegex(ValueError, 'URL / ユーザー名 / ID'):
            self.w.queue_collection_posts(collection, [post], {post.post_id: 'images'})
        state = self.w.catalog.conn.execute(
            'SELECT state FROM collection_posts WHERE collection_id=? AND post_id=?',
            (collection.collection_id, post.post_id),
        ).fetchone()[0]
        self.assertEqual(state, 'pending')
if __name__ == '__main__': unittest.main(verbosity=2)
