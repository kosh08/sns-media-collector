from pathlib import Path
import base64
import ctypes
import subprocess
import sys
import tempfile
import unittest

from windows_taskbar import APP_ID, relaunch_executable, window_values


class TaskbarTests(unittest.TestCase):
    def test_relaunch_target_is_stable_across_updates_and_cleanup(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "installed 日本語"
            root.mkdir()
            stable = root / "SNSMediaCollector.exe"
            stable.write_bytes(b"launcher")
            values = []
            for version in ("0.4.14", "0.4.15", "0.4.16"):
                executable = root / "versions" / version / "SNSMediaCollector.exe"
                executable.parent.mkdir(parents=True)
                executable.write_bytes(b"application")
                self.assertEqual(relaunch_executable(executable), stable)
                values.append(window_values(executable))
            self.assertEqual(values[0], values[1])
            self.assertEqual(values[1], values[2])
            self.assertEqual(values[0]["command"], '"' + str(stable) + '"')
            self.assertNotIn("versions", values[0]["icon"])

    def test_missing_launcher_does_not_create_a_broken_relaunch_command(self):
        with tempfile.TemporaryDirectory() as td:
            executable = Path(td) / "versions" / "0.4.15" / "SNSMediaCollector.exe"
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"app")
            self.assertIsNone(relaunch_executable(executable))
            self.assertEqual(window_values(executable), {"id": APP_ID})
            self.assertIsNone(relaunch_executable(Path(sys.executable)))

    def test_installer_shortcuts_have_matching_identity_and_stable_target(self):
        source = (Path(__file__).parent / "installer/setup.iss").read_text(encoding="utf-8")
        icons = source.split("[Icons]", 1)[1].split("[Run]", 1)[0]
        shortcuts = [line for line in icons.splitlines() if line.startswith("Name:")]
        self.assertEqual(len(shortcuts), 2)
        for line in shortcuts:
            self.assertIn(f'AppUserModelID: "{APP_ID}"', line)
            self.assertIn('Filename: "{app}\\SNSMediaCollector.exe"', line)
            self.assertNotIn("versions", line)

    @unittest.skipUnless(sys.platform == "win32", "Windows Shell API required")
    def test_native_shortcut_identity_and_target_survive_version_cleanup(self):
        from windows_taskbar import _property_store, _write, _method, shortcut_metadata
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "installed 日本語"
            root.mkdir()
            stable = root / "SNSMediaCollector.exe"
            stable.write_bytes(b"stable launcher")
            link = root / "sample-pin.lnk"
            literal = lambda path: "'" + str(path).replace("'", "''") + "'"
            script = "$s=(New-Object -ComObject WScript.Shell).CreateShortcut(" + literal(link) + ");$s.TargetPath=" + literal(stable) + ";$s.Save()"
            encoded = base64.b64encode(script.encode("utf-16-le")).decode()
            result = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                    capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
            with _property_store(path=link, flags=2) as store:  # GPS_READWRITE, temporary shortcut only
                _write(store, 5, APP_ID)
                self.assertEqual(_method(store, 7, ctypes.c_int32)(store), 0)
            metadata = shortcut_metadata(link)
            self.assertEqual(metadata["id"], APP_ID)
            self.assertTrue(Path(metadata["target"]).samefile(stable))
            before = link.read_bytes()
            stable.write_bytes(b"updated stable launcher")
            self.assertEqual(link.read_bytes(), before)
            self.assertEqual(shortcut_metadata(link), metadata)

    @unittest.skipUnless(sys.platform == "win32", "Windows Shell API required")
    def test_native_shell_properties_roundtrip_and_cleanup(self):
        # A separate process keeps the developer's Qt process identity unchanged.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "installed 日本語"
            root.mkdir()
            stable = root / "SNSMediaCollector.exe"
            stable.write_bytes(b"launcher")
            executable = root / "versions" / "0.4.15" / stable.name
            executable.parent.mkdir(parents=True)
            executable.write_bytes(b"app")
            code = "from pathlib import Path;from windows_taskbar import native_self_test;print(native_self_test(Path(" + repr(str(executable)) + ")))"
            result = subprocess.run([sys.executable, "-c", code], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(APP_ID, result.stdout)
