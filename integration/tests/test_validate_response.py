import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_response import validate


def observation():
    return {"observation_id": "a" * 64, "device_serial": "emulator-5554", "avd": "ai-sandbox-baseline",
            "coverage": "partial", "observed_at": "2026-10-15T09:00:00Z", "xml_path": "captured.xml",
            "nodes": [{"node_id": "aaaaaaaaaaaa:R.0", "text": "Search"}]}


def response(action, data):
    return {"ok": True, "action": action, "data": data, "error": None, "artifacts": []}


def search():
    return {"content_present": True, "content_verified": True, "input_matches_query": True,
            "empty_state": False, "network_verified": "unknown", "page_kind": "search_results",
            "real_result_candidates": [{"label": "Termux"}], "result_count": 1,
            "input_evidence": {"raw_text": "termux"}, "device_serial": "emulator-5554"}


class ContractChecks(unittest.TestCase):
    def test_valid_observation_and_device_binding(self):
        self.assertTrue(validate(response("observe", observation()), 0, "observe", expected_serial="emulator-5554")["protocol_valid"])
        self.assertFalse(validate(response("observe", observation()), 0, "observe", expected_serial="emulator-5556")["protocol_valid"])

    def test_error_envelope_cannot_exit_zero(self):
        failure = {"ok": False, "action": "observe", "data": None, "error": {"code": "driver_timeout", "message": "timeout"}, "artifacts": []}
        self.assertTrue(validate(failure, 1, "observe")["protocol_valid"])
        self.assertFalse(validate(failure, 0, "observe")["protocol_valid"])

    def test_string_true_is_not_success(self):
        payload = response("observe", observation())
        payload["ok"] = "true"
        self.assertFalse(validate(payload, 0, "observe")["protocol_valid"])

    def test_old_or_duplicate_node_ids_rejected(self):
        for node_id in ("b" * 12 + ":R.0", "a" * 12 + ":R.0"):
            data = observation()
            data["nodes"].append({"node_id": node_id})
            self.assertFalse(validate(response("observe", data), 0, "observe")["protocol_valid"])

    def test_short_hash_and_invented_coverage_rejected(self):
        for field, value in (("observation_id", "a" * 12), ("coverage", "full"), ("observed_at", None), ("device_serial", "emulator-5555")):
            data = observation()
            data[field] = value
            self.assertFalse(validate(response("observe", data), 0, "observe")["protocol_valid"])

    def test_successful_input_requires_exact_readback(self):
        data = {"input_text": "termux", "readback_text": "termuxtermux", "verified": True, "after_observation": observation()}
        self.assertFalse(validate(response("set-text", data), 0, "set-text")["protocol_valid"])
        data["readback_text"] = "termux"
        self.assertTrue(validate(response("set-text", data), 0, "set-text")["goal_verified"])

    def test_empty_search_is_valid_protocol_but_failed_goal(self):
        data = search()
        data.update(content_present=False, content_verified=False, empty_state=True, page_kind="search_empty_state", real_result_candidates=[], result_count=0)
        result = validate(response("evaluate-search", data), 0, "evaluate-search", query="termux")
        self.assertTrue(result["protocol_valid"])
        self.assertFalse(result["goal_verified"])

    def test_wrong_query_and_concat_cannot_be_verified(self):
        for raw in ("fdroid", "termuxtermux"):
            data = search()
            data["input_evidence"]["raw_text"] = raw
            result = validate(response("evaluate-search", data), 0, "evaluate-search", query="termux")
            self.assertFalse(result["protocol_valid"])
            self.assertFalse(result["goal_verified"])

    def test_xml_network_claim_and_count_mismatch_rejected(self):
        for field, value in (("network_verified", True), ("result_count", 2)):
            data = search()
            data[field] = value
            self.assertFalse(validate(response("evaluate-search", data), 0, "evaluate-search", query="termux")["protocol_valid"])

    def test_valid_search_needs_explicit_expected_query(self):
        payload = response("evaluate-search", search())
        self.assertTrue(validate(payload, 0, "evaluate-search", query="termux")["goal_verified"])
        self.assertIsNone(validate(payload, 0, "evaluate-search")["goal_verified"])

    def test_screenshot_requires_reason(self):
        self.assertFalse(validate(response("screenshot", {"artifact": "x.png", "reason": ""}), 0, "screenshot")["protocol_valid"])

    def test_unready_status_not_a_verified_goal(self):
        result = validate(response("status", {"ready": False, "device_serial": "emulator-5554", "avd": "ai-sandbox-baseline"}), 0, "status")
        self.assertTrue(result["protocol_valid"])
        self.assertFalse(result["goal_verified"])


if __name__ == "__main__":
    unittest.main()
