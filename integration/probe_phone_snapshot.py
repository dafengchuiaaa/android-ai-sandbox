"""Offline metadata-integrity probe of phone.py; never invokes an ADB runner.

Controller construction is bypassed only to keep all test files in Codex's own
temporary directory. Methods under test and content resolution remain unchanged.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / "departments/antigravity/phone.py"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("phone_under_review", path)
    phone = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(phone)
    original = ROOT / "departments/deepseek/fixtures/originals"
    live_bytes = (original / "hierarchy-search-results.xml").read_bytes()
    foreign_xml = original / "hierarchy-20261015T030637-30a5f3db.xml"
    live_hash = hashlib.sha256(live_bytes).hexdigest()
    foreign_obs = phone.ca.Observation(foreign_xml.read_bytes())
    target = next(item for item in foreign_obs.items if item.attr("text") == "Theme")
    calls = []

    class NoDevice:
        def input_tap(self, x, y):
            calls.append({"action": "fake_tap", "x": x, "y": y})

    probes = []
    with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as tmp:
        ctl = object.__new__(phone.PhoneController)
        ctl.output_dir = tmp
        ctl.serial, ctl.avd = "emulator-5554", "ai-sandbox-baseline"
        ctl.adb = NoDevice()
        ctl.verify_live_device = lambda: None
        ctl.dump_hierarchy_bytes = lambda: (live_bytes, "not-a-device-path")
        ctl.observe_live = lambda: ({"observation_id": live_hash}, "offline-post-observation")
        meta = {"observation_id": live_hash, "sha256": live_hash, "device_serial": "emulator-5556",
                "avd": "foreign-avd", "xml_path": str(foreign_xml), "observed_at": "2026-10-15T09:00:00Z"}
        meta_path = Path(tmp) / ("observation-" + live_hash[:12] + ".json")
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        try:
            loaded, _ = ctl.load_session_observation(live_hash)
            probes.append({"case": "foreign device metadata / external XML / wrong content hash",
                           "rejected": False, "requested_hash": live_hash, "loaded_hash": loaded.observation_id})
        except phone.PhoneError as exc:
            probes.append({"case": "foreign device metadata / external XML / wrong content hash", "rejected": True, "code": exc.code})
        try:
            data, _ = ctl.cmd_click(live_hash, target.node_id)
            probes.append({"case": "foreign XML drives click despite live-hash check", "rejected": False,
                           "plan": data["plan"], "fake_calls": calls})
        except phone.PhoneError as exc:
            probes.append({"case": "foreign XML drives click despite live-hash check", "rejected": True, "code": exc.code, "fake_calls": calls})
        # Isolate each guard instead of letting one foreign serial mask every
        # other defect. A positive control must also continue to load.
        local_xml = Path(tmp) / "known-session.xml"
        base = {"observation_id": live_hash, "sha256": live_hash,
                "device_serial": ctl.serial, "avd": ctl.avd, "xml_path": str(local_xml),
                "xml_filename": local_xml.name, "observed_at": "2026-10-15T09:00:00Z"}
        cases = [
            ("serial mismatch only", {"device_serial": "emulator-5556"}, live_bytes),
            ("AVD mismatch only", {"avd": "foreign"}, live_bytes),
            ("metadata hash mismatch only", {"sha256": "0" * 64}, live_bytes),
            ("stored XML hash mismatch only", {}, foreign_xml.read_bytes()),
            ("external XML path only", {"xml_path": str(original / "hierarchy-search-results.xml")}, live_bytes),
            ("filename and path mismatch only", {"xml_filename": "different.xml"}, live_bytes),
        ]
        for name, override, contents in cases:
            local_xml.write_bytes(contents)
            meta_path.write_text(json.dumps({**base, **override}), encoding="utf-8")
            try:
                ctl.load_session_observation(live_hash)
                probes.append({"case": name, "rejected": False})
            except phone.PhoneError as exc:
                probes.append({"case": name, "rejected": True, "code": exc.code})
        local_xml.write_bytes(live_bytes)
        meta_path.write_text(json.dumps(base), encoding="utf-8")
        loaded, _ = ctl.load_session_observation(live_hash)
        positive_control = loaded.observation_id == live_hash
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {"source_sha256_before": before, "source_sha256_after": after, "same_revision": before == after,
              "offline_only": True, "device_commands_executed": False, "probes": probes,
              "positive_control_passed": positive_control,
              "all_invalid_observations_rejected": all(p["rejected"] for p in probes)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["same_revision"] and result["all_invalid_observations_rejected"] and positive_control else 1


if __name__ == "__main__":
    sys.exit(main())
