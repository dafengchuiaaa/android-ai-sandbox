#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for departments/antigravity/phone.py using offline FakeRunner.

Zero network, zero ADB subprocess, purely standard library.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

# Setup paths
HERE = Path(__file__).resolve().parent
ANTIGRAVITY_DIR = HERE.parent
PROJECT_ROOT = ANTIGRAVITY_DIR.parent.parent

if str(ANTIGRAVITY_DIR) not in sys.path:
    sys.path.insert(0, str(ANTIGRAVITY_DIR))

import phone  # noqa: E402

FIXTURES_DIR = PROJECT_ROOT / "departments" / "deepseek" / "fixtures" / "originals"
SEARCH_RESULTS_XML = FIXTURES_DIR / "hierarchy-search-results.xml"
EMPTY_SEARCH_XML = FIXTURES_DIR / "hierarchy-20261015T024231-c934f0c7.xml"


class FakeRunner:
    """Mock runner that returns pre-configured responses and records calls."""

    def __init__(self, handler=None):
        self.calls = []
        self.handler = handler if handler is not None else self._default_handler
        self.xml_data = SEARCH_RESULTS_XML.read_bytes()
        self.png_data = b"\x89PNG fake png data"
        self.boot_completed = "1"
        self.avd_name = "ai-sandbox-baseline"
        self.devices_output = "List of devices attached\nemulator-5554\tdevice\n"
        self.current_text = "termux"
        self.fail_clear = False
        self.structure_change = False
        self.input_text_calls = []

    def _render_xml(self):
        text_str = self.current_text
        import xml.etree.ElementTree as ET
        try:
            tree = ET.fromstring(self.xml_data)
        except Exception:
            return self.xml_data
        for n in tree.iter("node"):
            cls = n.get("class") or ""
            if "EditText" in cls:
                if self.structure_change:
                    n.set("class", "android.widget.ImageView")
                else:
                    n.set("text", text_str)
        return ET.tostring(tree, encoding="utf-8")

    def run(self, cmd, timeout):
        self.calls.append({"cmd": list(cmd), "timeout": timeout})
        return self.handler(cmd)

    def _default_handler(self, cmd):
        adb, dash_s, serial, *tail = cmd
        assert dash_s == "-s" and serial == "emulator-5554"

        if tail == ["devices"]:
            return 0, self.devices_output, ""
        if tail == ["shell", "getprop", "sys.boot_completed"]:
            return 0, f"{self.boot_completed}\n", ""
        if tail == ["emu", "avd", "name"]:
            return 0, f"{self.avd_name}\r\nOK\r\n", ""
        if tail == ["shell", "wm", "size"]:
            return 0, "Physical size: 720x1280\n", ""
        if tail[0] == "shell" and tail[1] == "monkey":
            return 0, ":Monkey: seed=0 count=1\nEvents injected: 1\n", ""
        if tail[:3] == ["shell", "uiautomator", "dump"]:
            return 0, f"UI hierchary dumped to: {tail[3]}\n", ""
        if tail[:2] == ["shell", "screencap"]:
            return 0, "", ""
        if tail[:3] == ["shell", "input", "tap"]:
            return 0, "", ""
        if tail[:3] == ["shell", "input", "keyevent"]:
            if self.fail_clear:
                return 1, "", "input: send keyevent failed"
            # When DEL or FORWARD_DEL keyevents are sent, clear text
            if "67" in tail[3:] or "112" in tail[3:]:
                self.current_text = ""
            return 0, "", ""
        if tail[:3] == ["shell", "input", "text"]:
            val = tail[3] if len(tail) > 3 else ""
            self.input_text_calls.append(val)
            self.current_text = val
            return 0, "", ""
        if tail == ["shell", "rm", tail[-1]]:
            return 0, "", ""
        if tail[0] == "pull":
            remote, local = tail[1], tail[2]
            os.makedirs(os.path.dirname(local), exist_ok=True)
            if local.endswith(".png"):
                data = self.png_data
            else:
                data = self._render_xml()
            with open(local, "wb") as fh:
                fh.write(data)
            return 0, "1 file pulled.\n", ""
        raise AssertionError(f"Unexpected adb command: {cmd}")



class PhoneCLITest(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="test_phone_")
        self.runtime_dir = os.path.join(
            PROJECT_ROOT, "departments", "antigravity", "runtime", "test-session"
        )
        os.makedirs(self.runtime_dir, exist_ok=True)

        self.state_dir = os.path.join(
            PROJECT_ROOT, "departments", "workbuddy", "runtime", "antigravity"
        )
        os.makedirs(self.state_dir, exist_ok=True)
        self.state_file = os.path.join(self.state_dir, "test-instance.json")
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump({
                "pid": 1234,
                "serial": "emulator-5554",
                "port": 5554,
                "avd": "ai-sandbox-baseline",
            }, fh)

        # Dummy adb.exe path
        self.dummy_adb = os.path.join(self.test_dir, "adb.exe")
        with open(self.dummy_adb, "w") as fh:
            fh.write("fake-adb")

        self.runner = FakeRunner()

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)
        if os.path.exists(self.runtime_dir):
            shutil.rmtree(self.runtime_dir, ignore_errors=True)
        if os.path.exists(self.state_file):
            try:
                os.remove(self.state_file)
            except OSError:
                pass

    def run_cli(self, args_list, runner=None):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = phone.main(args_list, runner=runner if runner is not None else self.runner)
        out_str = out.getvalue()
        lines = [ln for ln in out_str.splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1, f"Expected exactly 1 line on stdout, got {len(lines)}: {out_str!r}")
        payload = json.loads(lines[0])
        return code, payload, err.getvalue()

    def test_output_dir_boundary_escape_rejected(self):
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.test_dir,  # outside departments/antigravity/runtime
            "status",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_output_path")

    def test_state_file_escape_rejected(self):
        outside_state = os.path.join(self.test_dir, "instance.json")
        with open(outside_state, "w") as f:
            json.dump({"serial": "emulator-5554", "avd": "ai-sandbox-baseline"}, f)

        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", outside_state,
            "--output-dir", self.runtime_dir,
            "status",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ownership_mismatch")

    def test_avd_ownership_mismatch_in_state_file(self):
        bad_state = os.path.join(self.state_dir, "bad-instance.json")
        with open(bad_state, "w") as f:
            json.dump({"serial": "emulator-5554", "avd": "wrong-avd"}, f)
        try:
            code, payload, _ = self.run_cli([
                "--adb", self.dummy_adb,
                "--state-file", bad_state,
                "--output-dir", self.runtime_dir,
                "status",
            ])
            self.assertEqual(code, 1)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["error"]["code"], "ownership_mismatch")
        finally:
            if os.path.exists(bad_state):
                os.remove(bad_state)

    def test_live_avd_ownership_mismatch_on_device(self):
        self.runner.avd_name = "rogue-avd"
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "status",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "ownership_mismatch")

    def test_device_offline(self):
        self.runner.devices_output = "List of devices attached\nemulator-5554\toffline\n"
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "status",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "device_unavailable")

    def test_device_not_booted(self):
        self.runner.boot_completed = "0"
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "status",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "device_unavailable")

    def test_status_success(self):
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "status",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "status")
        self.assertTrue(payload["data"]["ready"])
        self.assertEqual(payload["data"]["device_serial"], "emulator-5554")
        self.assertEqual(payload["data"]["avd"], "ai-sandbox-baseline")

    def test_launch_disallowed_package(self):
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "launch", "--package", "com.android.chrome",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_argument")

    def test_launch_and_observe_success(self):
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "launch", "--package", "org.fdroid.basic",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["package"], "org.fdroid.basic")
        after = payload["data"]["after_observation"]
        self.assertTrue(after["observation_id"])
        self.assertGreater(len(after["nodes"]), 0)
        self.assertEqual(after["coverage"], "partial")
        self.assertTrue(os.path.isfile(after["xml_path"]))

    def test_observe_and_click(self):
        # 1. observe
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        self.assertEqual(code, 0)
        obs_id = payload["data"]["observation_id"]
        nodes = payload["data"]["nodes"]

        # Find a clickable node
        clickable_nodes = [n for n in nodes if "click" in n["actions"]]
        self.assertTrue(clickable_nodes, "Expected at least one clickable node")
        target_node = clickable_nodes[0]

        # 2. click
        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "click", "--observation-id", obs_id, "--node-id", target_node["node_id"],
        ])
        self.assertEqual(code2, 0)
        self.assertTrue(payload2["ok"])
        self.assertEqual(payload2["data"]["plan"]["action"], "tap")
        self.assertIn("after_observation", payload2["data"])

    def test_click_stale_observation_detected(self):
        # 1. observe with search results
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        obs_id = payload["data"]["observation_id"]
        node_id = payload["data"]["nodes"][0]["node_id"]

        # 2. Device UI changes to empty search
        self.runner.xml_data = EMPTY_SEARCH_XML.read_bytes()

        # 3. click with old obs_id
        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "click", "--observation-id", obs_id, "--node-id", node_id,
        ])
        self.assertEqual(code2, 1)
        self.assertFalse(payload2["ok"])
        self.assertEqual(payload2["error"]["code"], "stale_observation")

    def test_evaluate_search(self):
        # 1. observe
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        obs_id = payload["data"]["observation_id"]

        # 2. evaluate-search for termux
        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "evaluate-search", "--observation-id", obs_id, "--query", "termux",
        ])
        self.assertEqual(code2, 0)
        self.assertTrue(payload2["ok"])
        data = payload2["data"]
        self.assertEqual(data["query"], "termux")
        self.assertEqual(data["device_serial"], "emulator-5554")
        self.assertIn("content_verified", data)

    def test_screenshot_requires_reason(self):
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "screenshot", "--reason", "diagnostic check on search empty state",
        ])
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["reason"], "diagnostic check on search empty state")
    def test_set_text_invalid_characters(self):
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "set-text", "--observation-id", "0" * 64, "--node-id", "n:1", "--value", "has space",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "invalid_argument")

    def test_set_text_unsupported_target_on_non_edittext(self):
        # 1. observe
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        obs_id = payload["data"]["observation_id"]
        # Find a non-EditText node
        non_et = [n for n in payload["data"]["nodes"] if not (n["class"] or "").endswith("EditText")][0]

        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "set-text", "--observation-id", obs_id, "--node-id", non_et["node_id"], "--value", "termux",
        ])
        self.assertEqual(code2, 1)
        self.assertFalse(payload2["ok"])
        self.assertEqual(payload2["error"]["code"], "unsupported_target")

    def test_set_text_success_and_verification_failed(self):
        # 1. observe
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        obs_id = payload["data"]["observation_id"]
        # Find the EditText node
        et = [n for n in payload["data"]["nodes"] if (n["class"] or "").endswith("EditText")][0]

        # 2. set-text to "termux"
        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "set-text", "--observation-id", obs_id, "--node-id", et["node_id"], "--value", "termux",
        ])
        self.assertEqual(code2, 0)
        self.assertTrue(payload2["ok"])
        self.assertEqual(payload2["data"]["input_text"], "termux")
        self.assertEqual(payload2["data"]["readback_text"], "termux")
        self.assertTrue(payload2["data"]["verified"])

    def test_set_text_200_chars_then_termux(self):
        """Dispatch 26: Write 200 chars, then write termux; verify clearing removes 200 chars."""
        # 1. observe
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        obs_id = payload["data"]["observation_id"]
        et = [n for n in payload["data"]["nodes"] if (n["class"] or "").endswith("EditText")][0]

        # 2. write 200 chars
        long_val = "A" * 200
        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "set-text", "--observation-id", obs_id, "--node-id", et["node_id"], "--value", long_val,
        ])
        self.assertEqual(code2, 0)
        self.assertTrue(payload2["ok"])
        self.assertEqual(payload2["data"]["readback_text"], long_val)

        # 3. Next action: write "termux"
        after_obs_id = payload2["data"]["after_observation"]["observation_id"]
        after_node_id = et["node_id"].split(":")[1]
        next_node_id = f"{after_obs_id[:12]}:{after_node_id}"

        code3, payload3, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "set-text", "--observation-id", after_obs_id, "--node-id", next_node_id, "--value", "termux",
        ])
        self.assertEqual(code3, 0)
        self.assertTrue(payload3["ok"])
        self.assertEqual(payload3["data"]["input_text"], "termux")
        self.assertEqual(payload3["data"]["readback_text"], "termux")
        # Ensure no concatenated text
        self.assertNotIn("A", payload3["data"]["readback_text"])

    def test_set_text_clear_failure_stops_input_text(self):
        """Dispatch 26: If clearing keyevents fail, input_text must NEVER be sent."""
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        obs_id = payload["data"]["observation_id"]
        et = [n for n in payload["data"]["nodes"] if (n["class"] or "").endswith("EditText")][0]

        self.runner.fail_clear = True
        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "set-text", "--observation-id", obs_id, "--node-id", et["node_id"], "--value", "termux",
        ])
        self.assertEqual(code2, 1)
        self.assertFalse(payload2["ok"])
        self.assertEqual(payload2["error"]["code"], "driver_timeout")
        # Critical verification: input_text was NEVER called
        self.assertEqual(self.runner.input_text_calls, [])

    def test_set_text_empty_string(self):
        """Dispatch 26: set-text to empty string clears the box and verifies empty."""
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        obs_id = payload["data"]["observation_id"]
        et = [n for n in payload["data"]["nodes"] if (n["class"] or "").endswith("EditText")][0]

        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "set-text", "--observation-id", obs_id, "--node-id", et["node_id"], "--value", "",
        ])
        self.assertEqual(code2, 0)
        self.assertTrue(payload2["ok"])
        self.assertEqual(payload2["data"]["input_text"], "")
        self.assertEqual(payload2["data"]["readback_text"], "")
        self.assertTrue(payload2["data"]["verified"])

    def test_set_text_structural_change_rejected(self):
        """Dispatch 26: If input box structure changes, it must fail rather than mistaking another node."""
        code, payload, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "observe",
        ])
        obs_id = payload["data"]["observation_id"]
        et = [n for n in payload["data"]["nodes"] if (n["class"] or "").endswith("EditText")][0]

        self.runner.structure_change = True
        code2, payload2, _ = self.run_cli([
            "--adb", self.dummy_adb,
            "--state-file", self.state_file,
            "--output-dir", self.runtime_dir,
            "set-text", "--observation-id", obs_id, "--node-id", et["node_id"], "--value", "termux",
        ])
        self.assertEqual(code2, 1)
        self.assertFalse(payload2["ok"])
        self.assertEqual(payload2["error"]["code"], "unsupported_target")


if __name__ == "__main__":
    unittest.main()


