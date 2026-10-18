from contextlib import redirect_stderr
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("phone_dump_under_test", ROOT / "departments/antigravity/phone.py")
phone = importlib.util.module_from_spec(spec)
spec.loader.exec_module(phone)


class ReadOnlyDriver:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def run(self, argv, timeout):
        self.calls.append((argv, timeout))
        if argv[0] == "pull":
            Path(argv[2]).write_bytes(b"<hierarchy/>")
            return 0, "1 file pulled", ""
        if argv[:2] == ["shell", "rm"]:
            return 0, "", ""
        return next(self.results)

    def pull(self, remote, local):
        Path(local).write_bytes(b"<hierarchy/>")

    def remove_remote(self, remote):
        pass


class DumpRetryTests(unittest.TestCase):
    def controller(self, folder, results):
        controller = object.__new__(phone.PhoneController)
        controller.output_dir = folder
        controller.adb = ReadOnlyDriver(results)
        return controller

    def test_transient_read_is_logged_and_retried_once(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            ctl = self.controller(folder, [(137, "idle transition", ""), (0, "done", "")])
            stderr = io.StringIO()
            with patch.object(phone.time, "sleep"), redirect_stderr(stderr):
                data, _ = ctl.dump_hierarchy_bytes()
            self.assertEqual(data, b"<hierarchy/>")
            dump_calls = [c for c in ctl.adb.calls if c[0][:3] == ["shell", "uiautomator", "dump"]]
            self.assertEqual(len(dump_calls), 2)
            self.assertIn('"returncode": 137', stderr.getvalue())
            # Protective assertion: zero input commands were executed
            self.assertTrue(all(c[0][0] != "shell" or c[0][1] != "input" for c in ctl.adb.calls))

    def test_persistent_failure_stops_after_two_reads(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            ctl = self.controller(folder, [(1, "first failure", ""), (2, "second failure", "")])
            with patch.object(phone.time, "sleep"), redirect_stderr(io.StringIO()), self.assertRaises(phone.PhoneError) as caught:
                ctl.dump_hierarchy_bytes()
            self.assertEqual(caught.exception.code, "driver_timeout")
            self.assertEqual(caught.exception.detail["stdout"], "second failure")
            dump_calls = [c for c in ctl.adb.calls if c[0][:3] == ["shell", "uiautomator", "dump"]]
            self.assertEqual(len(dump_calls), 2)
            # Protective assertion: zero input commands were executed
            self.assertTrue(all(c[0][0] != "shell" or c[0][1] != "input" for c in ctl.adb.calls))

    def test_input_with_trailing_newline_is_rejected_before_device(self):
        ctl = object.__new__(phone.PhoneController)
        with self.assertRaises(phone.PhoneError) as caught:
            ctl.cmd_set_text("unused", "unused", "termux\n")
        self.assertEqual(caught.exception.code, "invalid_argument")

    def test_same_focused_input_may_expand_after_clear_icon_disappears(self):
        def observation(right=608, focused="true", left=112, resource="search", package="org.fdroid.basic"):
            xml = ('<hierarchy><node class="android.widget.EditText" package="%s" resource-id="%s" '
                   'enabled="true" focused="%s" bounds="[%d,56][%d,168]"/></hierarchy>') % (package, resource, focused, left, right)
            return phone.ca.Observation(xml.encode())
        before = observation()
        original = before.items[1]
        ctl = object.__new__(phone.PhoneController)
        expanded = observation(right=704)
        result = ctl._find_target_in_observation(expanded, original, [112, 56, 608, 168], allow_focused_resize=True)
        self.assertEqual(result.path, original.path)
        for altered in (observation(right=704, focused="false"), observation(right=704, left=116),
                        observation(right=704, resource="other"), observation(right=704, package="foreign")):
            with self.assertRaises(phone.PhoneError):
                ctl._find_target_in_observation(altered, original, [112, 56, 608, 168], allow_focused_resize=True)
        with self.assertRaises(phone.PhoneError):
            ctl._find_target_in_observation(expanded, original, [112, 56, 608, 168])


if __name__ == "__main__":
    unittest.main()
