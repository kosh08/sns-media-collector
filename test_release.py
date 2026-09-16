import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import build_release
import windows_instance


class ReleaseTests(unittest.TestCase):
    def test_version_rejects_path_and_preprocessor_input(self):
        self.assertEqual(build_release.release_version('0.3.0\n'), '0.3.0')
        for value in ('../test', '0.3.0/other', '0.3.0\n#anything', 'v0.3.0'):
            with self.assertRaises(ValueError): build_release.release_version(value)

    def test_failed_or_stale_self_test_cannot_approve_bundle(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'report.json'
            path.write_text('{"success":true,"frozen":true}')
            with patch('build_release.checked'):
                with self.assertRaises(FileNotFoundError): build_release.self_test(Path('app.exe'), path)
            def write_failed(*args): path.write_text('{"success":false,"frozen":true}')
            with patch('build_release.checked', side_effect=write_failed):
                with self.assertRaises(RuntimeError): build_release.self_test(Path('app.exe'), path)

    def test_installer_only_targets_versioned_application_files(self):
        s=(build_release.ROOT / 'installer/setup.iss').read_text(encoding='utf-8')
        self.assertIn('PrivilegesRequired=lowest', s)
        self.assertIn('AppId=SNSMediaCollector.MasterTools.Desktop', s)
        self.assertIn('AppMutex=' + windows_instance.MUTEX_NAME, s)
        self.assertIn('DestDir: "{app}\\versions\\{#AppVersion}"', s)
        self.assertNotIn('[UninstallDelete]', s)
        self.assertNotIn('[InstallDelete]', s)

    def test_windows_builder_refuses_non_windows(self):
        with patch('build_release.sys.platform', 'linux'):
            with self.assertRaisesRegex(RuntimeError, 'Windows is required'): build_release.main()

if __name__ == '__main__': unittest.main()
