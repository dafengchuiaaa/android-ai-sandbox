"""One bounded real integration run of the delivered CLI and search scenario.

Only the unique instance started by this run is stopped. This is a fixed
regression workflow, not evidence that an AI selected each node independently.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from validate_response import validate

ROOT = Path(__file__).resolve().parents[1]
ADB = Path(r"C:\Users\LOCAL_USER\AppData\Local\Android\Sdk\platform-tools\adb.exe")
PHONE = ROOT / "departments/antigravity/phone.py"
SCENE = ROOT / "departments/deepseek/scenarios/fdroid_search.py"
STARTER = ROOT / "departments/workbuddy/start-baseline.ps1"


def stamp():
    return datetime.now(timezone.utc).isoformat()


def hashes():
    return {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (PHONE, SCENE, ROOT / "departments/deepseek/content_adapter.py", ROOT / "departments/zcode/adb_control.py")}


def main():
    token = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    output = ROOT / "reviews/round2-final" / ("live-" + token)
    session = ROOT / "departments/antigravity/runtime" / ("codex-" + token)
    state = ROOT / "departments/workbuddy/runtime/codex" / ("instance-" + token + ".json")
    scene_report = ROOT / "departments/deepseek/scenarios/evidence" / ("codex-live-" + token)
    output.mkdir(parents=True, exist_ok=False)
    deadline = time.monotonic() + 420
    result = {"kind": "real_fixed_regression", "started_utc": stamp(), "source_sha256_before": hashes(),
              "steps": [], "passed": False, "cleanup_verified": False,
              "limits": ["No current-network-refresh proof; public catalog may be cached.",
                         "No human foreground-input verification; fixed workflow is not a new AI node-selection demonstration."]}

    def run(label, cmd, timeout=60, cleanup=False):
        bound = timeout if cleanup else min(timeout, deadline - time.monotonic())
        if bound <= 0:
            raise RuntimeError("overall integration time budget exceeded")
        outpath, errpath = output / (label + ".stdout.txt"), output / (label + ".stderr.txt")
        entry = {"step": label, "argv": [str(item) for item in cmd], "started_utc": stamp()}
        start = time.monotonic()
        with outpath.open("x", encoding="utf-8") as out, errpath.open("x", encoding="utf-8") as err:
            child_env = os.environ.copy()
            child_env["PYTHONIOENCODING"] = "utf-8"
            proc = subprocess.Popen(entry["argv"], stdout=out, stderr=err, stdin=subprocess.DEVNULL,
                                    shell=False, env=child_env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                code = proc.wait(timeout=bound)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
                code = -1
        text = outpath.read_text(encoding="utf-8-sig", errors="replace")
        entry.update(exit_code=code, ended_utc=stamp(), elapsed_seconds=time.monotonic() - start,
                     stdout=str(outpath), stderr=str(errpath))
        result["steps"].append(entry)
        print(label, "exit", code, "seconds", round(entry["elapsed_seconds"], 2), flush=True)
        return code, text, entry

    serial = None

    def phone(label, action, extra=(), timeout=60):
        code, text, entry = run(label, [sys.executable, PHONE, "--adb", ADB, "--state-file", state,
                                       "--output-dir", session, action, *extra], timeout)
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise RuntimeError("%s invalid JSON: %s" % (label, exc))
        entry["independent_response_check"] = validate(payload, code, action, "termux" if action == "evaluate-search" else None, serial)
        if not entry["independent_response_check"]["protocol_valid"]:
            raise RuntimeError("%s contract error: %s" % (label, entry["independent_response_check"]["errors"]))
        return code, payload

    def set_value(label, value):
        for attempt in range(2):
            _, snapshot = phone(label + "-observe-" + str(attempt), "observe")
            data = snapshot["data"]
            fields = [node for node in data["nodes"] if (node.get("class") or "").endswith("EditText") and node.get("enabled") is True]
            if len(fields) != 1:
                raise RuntimeError("%s: expected one enabled input field" % label)
            code, response = phone(label + "-set-" + str(attempt), "set-text",
                                    ["--observation-id", data["observation_id"], "--node-id", fields[0]["node_id"], "--value", value])
            if code == 0 and response["data"]["verified"] is True and response["data"]["readback_text"] == value:
                return
            error = response.get("error") or {}
            if attempt == 0 and error.get("code") == "stale_observation":
                continue
            raise RuntimeError("%s: %s" % (label, error))
        raise RuntimeError(label + ": stale recovery exhausted")

    boot_attempted = False
    try:
        code, devices, _ = run("00-preflight-devices", [ADB, "devices"], 30)
        if code != 0 or re.search(r"^emulator-\d+\s", devices, re.MULTILINE):
            raise RuntimeError("Existing or unknown emulator/device-list failure; leaving it untouched")
        boot_attempted = True
        code, text, _ = run("01-start", ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", STARTER,
                                         "-StateFile", state, "-BootTimeoutSec", "120"], 170)
        if code != 0 or "BOOT OK" not in text:
            raise RuntimeError("project launcher failed")
        identity = json.loads(state.read_text(encoding="utf-8-sig"))
        serial = identity["serial"]
        result.update(device_serial=serial, state_file=str(state), session_dir=str(session))
        code, status = phone("02-status", "status")
        if code != 0 or status["data"]["ready"] is not True:
            raise RuntimeError("status not ready")
        code, scene_text, _ = run("03-fixed-search", [sys.executable, SCENE, "--phone-cli", PHONE, "--python", sys.executable,
                                                     "--adb", ADB, "--state-file", state, "--session-dir", session,
                                                     "--query", "termux", "--report-dir", scene_report], 105)
        scene = json.loads(scene_text)
        result["fixed_search"] = scene
        if code != 0 or scene.get("ok") is not True or scene.get("data", {}).get("target_verified") is not True:
            raise RuntimeError("fixed search scenario failed: " + str(scene.get("error")))
        set_value("04-long-input", "A" * 200)
        set_value("05-replace-long-input", "termux")
        _, observation = phone("06-final-observe", "observe")
        code, evaluation = phone("07-final-evaluate", "evaluate-search", ["--observation-id", observation["data"]["observation_id"], "--query", "termux"])
        result["final_search"] = evaluation
        if code != 0 or evaluation["data"]["content_verified"] is not True:
            raise RuntimeError("final restored search did not verify")
        result["passed"] = True
    except Exception as exc:
        result["error"] = "%s: %s" % (type(exc).__name__, exc)
        print("INTEGRATION FAILED:", result["error"], flush=True)
    finally:
        if boot_attempted and state.exists():
            try:
                code, text, _ = run("99-stop", ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", STARTER,
                                                "-Stop", "-StateFile", state], 120, cleanup=True)
                result["cleanup_verified"] = code == 0 and ("stopped and verified gone" in text or "not present in adb devices" in text)
            except Exception as exc:
                result["cleanup_error"] = str(exc)
        elif not boot_attempted:
            result["cleanup_verified"] = True
        result["source_sha256_after"] = hashes()
        result["same_revision"] = result["source_sha256_before"] == result["source_sha256_after"]
        result["passed"] = bool(result["passed"] and result["cleanup_verified"] and result["same_revision"])
        result["ended_utc"] = stamp()
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print("RESULT", output / "result.json", "PASS", result["passed"], flush=True)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
