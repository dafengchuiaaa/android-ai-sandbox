#!/usr/bin/env python3
"""Tests for adb_control.py using a fake ADB runner. No real ADB is invoked."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import adb_control  # noqa: E402

SERIAL = "emulator-5554"
PNG_BYTES = b"\x89PNG fake image data"
XML_BYTES = b"<hierarchy/>"


class FakeRunner:
    """Records every command and dispatches to a handler.

    The handler receives the full command list (argv passed to the runner,
    which in production would go to subprocess with shell=False) and returns
    (returncode, stdout, stderr), or raises adb_control.AdbTimeout /
    adb_control.AdbNotFound to simulate the corresponding failure.
    """

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler if handler is not None else make_default_handler()

    def run(self, cmd, timeout):
        self.calls.append({"cmd": list(cmd), "timeout": timeout})
        return self.handler(cmd)


def make_default_handler(serial=SERIAL, boot="1", wm_size="Physical size: 720x1280\n",
                         devices_fail=False, screencap_fail=False,
                         dump_fail=False, pull_fail=False, pulled_bytes=None):
    def handler(cmd):
        adb, dash_s, bound_serial, *tail = cmd
        assert adb == "adb.exe" and dash_s == "-s" and bound_serial == serial
        if tail == ["devices"]:
            if devices_fail:
                return 1, "", "adb: failed to list devices"
            return 0, "List of devices attached\n%s\tdevice\n" % serial, ""
        if tail == ["shell", "getprop", "sys.boot_completed"]:
            return 0, boot + "\n", ""
        if tail == ["shell", "wm", "size"]:
            return 0, wm_size, ""
        if tail == ["shell", "screencap", "-p", cmd[-1]] and cmd[-1].startswith("/sdcard/Download/"):
            if screencap_fail:
                return 1, "", "screencap: failed"
            return 0, "", ""
        if tail == ["shell", "uiautomator", "dump", cmd[-1]] and cmd[-1].startswith("/sdcard/Download/"):
            if dump_fail:
                return 1, "", "ERROR: could not get idle state"
            return 0, "UI hierchary dumped to: %s\n" % cmd[-1], ""
        if tail[0] == "pull" and len(tail) == 3:
            if pull_fail:
                return 1, "", "adb: error: failed to stat remote object"
            local = tail[2]
            os.makedirs(os.path.dirname(local), exist_ok=True)
            data = PNG_BYTES if local.endswith(".png") else XML_BYTES
            with open(local, "wb") as fh:
                fh.write(pulled_bytes if pulled_bytes is not None else data)
            return 0, "1 file pulled, 0 skipped.\n", ""
        if tail == ["shell", "rm", cmd[-1]] and cmd[-1].startswith("/sdcard/Download/"):
            return 0, "", ""
        if tail == ["shell", "input", "tap", tail[-2], tail[-1]]:
            return 0, "", ""
        if tail == ["shell", "input", "text", tail[-1]]:
            return 0, "", ""
        raise AssertionError("unexpected command: %r" % cmd)

    return handler


class CLITestBase(unittest.TestCase):
    def run_cli(self, argv, runner):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = adb_control.main(list(argv), runner=runner)
        return code, out.getvalue(), err.getvalue()

    def parse_single_json(self, stdout):
        lines = [ln for ln in stdout.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, "stdout must be exactly one JSON line: %r" % stdout)
        payload = json.loads(lines[0])
        self.assertEqual(set(payload), {"ok", "action", "serial", "artifact", "error"})
        return payload

    def cli_base(self, serial=SERIAL, output_dir=None):
        # NOTE: explicit `is not None` (not `or`) so an empty string reaches
        # the CLI and can be rejected as output_dir_missing.
        if output_dir is None:
            output_dir = self.tmpdir
        return ["--adb", "adb.exe", "--serial", serial,
                "--output-dir", output_dir]

    def setUp(self):
        # WB-02: output dirs must stay inside the approved artifact roots, so the
        # default test output dir is a unique subdirectory of departments/zcode
        # (test-artifacts/), removed after each test. System temp dirs are used
        # only to construct paths that MUST be rejected.
        self.test_root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test-artifacts")
        self.tmpdir = os.path.join(self.test_root, "t-" + uuid.uuid4().hex[:12])
        os.makedirs(self.tmpdir, exist_ok=True)

        def _cleanup():
            shutil.rmtree(self.tmpdir, ignore_errors=True)

        self.addCleanup(_cleanup)

    def assert_serial_binding(self, runner):
        self.assertTrue(runner.calls, "expected at least one ADB call")
        for call in runner.calls:
            self.assertEqual(call["cmd"][:3], ["adb.exe", "-s", SERIAL],
                             "every device command must be bound to the serial: %r" % call["cmd"])


class SerialValidationTest(CLITestBase):
    def test_missing_serial_is_usage_error(self):
        runner = FakeRunner()
        argv = ["--adb", "adb.exe", "--output-dir", self.tmpdir, "status"]
        code, out, _ = self.run_cli(argv, runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "usage")
        self.assertEqual(runner.calls, [], "no ADB call may happen without a serial")

    def test_empty_serial_rejected(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base(serial="") + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "serial_missing")
        self.assertEqual(runner.calls, [])

    def test_odd_port_serial_rejected(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base(serial="emulator-5555") + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "serial_invalid")
        self.assertEqual(runner.calls, [])

    def test_physical_device_serial_rejected(self):
        for bad in ["9A3200FFAZ00XY", "device-123", "emulator-5554a", "emulator-", "emulator-0"]:
            runner = FakeRunner()
            code, out, _ = self.run_cli(self.cli_base(serial=bad) + ["status"], runner)
            payload = self.parse_single_json(out)
            self.assertEqual(payload["error"], "serial_invalid", "serial %r" % bad)
            self.assertEqual(code, 1)
            self.assertEqual(runner.calls, [])

    def test_adb_path_missing(self):
        runner = FakeRunner()
        argv = ["--adb", "Z:/definitely/missing/adb.exe", "--serial", SERIAL,
                "--output-dir", self.tmpdir, "status"]
        code, out, _ = self.run_cli(argv, runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "adb_not_found")
        self.assertEqual(runner.calls, [])

    def test_adb_not_found_from_runner(self):
        def handler(cmd):
            raise adb_control.AdbNotFound("cannot execute")
        runner = FakeRunner(handler)
        code, out, _ = self.run_cli(self.cli_base() + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "adb_not_found")
        self.assertEqual(code, 1)


class StatusTest(CLITestBase):
    def test_status_ok(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "status")
        self.assertEqual(payload["serial"], SERIAL)
        self.assertIsNone(payload["artifact"])
        self.assertIsNone(payload["error"])
        self.assert_serial_binding(runner)

    def test_status_not_booted(self):
        runner = FakeRunner(make_default_handler(boot="0"))
        code, out, _ = self.run_cli(self.cli_base() + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "not_booted")

    def test_serial_not_connected(self):
        def devices_only(cmd):
            if cmd[3:] == ["devices"]:
                return 0, "List of devices attached\nemulator-9999\tdevice\n", ""
            return make_default_handler()(cmd)
        runner = FakeRunner(devices_only)
        code, out, _ = self.run_cli(self.cli_base() + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "device_not_connected")
        self.assertEqual(code, 1)

    def test_serial_offline(self):
        def handler(cmd):
            if cmd[3:] == ["devices"]:
                return 0, "List of devices attached\n%s\toffline\n" % SERIAL, ""
            return make_default_handler()(cmd)
        runner = FakeRunner(handler)
        code, out, _ = self.run_cli(self.cli_base() + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "device_offline")
        self.assertEqual(code, 1)

    def test_adb_devices_fails(self):
        runner = FakeRunner(make_default_handler(devices_fail=True))
        code, out, _ = self.run_cli(self.cli_base() + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "adb_error")
        self.assertEqual(code, 1)


class ScreenshotTest(CLITestBase):
    def test_screenshot_ok(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["screenshot"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        artifact = payload["artifact"]
        self.assertTrue(os.path.isabs(artifact))
        self.assertTrue(artifact.startswith(self.tmpdir))
        self.assertTrue(artifact.endswith(".png"))
        with open(artifact, "rb") as fh:
            self.assertEqual(fh.read(), PNG_BYTES)
        # exact temp file created by the tool gets cleaned up: one screencap, one rm
        screencap_targets = [c["cmd"][-1] for c in runner.calls if c["cmd"][4:6] == ["screencap", "-p"]]
        rm_targets = [c["cmd"][-1] for c in runner.calls if c["cmd"][4:6] == ["rm", c["cmd"][-1]]]
        self.assertEqual(len(screencap_targets), 1)
        self.assertEqual(rm_targets, screencap_targets)
        self.assertTrue(screencap_targets[0].startswith("/sdcard/Download/ai-sandbox-screenshot-"))
        self.assert_serial_binding(runner)

    def test_screencap_failure(self):
        runner = FakeRunner(make_default_handler(screencap_fail=True))
        code, out, _ = self.run_cli(self.cli_base() + ["screenshot"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "adb_error")
        self.assertEqual(code, 1)
        # cleanup still attempted for the temp file the tool created
        self.assertTrue(any(c["cmd"][4:6] == ["rm", c["cmd"][-1]] for c in runner.calls))

    def test_pull_failure(self):
        runner = FakeRunner(make_default_handler(pull_fail=True))
        code, out, _ = self.run_cli(self.cli_base() + ["screenshot"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "adb_error")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_pulled_file_empty_is_output_failure(self):
        runner = FakeRunner(make_default_handler(pulled_bytes=b""))
        code, out, _ = self.run_cli(self.cli_base() + ["screenshot"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "output_failed")
        self.assertEqual(code, 1)

    def test_output_dir_is_a_file(self):
        blocker = os.path.join(self.tmpdir, "not-a-dir")
        with open(blocker, "w") as fh:
            fh.write("x")
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base(output_dir=blocker) + ["screenshot"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "output_failed")
        self.assertEqual(code, 1)


class HierarchyTest(CLITestBase):
    def test_hierarchy_ok(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["hierarchy"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        artifact = payload["artifact"]
        self.assertTrue(artifact.endswith(".xml"))
        with open(artifact, "rb") as fh:
            self.assertEqual(fh.read(), XML_BYTES)
        dump_targets = [c["cmd"][-1] for c in runner.calls if c["cmd"][4] == "uiautomator"]
        rm_targets = [c["cmd"][-1] for c in runner.calls if c["cmd"][4:6] == ["rm", c["cmd"][-1]]]
        self.assertEqual(rm_targets, dump_targets)
        self.assert_serial_binding(runner)

    def test_dump_failure(self):
        runner = FakeRunner(make_default_handler(dump_fail=True))
        code, out, _ = self.run_cli(self.cli_base() + ["hierarchy"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "adb_error")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])


class TapTest(CLITestBase):
    def test_tap_ok(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["tap", "--x", "100", "--y", "200"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertIsNone(payload["artifact"])
        tap_calls = [c["cmd"] for c in runner.calls if c["cmd"][4:6] == ["input", "tap"]]
        self.assertEqual(tap_calls, [["adb.exe", "-s", SERIAL, "shell", "input", "tap", "100", "200"]])
        self.assert_serial_binding(runner)

    def test_tap_zero_zero_ok(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["tap", "--x", "0", "--y", "0"], runner)
        self.assertEqual(json.loads(out)["error"], None)
        self.assertEqual(code, 0)

    def test_tap_out_of_bounds(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["tap", "--x", "720", "--y", "1280"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "invalid_coords")
        self.assertEqual(code, 1)
        self.assertFalse(any(c["cmd"][4:6] == ["input", "tap"] for c in runner.calls))

    def test_tap_negative(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["tap", "--x", "-5", "--y", "100"], runner)
        self.assertEqual(json.loads(out)["error"], "invalid_coords")
        self.assertEqual(code, 1)

    def test_tap_non_integer(self):
        for x, y in [("abc", "1"), ("1.5", "2"), ("", "3")]:
            runner = FakeRunner()
            code, out, _ = self.run_cli(self.cli_base() + ["tap", "--x", x, "--y", y], runner)
            payload = self.parse_single_json(out)
            self.assertEqual(payload["error"], "invalid_coords", "(%r, %r)" % (x, y))
            self.assertEqual(code, 1)

    def test_tap_uses_override_size(self):
        runner = FakeRunner(make_default_handler(
            wm_size="Physical size: 720x1280\nOverride size: 600x1200\n"))
        code, out, _ = self.run_cli(self.cli_base() + ["tap", "--x", "650", "--y", "50"], runner)
        self.assertEqual(json.loads(out)["error"], "invalid_coords")
        self.assertEqual(code, 1)

    def test_wm_size_unparseable(self):
        runner = FakeRunner(make_default_handler(wm_size="garbage"))
        code, out, _ = self.run_cli(self.cli_base() + ["tap", "--x", "1", "--y", "2"], runner)
        self.assertEqual(json.loads(out)["error"], "adb_error")
        self.assertEqual(code, 1)


class TextTest(CLITestBase):
    def test_text_ok(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["text", "--value", "termux"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        text_calls = [c["cmd"] for c in runner.calls if c["cmd"][4:6] == ["input", "text"]]
        self.assertEqual(text_calls, [["adb.exe", "-s", SERIAL, "shell", "input", "text", "termux"]])
        self.assert_serial_binding(runner)

    def test_unsupported_text_rejected_before_adb(self):
        for bad in ["termux world", "termux;ls", "termux&ls", "你好", "term-ux", "term_ux",
                    "café", "100%", ""]:
            runner = FakeRunner()
            code, out, _ = self.run_cli(self.cli_base() + ["text", "--value", bad], runner)
            payload = self.parse_single_json(out)
            self.assertEqual(payload["error"], "unsupported_text", "value %r" % bad)
            self.assertEqual(code, 1)
            self.assertEqual([c for c in runner.calls if c["cmd"][4:6] == ["input", "text"]], [],
                             "no input text call may happen for %r" % bad)

    def test_overlong_text_rejected(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base() + ["text", "--value", "a" * 201], runner)
        self.assertEqual(json.loads(out)["error"], "unsupported_text")
        self.assertEqual(code, 1)


class TimeoutAndContractTest(CLITestBase):
    def test_timeout(self):
        def handler(cmd):
            raise adb_control.AdbTimeout("timed out")
        runner = FakeRunner(handler)
        code, out, _ = self.run_cli(self.cli_base() + ["screenshot"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "adb_timeout")
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])

    def test_failure_never_reports_ok_true(self):
        cases = [
            (["status"], make_default_handler(devices_fail=True)),
            (["screenshot"], make_default_handler(screencap_fail=True)),
            (["hierarchy"], make_default_handler(dump_fail=True)),
            (["tap", "--x", "720", "--y", "0"], None),
            (["text", "--value", "你好"], None),
        ]
        for argv_tail, handler in cases:
            runner = FakeRunner(handler) if handler else FakeRunner()
            code, out, _ = self.run_cli(self.cli_base() + argv_tail, runner)
            payload = self.parse_single_json(out)
            self.assertFalse(payload["ok"], "argv %r" % argv_tail)
            self.assertTrue(payload["error"], "failure must carry an error code")
            self.assertEqual(code, 1)

    def test_every_call_binds_serial_across_actions(self):
        runner = FakeRunner()
        for tail in (["status"], ["screenshot"], ["hierarchy"],
                     ["tap", "--x", "10", "--y", "20"], ["text", "--value", "termux"]):
            code, out, _ = self.run_cli(self.cli_base() + tail, runner)
            self.assertEqual(code, 0, tail)
        self.assert_serial_binding(runner)

    def test_help_exits_zero(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            with self.assertRaises(SystemExit) as ctx:
                adb_control.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("--serial", out.getvalue())
        self.assertIn("status", out.getvalue())


class OutputBoundaryTest(CLITestBase):
    """WB-02 review item 4: output-dir containment, validated before device calls."""

    def _outside_temp(self):
        path = tempfile.mkdtemp(prefix="adbctl-outside-")
        self.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
        return path

    def test_outside_project_rejected_without_device_calls(self):
        outside = self._outside_temp()
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base(output_dir=outside) + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "output_dir_rejected")
        self.assertEqual(code, 1)
        self.assertEqual(runner.calls, [],
                         "no ADB call may happen for a rejected output dir")

    def test_empty_output_dir_rejected(self):
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base(output_dir="") + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "output_dir_missing")
        self.assertEqual(runner.calls, [])

    def test_prefix_sibling_root_rejected_at_unit_level(self):
        # departments/zcode-evil must NOT pass a plain StartsWith("departments/zcode")
        root = adb_control.APPROVED_OUTPUT_ROOTS[0]
        sibling = root + "-evil"
        self.assertEqual(adb_control.check_output_dir(sibling), "output_dir_rejected")
        self.assertEqual(adb_control.check_output_dir(root + "x"), "output_dir_rejected")
        # the root itself and a normal child are accepted
        self.assertIsNone(adb_control.check_output_dir(root))
        self.assertIsNone(adb_control.check_output_dir(os.path.join(root, "artifacts")))

    def _make_symlink_or_skip(self, link, target, target_is_directory=True):
        """Create a symlink, or skip if this environment neutralizes link creation.

        Verified 2026-10-15: the execution sandbox here turns os.symlink into a
        plain non-reparse entry (os.path.islink is False afterwards), so the
        link-escape path cannot be exercised end-to-end in-sandbox. The boundary
        logic itself is covered by the unit-level tests below.
        """
        # Public snapshot portability fix (2026-10-18): lack of Windows
        # symlink privilege is an unavailable test prerequisite, not a PASS.
        try:
            os.symlink(target, link, target_is_directory=target_is_directory)
        except OSError as exc:
            if isinstance(exc, PermissionError) or getattr(exc, "winerror", None) == 1314:
                self.skipTest("symlink creation requires unavailable OS privilege")
            raise
        if not os.path.islink(link):
            self.skipTest("sandbox neutralizes symlink creation (islink=False after "
                          "os.symlink); link-escape rejection not exercisable here")

    def test_symlink_escape_rejected(self):
        outside = self._outside_temp()
        link = os.path.join(self.tmpdir, "escape-link")
        self._make_symlink_or_skip(link, outside)
        self.assertEqual(adb_control.check_output_dir(link), "output_dir_rejected")
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base(output_dir=link) + ["status"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(payload["error"], "output_dir_rejected")
        self.assertEqual(runner.calls, [])

    def test_file_symlink_to_dir_escape_rejected(self):
        # Windows "file" symlink (no target_is_directory) pointing at a directory:
        # realpath alone may not follow it, the explicit islink pass must catch it.
        outside = self._outside_temp()
        link = os.path.join(self.tmpdir, "escape-file-link")
        self._make_symlink_or_skip(link, outside, target_is_directory=False)
        self.assertEqual(adb_control.check_output_dir(link), "output_dir_rejected")

    def test_output_validated_before_device_calls_for_every_action(self):
        outside = self._outside_temp()
        for tail in (["status"], ["screenshot"], ["hierarchy"],
                     ["tap", "--x", "1", "--y", "2"], ["text", "--value", "termux"]):
            runner = FakeRunner()
            code, out, _ = self.run_cli(self.cli_base(output_dir=outside) + tail, runner)
            payload = self.parse_single_json(out)
            self.assertEqual(payload["error"], "output_dir_rejected", "action %r" % tail)
            self.assertEqual(code, 1)
            self.assertEqual(runner.calls, [], "action %r must not touch the device" % tail)

    def test_workbuddy_integration_root_accepted(self):
        integ_root = adb_control.APPROVED_OUTPUT_ROOTS[1]
        sub = os.path.join(integ_root, "cli-test-" + uuid.uuid4().hex[:8])
        os.makedirs(sub, exist_ok=True)
        self.addCleanup(lambda: shutil.rmtree(sub, ignore_errors=True))
        runner = FakeRunner()
        code, out, _ = self.run_cli(self.cli_base(output_dir=sub) + ["screenshot"], runner)
        payload = self.parse_single_json(out)
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["artifact"].startswith(integ_root),
                        "artifact must land inside the integration root: %r" % payload["artifact"])


if __name__ == "__main__":
    unittest.main()
