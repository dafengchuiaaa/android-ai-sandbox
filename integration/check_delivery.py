"""Inspect known delivery files without executing agents, ADB, or launchers."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_HASH = "84984c502b32ecae04485fe98ca7076a1164ce733e2b23a4a4b843217be7d293"
COMPONENTS = {
    "launcher": "departments/workbuddy/start-baseline.ps1",
    "driver": "departments/zcode/adb_control.py",
    "adapter": "departments/deepseek/content_adapter.py",
    "phone_cli": "departments/antigravity/phone.py",
    "phone_verification": "departments/antigravity/VERIFICATION.md",
    "search_scenario": "departments/deepseek/scenarios/fdroid_search.py",
    "independent_probe": "departments/zcode/review-phone/probe_phone_cli.py",
    "baseline_summary": "departments/workbuddy/integration/runs/p1-01-r2/summary.json",
}

# New profile definitions and validators affect device ownership even when
# phone.py itself is unchanged. Pin the integrated bundle as one revision.
BUNDLE = {
    "entry": "sandbox.py",
    "profiles": "integration/instance_profiles.py",
    "validator": "integration/validate_response.py",
    "scenario_profiles": "departments/deepseek/scenarios/round4_profiles.py",
    "details": "departments/deepseek/scenarios/round3/fdroid_details.py",
    "api35_launcher": "departments/workbuddy/round4-launcher/start-api35.ps1",
    "api35_prepare": "departments/workbuddy/round4-launcher/prepare-api35.ps1",
    "driver": "departments/zcode/adb_control.py",
    "delivery_checker": "integration/check_delivery.py",
}


def inspect(root=ROOT):
    root = Path(root)
    components = {}
    for name, relative in COMPONENTS.items():
        path = root / relative
        present = path.is_file()
        components[name] = {"path": relative, "present": present,
                            "sha256": hashlib.sha256(path.read_bytes()).hexdigest() if present else None,
                            "state": "present_unverified" if present else "missing"}
    receipt_path = root / "integration/acceptance.json"
    receipt_errors = []
    try:
        receipts = json.loads(receipt_path.read_text(encoding="utf-8"))
        if not isinstance(receipts, dict):
            raise ValueError("acceptance record must be an object")
    except (OSError, ValueError) as exc:
        receipts = {}
        receipt_errors.append(str(exc))
    accepted = {}
    for name in ("phone_cli", "search_scenario"):
        entry = receipts.get(name, {})
        accepted[name] = bool(isinstance(entry, dict) and entry.get("result") == "accepted"
                              and components[name]["present"]
                              and entry.get("sha256") == components[name]["sha256"])
    bundle_required = (root / BUNDLE["profiles"]).is_file()
    bundle_checks = {}
    if bundle_required:
        bundle_receipt = receipts.get("component_bundle", {})
        for name, relative in BUNDLE.items():
            path = root / relative
            actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
            expected = bundle_receipt.get(name) if isinstance(bundle_receipt, dict) else None
            bundle_checks[name] = {"path": relative, "sha256": actual,
                                   "accepted": actual is not None and actual == expected}
        if not all(item["accepted"] for item in bundle_checks.values()):
            accepted["phone_cli"] = False
            accepted["search_scenario"] = False
    blockers = list(receipt_errors)
    for name, item in bundle_checks.items():
        if not item["accepted"]:
            blockers.append("component_bundle: " + name + " missing or source revision changed")
    if components["adapter"]["sha256"] != ADAPTER_HASH:
        blockers.append("adapter revision differs from the independently accepted version")
    if not components["phone_cli"]["present"]:
        blockers.append("phone CLI implementation not available")
    if not components["phone_verification"]["present"]:
        blockers.append("phone command verification evidence not available")
    for name, passed in accepted.items():
        if not passed:
            blockers.append(name + ": independent acceptance missing or source revision changed")
    required = ("launcher", "driver", "phone_cli", "phone_verification", "search_scenario")
    integration_ready = (all(components[name]["present"] for name in required)
                         and components["adapter"]["sha256"] == ADAPTER_HASH
                         and accepted["phone_cli"] and accepted["search_scenario"])
    gates = receipts.get("product_gates", {})
    product_ready = bool(integration_ready and isinstance(gates, dict)
                         and gates.get("live_ai_demo") is True and gates.get("application_refresh") is True
                         and gates.get("human_foreground_input") is True)
    missing_gates = [name for name in ("live_ai_demo", "application_refresh", "human_foreground_input")
                     if not isinstance(gates, dict) or gates.get(name) is not True]
    if missing_gates:
        blockers.append("Product evidence still missing: " + ", ".join(missing_gates))
    return {"generator": "codex-delivery-inventory", "ready_for_product_acceptance": product_ready,
            "ready_for_device_integration": integration_ready, "accepted_revisions": accepted,
            "acceptance_scope": receipts.get("scope", "Component inventory and scoped functional evidence only."),
            "lightweight_target_met": receipts.get("lightweight_target_met") is True,
            "remaining_project_work": receipts.get("remaining_project_work", []),
            "components": components, "blockers": blockers,
            "component_bundle": bundle_checks,
            "limits": ["File presence and author reports are inventory only, not test-pass evidence.",
                       "This checker neither controls the device nor changes any settings."]}


def main(argv=None):
    if argv:
        print(json.dumps({"error": "check takes no arguments"}))
        return 2
    report = inspect()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready_for_product_acceptance"] else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
