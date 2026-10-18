from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from integration import check_delivery
import sandbox


class AcceptanceGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.root = Path(self.temp.name)
        for path in check_delivery.COMPONENTS.values():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("SYNTHETIC fixture", encoding="utf-8")
        (self.root / "integration").mkdir()
        self.digest = hashlib.sha256(b"SYNTHETIC fixture").hexdigest()

    def tearDown(self):
        self.temp.cleanup()

    def receipt(self):
        receipt = {name: {"result": "accepted", "sha256": self.digest} for name in ("phone_cli", "search_scenario")}
        receipt["product_gates"] = {name: True for name in ("live_ai_demo", "application_refresh", "human_foreground_input")}
        (self.root / "integration/acceptance.json").write_text(json.dumps(receipt), encoding="utf-8")

    def test_files_alone_are_not_acceptance(self):
        result = check_delivery.inspect(self.root)
        self.assertTrue(all(item["present"] for item in result["components"].values()))
        self.assertFalse(result["ready_for_product_acceptance"])

    def test_source_change_invalidates_previous_acceptance(self):
        self.receipt()
        with patch.object(check_delivery, "ADAPTER_HASH", self.digest):
            self.assertTrue(check_delivery.inspect(self.root)["ready_for_product_acceptance"])
            (self.root / check_delivery.COMPONENTS["phone_cli"]).write_text("NEW UNREVIEWED REVISION", encoding="utf-8")
            self.assertFalse(check_delivery.inspect(self.root)["ready_for_product_acceptance"])

    def test_missing_dependency_cannot_be_ready(self):
        self.receipt()
        (self.root / check_delivery.COMPONENTS["driver"]).unlink()
        with patch.object(check_delivery, "ADAPTER_HASH", self.digest):
            self.assertFalse(check_delivery.inspect(self.root)["ready_for_device_integration"])

    def test_invalid_state_does_not_spawn_backend(self):
        state = self.root / "bad-state.json"
        state.write_text(json.dumps({"avd": "foreign", "serial": "emulator-5554"}), encoding="utf-8")
        backend = self.root / check_delivery.COMPONENTS["phone_cli"]
        stdout = io.StringIO()
        with patch.object(sandbox, "PHONE", backend), patch.object(sandbox.check_delivery, "inspect", return_value={"accepted_revisions": {"phone_cli": True}}), patch.object(sandbox.subprocess, "Popen") as spawn, redirect_stdout(stdout):
            code = sandbox.forward(["--adb", "fake-adb", "--state-file", str(state), "--output-dir", "unused", "observe"])
        spawn.assert_not_called()
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"], "invalid_argument")

    def test_unicode_backend_uses_utf8_even_with_legacy_host_encoding(self):
        state = self.root / "state.json"
        state.write_text(json.dumps({"avd": "ai-sandbox-baseline", "serial": "emulator-5554"}), encoding="utf-8")
        backend = self.root / "unicode-backend.py"
        payload = {"ok": True, "action": "status", "data": {"ready": True, "device_serial": "emulator-5554", "avd": "ai-sandbox-baseline", "label": "中文🙂"}, "error": None, "artifacts": []}
        backend.write_text("import json\npayload = " + repr(payload) + "\nprint(json.dumps(payload, ensure_ascii=False))\n", encoding="utf-8")
        stdout = io.StringIO()
        with patch.object(sandbox, "PHONE", backend), patch.object(sandbox, "LOG_ROOT", self.root / "logs"), patch.object(sandbox.check_delivery, "inspect", return_value={"accepted_revisions": {"phone_cli": True}}), patch.dict(os.environ, {"PYTHONIOENCODING": "cp936"}), redirect_stdout(stdout):
            code = sandbox.forward(["--adb", "unused", "--state-file", str(state), "--output-dir", "unused", "status"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())["data"]["label"], "中文🙂")


if __name__ == "__main__":
    unittest.main()
