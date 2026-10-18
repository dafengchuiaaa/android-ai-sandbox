# !/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline counterfactual tests for Task 83-r2: pre_input_read raw XML retention & forensic diff.

All synthetic mutations are explicitly labelled SYNTHETIC and never treated as live evidence.
All real subprocess / socket / network / ADB calls are strictly blocked.
"""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

THIS_FILE = Path(__file__).resolve()
PROJECT_ROOT = THIS_FILE.parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent
ANTIGRAVITY_DIR = PROJECT_ROOT / "departments" / "antigravity"
INTEGRATION_DIR = PROJECT_ROOT / "integration"

for p in (str(PROJECT_ROOT), str(ANTIGRAVITY_DIR), str(INTEGRATION_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import phone  # noqa: E402
from tests import test_phone  # noqa: E402
import xml_diff_forensics as forensics  # noqa: E402

READINESS_XML_PATH = WORKSPACE_ROOT / "evidence" / "observations" / "hierarchy-20261018T022724-ebef74bc.xml"
LAUNCH_XML_PATH = WORKSPACE_ROOT / "evidence" / "observations" / "hierarchy-20261018T022720-9fbffd7a.xml"
EXPECTED_READINESS_HASH = "ebef74bc0e8168546f94b6c166d265190d17c5bc55e203f92c73c970eab8d60e"
SEARCH_NODE_ID = "ebef74bc0e81:R.0.0.0.0.0.0.0.0.0.1.0.1.0.0"


class PreClickForensicsTests(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory(prefix="task83-forensics-")
        self.tmp = Path(self.tmp_obj.name).resolve()
        self.approved_out = self.tmp / "departments" / "antigravity" / "runtime"
        self.approved_state = self.tmp / "departments" / "workbuddy" / "runtime"
        self.session_dir = self.approved_out / "session-task83"
        self.state_dir = self.approved_state / "round4"
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.state_file = self.state_dir / "instance.json"
        self.state_file.write_text(
            json.dumps({
                "pid": 1234,
                "serial": "emulator-5554",
                "port": 5554,
                "avd": "ai-sandbox-baseline",
            }),
            encoding="utf-8",
        )
        self.dummy_adb = self.tmp / "adb.exe"
        self.dummy_adb.write_text("fake-adb", encoding="utf-8")

        self.runner = test_phone.FakeRunner()
        if READINESS_XML_PATH.is_file():
            self.base_xml_bytes = READINESS_XML_PATH.read_bytes()
        else:
            self.base_xml_bytes = test_phone.SEARCH_RESULTS_XML.read_bytes()

        # Guard against any real subprocess or network execution during offline tests
        self._popen_patch = patch.object(
            subprocess, "Popen", side_effect=AssertionError("Real subprocess.Popen forbidden in offline test")
        )
        self._run_patch = patch.object(
            subprocess, "run", side_effect=AssertionError("Real subprocess.run forbidden in offline test")
        )
        self._sock_patch = patch.object(
            socket, "create_connection", side_effect=AssertionError("Network forbidden in offline test")
        )
        self._out_root_patch = patch.object(phone, "APPROVED_OUTPUT_ROOT", str(self.approved_out))
        self._state_root_patch = patch.object(phone, "APPROVED_STATE_ROOT", str(self.approved_state))

        self._popen_patch.start()
        self._run_patch.start()
        self._sock_patch.start()
        self._out_root_patch.start()
        self._state_root_patch.start()

    def tearDown(self):
        self._state_root_patch.stop()
        self._out_root_patch.stop()
        self._sock_patch.stop()
        self._run_patch.stop()
        self._popen_patch.stop()
        self.tmp_obj.cleanup()

    def _run_phone_cli(self, argv: list[str]) -> tuple[int, dict, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = phone.main(argv, runner=self.runner)
        lines = [ln for ln in out.getvalue().splitlines() if ln.strip()]
        self.assertEqual(len(lines), 1)
        return rc, json.loads(lines[0]), err.getvalue()

    def _install_raw_pull_runner(self, raw_bytes_Getter):
        """Configure FakeRunner so pull writes exact raw bytes without ElementTree re-serialization."""
        orig_handler = self.runner._default_handler

        def custom_handler(cmd):
            adb, dash_s, serial, *tail = cmd
            if tail and tail[0] == "pull":
                remote, local = tail[1], tail[2]
                os.makedirs(os.path.dirname(local), exist_ok=True)
                with open(local, "wb") as fh:
                    fh.write(raw_bytes_Getter())
                return 0, "1 file pulled.\n", ""
            return orig_handler(cmd)

        self.runner.handler = custom_handler

    def test_1_readiness_xml_copy_preserves_exact_bytes_and_success_path_writes_zero_mismatch_files(self):
        """Case 1: Exact copy of readiness XML keeps bytes identical;
        click success path does NOT write any pre-input-mismatch files or extra reads.
        """
        current_bytes_box = [self.base_xml_bytes]
        self._install_raw_pull_runner(lambda: current_bytes_box[0])

        rc1, obs_payload, _ = self._run_phone_cli([
            "--adb", str(self.dummy_adb),
            "--state-file", str(self.state_file),
            "--output-dir", str(self.session_dir),
            "observe",
        ])
        self.assertEqual(rc1, 0)
        obs_id = obs_payload["data"]["observation_id"]
        if READINESS_XML_PATH.is_file():
            self.assertEqual(obs_id, EXPECTED_READINESS_HASH)

        # Compare readiness XML with an exact byte copy
        copy_path = self.tmp / "copy-readiness.xml"
        copy_path.write_bytes(self.base_xml_bytes)
        rep = forensics.compare_xml_files(
            expected_xml_path=Path(obs_payload["data"]["xml_path"]),
            current_xml_path=copy_path,
            expected_hash=obs_id,
            current_hash=obs_id,
            requested_node_id=SEARCH_NODE_ID if READINESS_XML_PATH.is_file() else obs_payload["data"]["nodes"][1]["node_id"],
        )
        self.assertTrue(rep["ok"])
        self.assertTrue(rep["hash_verified"])
        self.assertTrue(rep["bytes_identical"])
        self.assertEqual(rep["classification"], "identical_bytes")
        self.assertEqual(rep["target_and_ancestor_report"]["overall_chain_verdict"], "target_and_ancestor_chain_unchanged")

        # Now run click when UI is unchanged: verify 0 mismatch files written
        node_to_click = SEARCH_NODE_ID if READINESS_XML_PATH.is_file() else [
            n["node_id"] for n in obs_payload["data"]["nodes"] if "click" in n["actions"]
        ][0]
        rc2, click_payload, _ = self._run_phone_cli([
            "--adb", str(self.dummy_adb),
            "--state-file", str(self.state_file),
            "--output-dir", str(self.session_dir),
            "click", "--observation-id", obs_id, "--node-id", node_to_click,
        ])
        self.assertEqual(rc2, 0, click_payload)
        self.assertEqual(list(self.session_dir.glob("pre-input-mismatch-*")), [])

    def test_2_synthetic_non_target_attribute_change_saves_raw_bytes_and_proves_target_chain_unchanged(self):
        """Case 2 (SYNTHETIC): Modify a non-target card title ('Immich' -> 'SYNTHETIC_App_Update')
        plus raw XML trailing whitespace.
        Verify:
        - cmd_click rejects with stale_observation at pre_input_read
        - zero tap / zero set-text / zero extra read after mismatch
        - original observation XML and metadata untouched
        - raw current_bytes saved verbatim (not re-serialized) with matching SHA-256
        - forensics tool reports non-target attribute diff while Search target & clickable parent are unchanged.
        """
        assert b"Immich" in self.base_xml_bytes or b"Termux" in self.base_xml_bytes
        target_token = b"Immich" if b"Immich" in self.base_xml_bytes else b"Termux"
        synthetic_current_bytes = (
            self.base_xml_bytes.replace(target_token, b"SYNTHETIC_NON_TARGET_ATTR", 1) + b"<!-- SYNTHETIC_RAW_COMMENT -->"
        )
        synthetic_hash = hashlib.sha256(synthetic_current_bytes).hexdigest()

        current_bytes_box = [self.base_xml_bytes]
        self._install_raw_pull_runner(lambda: current_bytes_box[0])

        rc1, obs_payload, _ = self._run_phone_cli([
            "--adb", str(self.dummy_adb),
            "--state-file", str(self.state_file),
            "--output-dir", str(self.session_dir),
            "observe",
        ])
        self.assertEqual(rc1, 0)
        obs_id = obs_payload["data"]["observation_id"]
        orig_xml_path = Path(obs_payload["data"]["xml_path"])
        orig_meta_path = self.session_dir / f"observation-{obs_id[:12]}.json"
        orig_xml_before = orig_xml_path.read_bytes()
        orig_meta_before = orig_meta_path.read_bytes()

        # Switch live dump bytes to synthetic_current_bytes before click
        current_bytes_box[0] = synthetic_current_bytes
        self.runner.calls.clear()

        req_node_id = SEARCH_NODE_ID if READINESS_XML_PATH.is_file() else obs_payload["data"]["nodes"][1]["node_id"]
        rc2, click_err, _ = self._run_phone_cli([
            "--adb", str(self.dummy_adb),
            "--state-file", str(self.state_file),
            "--output-dir", str(self.session_dir),
            "click", "--observation-id", obs_id, "--node-id", req_node_id,
        ])
        self.assertEqual(rc2, 1)
        self.assertFalse(click_err["ok"])
        self.assertEqual(click_err["error"]["code"], "stale_observation")
        detail = click_err["error"]["detail"]
        self.assertEqual(detail["expected_observation_id"], obs_id)
        self.assertEqual(detail["current_hash"], synthetic_hash)
        self.assertEqual(detail["stage"], "pre_input_read")
        self.assertFalse(detail["click_stage"]["input_attempted"])
        self.assertFalse(detail["click_stage"]["input_send"]["requested"])

        # Zero tap, zero keyevent/text, and exactly 1 dump attempt during click
        tap_calls = [c for c in self.runner.calls if c["cmd"][3:6] == ["shell", "input", "tap"]]
        text_calls = [c for c in self.runner.calls if c["cmd"][3:6] == ["shell", "input", "text"]]
        dump_calls = [c for c in self.runner.calls if c["cmd"][3:6] == ["shell", "uiautomator", "dump"]]
        self.assertEqual(tap_calls, [])
        self.assertEqual(text_calls, [])
        self.assertEqual(len(dump_calls), 1, "Must not perform any extra read after pre_input_read mismatch!")

        # Original observation files must be completely untouched
        self.assertEqual(orig_xml_path.read_bytes(), orig_xml_before)
        self.assertEqual(orig_meta_path.read_bytes(), orig_meta_before)

        # Verify saved mismatch evidence
        ev = detail["pre_input_mismatch_evidence"]
        self.assertIsInstance(ev, dict)
        saved_cur_xml = Path(ev["current_xml_path"])
        saved_meta_json = Path(ev["evidence_meta_path"])
        self.assertTrue(saved_cur_xml.is_file())
        self.assertTrue(saved_meta_json.is_file())
        # Raw bytes including <!-- SYNTHETIC_RAW_COMMENT --> must be preserved byte-for-byte
        self.assertEqual(saved_cur_xml.read_bytes(), synthetic_current_bytes)
        self.assertEqual(hashlib.sha256(saved_cur_xml.read_bytes()).hexdigest(), synthetic_hash)

        # Run offline forensics tool via evidence_meta
        diff_rep = forensics.compare_from_evidence_meta(saved_meta_json)
        self.assertTrue(diff_rep["ok"])
        self.assertTrue(diff_rep["hash_verified"])
        self.assertEqual(diff_rep["classification"], "structural_or_attribute_diff")
        self.assertEqual(diff_rep["summary_counts"]["attribute_or_text_diffs"], 1)
        self.assertFalse(diff_rep["attribute_or_text_diffs"][0]["identity_confirmed"])
        self.assertEqual(
            diff_rep["target_and_ancestor_report"]["overall_chain_verdict"],
            "target_and_ancestor_chain_unchanged",
        )

    def test_3_synthetic_target_or_clickable_ancestor_modification_detected(self):
        """Case 3 (SYNTHETIC): Modify the clickable ancestor bounds/enabled state of Search."""
        exp_path = self.tmp / "exp-base.xml"
        exp_path.write_bytes(self.base_xml_bytes)
        exp_hash = hashlib.sha256(self.base_xml_bytes).hexdigest()

        # Mutate the clickable parent of Search: bounds="[246,1072][475,1232]" -> clickable="false"
        mutated_bytes = self.base_xml_bytes.replace(
            b'clickable="true" enabled="true" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[246,1072][475,1232]"',
            b'clickable="false" enabled="false" focusable="true" focused="false" scrollable="false" long-clickable="false" password="false" selected="false" bounds="[246,1072][475,1232]"',
            1,
        )
        cur_path = self.tmp / "cur-SYNTHETIC-ancestor-changed.xml"
        cur_path.write_bytes(mutated_bytes)
        cur_hash = hashlib.sha256(mutated_bytes).hexdigest()

        rep = forensics.compare_xml_files(
            expected_xml_path=exp_path,
            current_xml_path=cur_path,
            expected_hash=exp_hash,
            current_hash=cur_hash,
            requested_node_id=f"{exp_hash[:12]}:R.0.0.0.0.0.0.0.0.0.1.0.1.0.0",
        )
        self.assertTrue(rep["ok"])
        self.assertEqual(
            rep["target_and_ancestor_report"]["overall_chain_verdict"],
            "target_or_ancestor_changed",
        )
        ancestor_entry = rep["target_and_ancestor_report"]["nodes"][-1]
        self.assertEqual(ancestor_entry["status"], "changed")
        self.assertIn("clickable", ancestor_entry["attribute_diffs"])

    def test_4_synthetic_duplicate_anchors_node_add_remove_and_order_shift(self):
        """Case 4 (SYNTHETIC): Duplicate semantic anchors, added/removed nodes, and sibling order shift."""
        xml_a = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<hierarchy rotation="0">'
            b'<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="org.fdroid.basic" bounds="[0,0][720,1280]" clickable="false" enabled="true">'
            b'<node index="0" text="ItemA" resource-id="org.fdroid.basic:id/item_a" class="android.widget.TextView" package="org.fdroid.basic" bounds="[0,100][720,200]" clickable="true" enabled="true"/>'
            b'<node index="1" text="ItemB" resource-id="org.fdroid.basic:id/item_b" class="android.widget.TextView" package="org.fdroid.basic" bounds="[0,200][720,300]" clickable="true" enabled="true"/>'
            b'<node index="2" text="DupLabel" resource-id="org.fdroid.basic:id/dup" class="android.widget.TextView" package="org.fdroid.basic" bounds="[0,300][720,400]" clickable="true" enabled="true"/>'
            b'</node></hierarchy>'
        )
        # In xml_b (SYNTHETIC):
        # - ItemB moves R.0.1 -> R.0.0 (order_or_index_changes)
        # - ItemA is removed, ItemC is added
        # - DupLabel appears twice (ambiguous_anchors)
        xml_b = (
            b'<?xml version="1.0" encoding="UTF-8"?>'
            b'<hierarchy rotation="0">'
            b'<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="org.fdroid.basic" bounds="[0,0][720,1280]" clickable="false" enabled="true">'
            b'<node index="0" text="ItemB" resource-id="org.fdroid.basic:id/item_b" class="android.widget.TextView" package="org.fdroid.basic" bounds="[0,200][720,300]" clickable="true" enabled="true"/>'
            b'<node index="1" text="ItemC_SYNTHETIC" resource-id="org.fdroid.basic:id/item_c" class="android.widget.TextView" package="org.fdroid.basic" bounds="[0,250][720,350]" clickable="true" enabled="true"/>'
            b'<node index="2" text="DupLabel" resource-id="org.fdroid.basic:id/dup" class="android.widget.TextView" package="org.fdroid.basic" bounds="[0,300][720,400]" clickable="true" enabled="true"/>'
            b'<node index="3" text="DupLabel" resource-id="org.fdroid.basic:id/dup" class="android.widget.TextView" package="org.fdroid.basic" bounds="[0,400][720,500]" clickable="true" enabled="true"/>'
            b'</node></hierarchy>'
        )
        pa = self.tmp / "syn-a.xml"
        pb = self.tmp / "syn-b.xml"
        pa.write_bytes(xml_a)
        pb.write_bytes(xml_b)
        ha = hashlib.sha256(xml_a).hexdigest()
        hb = hashlib.sha256(xml_b).hexdigest()

        # Request node_id pointing to DupLabel in xml_a (R.0.2)
        rep = forensics.compare_xml_files(
            expected_xml_path=pa,
            current_xml_path=pb,
            expected_hash=ha,
            current_hash=hb,
            requested_node_id=f"{ha[:12]}:R.0.2",
        )
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["summary_counts"]["order_or_index_changes"], 1)  # ItemB moved R.0.1 -> R.0.0
        self.assertEqual(rep["summary_counts"]["removed_nodes"], 1)         # ItemA removed
        self.assertEqual(rep["summary_counts"]["added_nodes"], 1)           # ItemC added
        self.assertEqual(rep["summary_counts"]["ambiguous_anchors"], 1)     # DupLabel duplicated
        self.assertEqual(
            rep["target_and_ancestor_report"]["overall_chain_verdict"],
            "target_or_ancestor_ambiguous_or_missing",
        )

    def test_5_raw_byte_only_diff_distinguished_from_structural_diff(self):
        """Case 5 (SYNTHETIC): XML declaration / trailing newline difference with identical tree."""
        pa = self.tmp / "raw-a.xml"
        pb = self.tmp / "raw-b.xml"
        pa.write_bytes(self.base_xml_bytes)
        pb.write_bytes(self.base_xml_bytes + b"\n   \n")

        rep = forensics.compare_xml_files(
            expected_xml_path=pa,
            current_xml_path=pb,
            expected_hash=hashlib.sha256(pa.read_bytes()).hexdigest(),
            current_hash=hashlib.sha256(pb.read_bytes()).hexdigest(),
        )
        self.assertTrue(rep["ok"])
        self.assertFalse(rep["bytes_identical"])
        self.assertTrue(rep["raw_byte_only_diff"])
        self.assertEqual(rep["classification"], "raw_byte_only_diff")

    def test_6_tampered_xml_file_with_old_hash_rejected(self):
        """Case 6 (SYNTHETIC): Tamper with XML file on disk while keeping old hash in metadata."""
        pa = self.tmp / "tamper-a.xml"
        pb = self.tmp / "tamper-b.xml"
        pa.write_bytes(self.base_xml_bytes)
        pb.write_bytes(self.base_xml_bytes + b"<!-- tampered -->")
        old_hash = hashlib.sha256(self.base_xml_bytes).hexdigest()

        rep = forensics.compare_xml_files(
            expected_xml_path=pa,
            current_xml_path=pb,
            expected_hash=old_hash,
            current_hash=old_hash,  # Lies that pb has old_hash
        )
        self.assertFalse(rep["ok"])
        self.assertFalse(rep["hash_verified"])
        self.assertEqual(rep["error_code"], "hash_verification_failed")

    def test_7_evidence_write_failure_still_blocks_input_and_preserves_stale_observation(self):
        """Case 7 (SYNTHETIC): If saving pre-input mismatch evidence raises OSError,
        cmd_click must STILL reject with stale_observation, send 0 taps, and report the evidence error.
        """
        current_bytes_box = [self.base_xml_bytes]
        self._install_raw_pull_runner(lambda: current_bytes_box[0])

        rc1, obs_payload, _ = self._run_phone_cli([
            "--adb", str(self.dummy_adb),
            "--state-file", str(self.state_file),
            "--output-dir", str(self.session_dir),
            "observe",
        ])
        self.assertEqual(rc1, 0)
        obs_id = obs_payload["data"]["observation_id"]

        current_bytes_box[0] = self.base_xml_bytes + b"<!-- force mismatch -->"
        self.runner.calls.clear()

        with patch.object(
            phone.PhoneController,
            "_save_pre_input_mismatch_evidence",
            side_effect=OSError("Simulated disk full during forensic capture"),
        ):
            rc2, click_err, _ = self._run_phone_cli([
                "--adb", str(self.dummy_adb),
                "--state-file", str(self.state_file),
                "--output-dir", str(self.session_dir),
                "click", "--observation-id", obs_id, "--node-id", obs_payload["data"]["nodes"][1]["node_id"],
            ])

        self.assertEqual(rc2, 1)
        self.assertFalse(click_err["ok"])
        self.assertEqual(click_err["error"]["code"], "stale_observation")
        detail = click_err["error"]["detail"]
        self.assertIsNone(detail["pre_input_mismatch_evidence"])
        self.assertIn("Simulated disk full", detail["pre_input_mismatch_evidence_error"])
        self.assertFalse(detail["click_stage"]["input_attempted"])
        tap_calls = [c for c in self.runner.calls if c["cmd"][3:6] == ["shell", "input", "tap"]]
        self.assertEqual(tap_calls, [])

    def test_8_missing_or_invalid_declared_hashes_and_bad_evidence_meta_refused(self):
        """Case 8 (SYNTHETIC counterfactual from review/independent_checks.py):
        - Calling compare_xml_files without expected_hash or current_hash (or with malformed hashes)
          must set hash_verified=False, ok=False, error_code='missing_or_invalid_declared_hash'.
        - Calling compare_from_evidence_meta on missing or malformed metadata files must return
          structured error_code='invalid_evidence_meta' without raising uncaught exceptions.
        """
        pa = self.tmp / "hash-a.xml"
        pb = self.tmp / "hash-b.xml"
        pa.write_bytes(self.base_xml_bytes)
        pb.write_bytes(self.base_xml_bytes)
        valid_h = hashlib.sha256(self.base_xml_bytes).hexdigest()

        for exp_h, cur_h in (
            (None, None),
            (valid_h, None),
            (None, valid_h),
            ("short", valid_h),
            (valid_h, "UPPERCASE" + "0" * 55),
        ):
            rep = forensics.compare_xml_files(pa, pb, expected_hash=exp_h, current_hash=cur_h)
            self.assertFalse(rep["ok"])
            self.assertFalse(rep["hash_verified"])
            self.assertTrue(rep["hash_computed"])
            self.assertEqual(rep["error_code"], "missing_or_invalid_declared_hash")
            self.assertEqual(rep["expected_xml"]["actual_sha256"], valid_h)
            self.assertEqual(rep["current_xml"]["actual_sha256"], valid_h)

        # Missing evidence-meta file
        rep_missing = forensics.compare_from_evidence_meta(self.tmp / "nonexistent-meta.json")
        self.assertFalse(rep_missing["ok"])
        self.assertFalse(rep_missing["hash_verified"])
        self.assertEqual(rep_missing["error_code"], "invalid_evidence_meta")

        # Malformed JSON and incomplete JSON object in evidence-meta
        bad_meta = self.tmp / "bad-meta.json"
        bad_meta.write_text("{not valid json", encoding="utf-8")
        rep_bad_json = forensics.compare_from_evidence_meta(bad_meta)
        self.assertFalse(rep_bad_json["ok"])
        self.assertFalse(rep_bad_json["hash_verified"])
        self.assertEqual(rep_bad_json["error_code"], "invalid_evidence_meta")

        incomplete_meta = self.tmp / "incomplete-meta.json"
        incomplete_meta.write_text(json.dumps({"expected_xml_path": str(pa)}), encoding="utf-8")
        rep_inc = forensics.compare_from_evidence_meta(incomplete_meta)
        self.assertFalse(rep_inc["ok"])
        self.assertFalse(rep_inc["hash_verified"])
        self.assertEqual(rep_inc["error_code"], "invalid_evidence_meta")

    def test_9_synthetic_xml_element_text_and_tag_changes_never_classified_as_raw_byte_only(self):
        """Case 9 (SYNTHETIC counterfactuals from review/independent_checks.py):
        1) Changing XML element.text ('>original<' -> '>SYNTHETIC_CHANGED<') while Android 'text'
           attribute stays 'Search' must be classified as 'structural_or_attribute_diff', NOT 'raw_byte_only_diff'.
        2) Changing XML element tag ('<node ...>' -> '<other ...>') must be classified as
           'structural_or_attribute_diff' and flagged in unsupported_xml_elements, NOT 'raw_byte_only_diff'.
        """
        a = b'<hierarchy><node package="org.fdroid.basic" class="android.widget.TextView" text="Search">original</node></hierarchy>'
        b_text = a.replace(b">original<", b">SYNTHETIC_CHANGED<")
        pa = self.tmp / "elem-a.xml"
        pb_text = self.tmp / "elem-b-text.xml"
        pa.write_bytes(a)
        pb_text.write_bytes(b_text)

        rep_text = forensics.compare_xml_files(
            pa,
            pb_text,
            expected_hash=hashlib.sha256(a).hexdigest(),
            current_hash=hashlib.sha256(b_text).hexdigest(),
        )
        self.assertTrue(rep_text["ok"])
        self.assertFalse(rep_text["raw_byte_only_diff"])
        self.assertEqual(rep_text["classification"], "structural_or_attribute_diff")
        self.assertEqual(rep_text["summary_counts"]["attribute_or_text_diffs"], 1)
        self.assertEqual(
            rep_text["attribute_or_text_diffs"][0]["attribute_diffs"]["__element_text__"],
            {"expected": "original", "current": "SYNTHETIC_CHANGED"},
        )

        b_tag = a.replace(b"<node ", b"<other ").replace(b"</node>", b"</other>")
        pb_tag = self.tmp / "elem-b-tag.xml"
        pb_tag.write_bytes(b_tag)
        rep_tag = forensics.compare_xml_files(
            pa,
            pb_tag,
            expected_hash=hashlib.sha256(a).hexdigest(),
            current_hash=hashlib.sha256(b_tag).hexdigest(),
        )
        self.assertTrue(rep_tag["ok"])
        self.assertFalse(rep_tag["raw_byte_only_diff"])
        self.assertEqual(rep_tag["classification"], "structural_or_attribute_diff")
        self.assertGreaterEqual(rep_tag["summary_counts"]["unsupported_xml_elements"], 1)

    def test_10_synthetic_target_replacement_at_same_path_and_class_reports_unconfirmed(self):
        """Case 10 (SYNTHETIC counterfactual from review/independent_checks.py):
        When the target's resource-id and text anchor disappear and are replaced by different
        values at the same path/class/package, target report must report status='unconfirmed'
        (never claiming unique correspondence from path/class alone) and list candidate_evidence.
        """
        a = (
            b'<hierarchy><node package="org.fdroid.basic" class="android.widget.Button" '
            b'resource-id="org.fdroid.basic:id/old" text="Search" enabled="true" clickable="true" '
            b'bounds="[0,0][100,100]"/></hierarchy>'
        )
        b = a.replace(b":id/old", b":id/SYNTHETIC_replacement").replace(b'text="Search"', b'text="Replacement"')
        pa = self.tmp / "rep-a.xml"
        pb = self.tmp / "rep-b.xml"
        pa.write_bytes(a)
        pb.write_bytes(b)
        ha = hashlib.sha256(a).hexdigest()
        hb = hashlib.sha256(b).hexdigest()
        target_node_id = phone.ca.Observation(a).items[1].node_id

        rep = forensics.compare_xml_files(
            pa,
            pb,
            expected_hash=ha,
            current_hash=hb,
            requested_node_id=target_node_id,
        )
        self.assertTrue(rep["ok"])
        self.assertEqual(
            rep["target_and_ancestor_report"]["overall_chain_verdict"],
            "target_or_ancestor_ambiguous_or_missing",
        )
        entry = rep["target_and_ancestor_report"]["nodes"][0]
        self.assertEqual(entry["status"], "unconfirmed")
        self.assertEqual(entry["match_basis"], "semantic_anchor_missing_cannot_confirm_from_path_and_class")
        self.assertIsNone(entry["current"])
        self.assertIsInstance(entry["candidate_evidence"], dict)
        self.assertFalse(entry["candidate_evidence"]["identity_confirmed"])
        self.assertEqual(
            entry["candidate_evidence"]["same_path_candidate"]["resource_id"],
            "org.fdroid.basic:id/SYNTHETIC_replacement",
        )
        # Global diff must also record unconfirmed replacement rather than confirmed attribute diff
        self.assertEqual(rep["summary_counts"]["attribute_or_text_diffs"], 0)
        self.assertEqual(rep["summary_counts"]["unconfirmed_replacements"], 1)
        self.assertEqual(rep["summary_counts"]["removed_nodes"], 1)
        self.assertEqual(rep["summary_counts"]["added_nodes"], 1)

    def test_11_synthetic_stable_direct_capture_via_cmd_click_preserves_raw_bytes_and_metadata(self):
        """Case 11 (SYNTHETIC stable-direct branch check from review/independent_checks.py):
        Exercises PhoneController.cmd_click -> _cmd_click_traced -> _save_pre_input_mismatch_evidence
        with read_backend='stable-direct'. Note: mocked evidence tests retention only, not live bridge proof.
        """
        session = self.approved_out / "stable-direct-session"
        session.mkdir(parents=True, exist_ok=True)
        expected_xml = session / "original.xml"
        expected_xml.write_bytes(self.base_xml_bytes)
        original_meta = session / "observation-original.json"
        original_meta.write_text('{"do_not_overwrite":true}', encoding="utf-8")
        current_bytes = self.base_xml_bytes + b"<!-- SYNTHETIC_STABLE_DIRECT_CAPTURE -->"

        ctl = phone.PhoneController.__new__(phone.PhoneController)
        ctl.output_dir = str(session)
        ctl.profile_id = "api35-google-x86_64"
        ctl.avd = "ai-sandbox-api35-google"
        ctl.serial = "emulator-5580"
        ctl.read_backend = "stable-direct"
        ctl.latest_read_evidence = {"SYNTHETIC": True, "not_real_bridge_proof": True}

        obs = phone.ca.Observation(self.base_xml_bytes)
        target = next(it for it in obs.items if it.attr("text") == "Search")
        ctl.verify_live_device = Mock()
        ctl.load_session_observation = Mock(
            return_value=(obs, {"xml_filename": expected_xml.name, "xml_path": str(expected_xml)})
        )
        ctl.dump_hierarchy_bytes = Mock(return_value=(current_bytes, "SYNTHETIC-read-artifact"))
        ctl.adb = Mock()

        with self.assertRaises(phone.PhoneError) as ctx:
            ctl.cmd_click(hashlib.sha256(self.base_xml_bytes).hexdigest(), target.node_id)

        self.assertEqual(ctx.exception.code, "stale_observation")
        detail = ctx.exception.detail
        ev = detail["pre_input_mismatch_evidence"]
        self.assertEqual(Path(ev["current_xml_path"]).read_bytes(), current_bytes)
        self.assertEqual(ev["read_evidence"], ctl.latest_read_evidence)
        self.assertEqual(ev["read_backend"], "stable-direct")
        self.assertFalse(detail["click_stage"]["input_attempted"])
        self.assertFalse(detail["click_stage"]["input_send"]["requested"])
        self.assertEqual(ctl.dump_hierarchy_bytes.call_count, 1)
        self.assertEqual(ctl.adb.mock_calls, [])
        self.assertEqual(expected_xml.read_bytes(), self.base_xml_bytes)
        self.assertEqual(original_meta.read_text(encoding="utf-8"), '{"do_not_overwrite":true}')
        self.assertEqual(ev["current_hash"], hashlib.sha256(current_bytes).hexdigest())
        self.assertEqual(ev["device_serial"], "emulator-5580")


if __name__ == "__main__":
    unittest.main(verbosity=2)
