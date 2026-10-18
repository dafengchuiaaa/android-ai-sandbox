"""Independent, offline checks for the frozen phone CLI contract.

Reads captured JSON only; never imports phone.py or invokes a device command.
Protocol validity is separate from evidence that the requested goal succeeded.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sys

try:  # imported as integration.validate_response
    from integration import instance_profiles
except ImportError:  # imported as a top-level module with integration/ on sys.path
    import instance_profiles

ACTIONS = {"status", "launch", "observe", "click", "set-text", "evaluate-search", "screenshot"}
# Baseline AVD, kept as a module constant for existing callers. The expected
# AVD for a check now comes from the selected fixed profile, never from the
# payload under test.
AVD = instance_profiles.avd_for(instance_profiles.BASELINE_PROFILE_ID)
HASH = re.compile(r"[0-9a-f]{64}\Z")
SERIAL = re.compile(r"emulator-([0-9]+)\Z")


def profile_matches(found, expected_profile_id):
    """Profile binding rule shared by status and observation checks.

    A non-baseline payload must state its profile_id exactly. A baseline
    payload may omit it (round-3 captures predate profiles) but must not claim
    a different one.
    """
    if found is None:
        return expected_profile_id == instance_profiles.BASELINE_PROFILE_ID
    return found == expected_profile_id


def valid_serial(value):
    match = SERIAL.fullmatch(value) if isinstance(value, str) else None
    return bool(match and 0 < int(match[1]) <= 65534 and int(match[1]) % 2 == 0)


def observation_errors(data, profile_id=instance_profiles.BASELINE_PROFILE_ID):
    errors = []
    expected_avd = instance_profiles.avd_for(profile_id)
    if not isinstance(data, dict):
        return ["observation must be an object"]
    digest = data.get("observation_id")
    if not isinstance(digest, str) or not HASH.fullmatch(digest):
        errors.append("observation_id must be a full lowercase SHA-256")
    if not valid_serial(data.get("device_serial")):
        errors.append("observation device_serial invalid")
    if data.get("avd") != expected_avd:
        errors.append("observation AVD mismatch")
    if not profile_matches(data.get("profile_id"), profile_id):
        errors.append("observation profile_id does not match the selected profile")
    if data.get("coverage") != "partial":
        errors.append("UI observation must retain partial coverage")
    timestamp = data.get("observed_at")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0:
            raise ValueError("not UTC")
    except (AttributeError, TypeError, ValueError):
        errors.append("observed_at must be a UTC timestamp")
    if not isinstance(data.get("xml_path"), str) or not data["xml_path"]:
        errors.append("xml_path missing")
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        errors.append("nodes must be a list")
    else:
        seen = set()
        for node in nodes:
            node_id = node.get("node_id") if isinstance(node, dict) else None
            if not isinstance(node_id, str) or not node_id:
                errors.append("node missing node_id")
            elif node_id in seen:
                errors.append("duplicate node_id")
            elif isinstance(digest, str) and not node_id.startswith(digest[:12] + ":"):
                errors.append("node_id does not belong to observation")
            if isinstance(node_id, str):
                seen.add(node_id)
    return errors


def validate(payload, exit_code, action, query=None, expected_serial=None, expected_profile=None):
    """Do not accept an author's `ok` field as the task-success verdict."""
    try:
        profile = instance_profiles.resolve(expected_profile)
    except instance_profiles.ProfileError as exc:
        return {"protocol_valid": False, "goal_verified": None, "errors": [exc.message], "warnings": []}
    expected_profile_id = profile.profile_id
    expected_avd = profile.avd
    errors = []
    warnings = []
    goal = None
    if not isinstance(payload, dict):
        return {"protocol_valid": False, "goal_verified": None, "errors": ["response must be an object"], "warnings": []}
    if not {"ok", "action", "data", "error", "artifacts"}.issubset(payload):
        errors.append("response missing required envelope fields")
    if action not in ACTIONS:
        errors.append("unknown action")
    if payload.get("action") != action:
        errors.append("action mismatch")
    if not isinstance(payload.get("artifacts"), list):
        errors.append("artifacts must be a list")
    ok = payload.get("ok")
    if type(ok) is not bool:
        errors.append("ok must be a boolean")
    if ok is False:
        error = payload.get("error")
        if exit_code == 0:
            errors.append("failure returned zero exit code")
        if payload.get("data") is not None:
            errors.append("failure data must be null")
        if not isinstance(error, dict) or not all(isinstance(error.get(k), str) and error[k] for k in ("code", "message")):
            errors.append("failure needs error.code and error.message")
        goal = False
    elif ok is True:
        if exit_code != 0:
            errors.append("success returned nonzero exit code")
        if payload.get("error") is not None:
            errors.append("success error must be null")
        data = payload.get("data")
        if not isinstance(data, dict):
            errors.append("success data must be an object")
        else:
            if expected_serial and data.get("device_serial", data.get("after_observation", {}).get("device_serial") if isinstance(data.get("after_observation"), dict) else None) != expected_serial:
                errors.append("response belongs to a different device")
            if action == "observe":
                errors.extend(observation_errors(data, expected_profile_id))
            elif action == "status":
                if type(data.get("ready")) is not bool or not valid_serial(data.get("device_serial")) or data.get("avd") != expected_avd:
                    errors.append("status lacks valid ready/device_serial/avd")
                elif not profile_matches(data.get("profile_id"), expected_profile_id):
                    errors.append("status profile_id does not match the selected profile")
                goal = data.get("ready") is True
            elif action in {"launch", "click", "set-text"}:
                errors.extend(observation_errors(data.get("after_observation"), expected_profile_id))
                if action == "launch" and data.get("package") not in ("org.fdroid.basic", "com.android.settings"):
                    errors.append("launched package is not an approved package")
                if action == "click":
                    plan = data.get("plan")
                    if not isinstance(data.get("target_node_id"), str) or not data["target_node_id"]:
                        errors.append("click missing target_node_id")
                    if not isinstance(plan, dict) or plan.get("action") != "tap" or any(type(plan.get(k)) is not int or plan[k] < 0 for k in ("x", "y")):
                        errors.append("click missing valid tap plan")
                if action == "set-text":
                    text = data.get("input_text")
                    if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z0-9]{0,200}", text):
                        errors.append("set-text input outside supported alphabet")
                    if data.get("verified") is not True or data.get("readback_text") != text:
                        errors.append("set-text success without exact verified readback")
                    goal = data.get("verified") is True and data.get("readback_text") == text
            elif action == "evaluate-search":
                candidates = data.get("real_result_candidates")
                for field in ("input_matches_query", "empty_state", "content_present", "content_verified"):
                    if type(data.get(field)) is not bool:
                        errors.append(field + " must be a boolean")
                if not isinstance(candidates, list):
                    errors.append("search candidates must be a list")
                elif type(data.get("result_count")) is not int or data["result_count"] != len(candidates):
                    errors.append("result_count must equal candidate count")
                elif data.get("content_present") is not bool(candidates):
                    errors.append("content_present disagrees with candidates")
                if data.get("network_verified") != "unknown":
                    errors.append("XML cannot assert network verification")
                if query is None:
                    warnings.append("no expected query supplied; goal verification unavailable")
                else:
                    evidence = data.get("input_evidence")
                    raw = evidence.get("raw_text") if isinstance(evidence, dict) else None
                    exact = isinstance(raw, str) and raw.strip().casefold() == query.strip().casefold()
                    goal = bool(data.get("content_verified") is True and data.get("input_matches_query") is True and data.get("empty_state") is False and isinstance(candidates, list) and candidates and data.get("page_kind") == "search_results" and exact)
                    if data.get("content_verified") is True and not goal:
                        errors.append("content_verified claims an unconfirmed search goal")
            elif action == "screenshot":
                if not all(isinstance(data.get(k), str) and data[k].strip() for k in ("reason", "artifact")):
                    errors.append("screenshot needs explicit reason and artifact")
    if errors and goal is True:
        goal = False
    return {"protocol_valid": not errors, "goal_verified": goal, "errors": errors, "warnings": warnings,
            "limits": ["Captured JSON only; no live-device, freshness, or artifact-authenticity proof."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--action", choices=sorted(ACTIONS), required=True)
    parser.add_argument("--query")
    parser.add_argument("--serial")
    parser.add_argument("--profile", dest="profile", default=instance_profiles.DEFAULT_PROFILE_ID,
                        help="Fixed instance profile the payload must belong to (default: %s)"
                             % instance_profiles.DEFAULT_PROFILE_ID)
    args = parser.parse_args(argv)
    try:
        # json.loads rejects multiple JSON values and mixed ADB stdout.
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
        result = validate(payload, args.exit_code, args.action, args.query, args.serial, args.profile)
    except (OSError, ValueError, TypeError) as exc:
        result = {"protocol_valid": False, "goal_verified": None, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["protocol_valid"] else 1


if __name__ == "__main__":
    sys.exit(main())
