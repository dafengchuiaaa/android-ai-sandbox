"""Project entry: check, audit, validate, or forward frozen phone CLI commands.

Device commands require an explicit state file and session output directory.
This file does not start/stop an emulator; ownership remains with the launcher.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

from integration import check_delivery, instance_profiles, validate_response, verify_baseline

ROOT = Path(__file__).resolve().parent
PHONE = ROOT / "departments/antigravity/phone.py"
LOG_ROOT = ROOT / "integration/runtime"


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--adb", required=True)
    result.add_argument("--state-file", required=True)
    result.add_argument("--output-dir", required=True)
    result.add_argument("--profile", dest="profile_id", default=instance_profiles.DEFAULT_PROFILE_ID,
                        help="Fixed instance profile for the forwarded command (default: %s). "
                             "The selected profile is passed to the backend and re-checked here."
                             % instance_profiles.DEFAULT_PROFILE_ID)
    result.add_argument("--read-backend", choices=("legacy", "stable-direct"), default="legacy")
    sub = result.add_subparsers(dest="action", required=True)
    for action in ("status", "observe"):
        sub.add_parser(action)
    launch = sub.add_parser("launch")
    launch.add_argument("--package", required=True, choices=["org.fdroid.basic", "com.android.settings"])
    for action in ("click", "set-text"):
        command = sub.add_parser(action)
        command.add_argument("--observation-id", required=True)
        command.add_argument("--node-id", required=True)
        if action == "set-text":
            command.add_argument("--value", required=True)
    search = sub.add_parser("evaluate-search")
    search.add_argument("--observation-id", required=True)
    search.add_argument("--query", required=True)
    sub.add_parser("screenshot").add_argument("--reason", required=True)
    return result


def failure(action, code, message, artifacts=(), detail=None):
    error = {"code": code, "message": message}
    if detail is not None:
        error["detail"] = detail
    print(json.dumps({"ok": False, "action": action, "data": None, "error": error, "artifacts": list(artifacts)}, ensure_ascii=False))
    return 1


def forward(argv):
    args = parser().parse_args(argv)
    if not PHONE.is_file():
        return failure(args.action, "backend_not_ready", "The Antigravity phone CLI is not available; no device command was executed.")
    readiness = check_delivery.inspect()
    if not readiness["accepted_revisions"]["phone_cli"]:
        return failure(args.action, "backend_not_accepted", "The phone CLI revision has not passed independent acceptance; use its own review workflow first.")
    try:
        profile = instance_profiles.resolve(args.profile_id)
    except instance_profiles.ProfileError as exc:
        return failure(args.action, exc.code, exc.message)
    try:
        state = json.loads(Path(args.state_file).read_text(encoding="utf-8-sig"))
        if not isinstance(state, dict):
            raise ValueError("state is not a JSON object")
        problems = instance_profiles.state_identity_problems(state, profile)
        if problems:
            return failure(args.action, problems[0]["code"], problems[0]["message"],
                           detail=problems[0].get("detail"))
        if state.get("avd") != profile.avd:
            raise ValueError("state does not identify AVD %r for profile %r"
                             % (profile.avd, profile.profile_id))
        if not validate_response.valid_serial(state.get("serial")):
            raise ValueError("state does not identify a valid emulator serial")
    except (OSError, ValueError) as exc:
        return failure(args.action, "invalid_argument", str(exc))
    # Make the selected profile explicit on the backend command line instead of
    # relying on both ends defaulting the same way.
    child_argv = list(argv)
    if "--profile" not in child_argv:
        child_argv = ["--profile", profile.profile_id] + child_argv
    # Unique file redirection avoids waiting for EOF on inherited ADB pipes.
    if not LOG_ROOT.resolve().is_relative_to((ROOT / "integration").resolve()):
        return failure(args.action, "invalid_output_path", "Wrapper log path escapes its integration directory.")
    LOG_ROOT.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    stdout_path, stderr_path = LOG_ROOT / (token + ".stdout.json"), LOG_ROOT / (token + ".stderr.txt")
    artifacts = [str(stdout_path), str(stderr_path)]
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    child_env = os.environ.copy()
    child_env["PYTHONIOENCODING"] = "utf-8"
    with stdout_path.open("x", encoding="utf-8") as out, stderr_path.open("x", encoding="utf-8") as err:
        try:
            proc = subprocess.Popen([sys.executable, str(PHONE), *child_argv], stdout=out, stderr=err,
                                    stdin=subprocess.DEVNULL, shell=False, creationflags=flags, env=child_env)
        except OSError as exc:
            return failure(args.action, "backend_launch_failed", str(exc), artifacts)
        try:
            code = proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
            return failure(args.action, "driver_timeout", "Phone command exceeded 180 seconds; device state requires a fresh status check.", artifacts)
    try:
        payload = json.loads(stdout_path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        return failure(args.action, "invalid_backend_response", str(exc), artifacts)
    checked = validate_response.validate(payload, code, args.action, getattr(args, "query", None),
                                        state["serial"], profile.profile_id)
    if not checked["protocol_valid"]:
        return failure(args.action, "contract_violation", "Backend response failed independent checks.", artifacts, checked["errors"])
    # Preserve the frozen public envelope. Search empty-state remains a valid
    # response whose content_verified is false, not a completed search goal.
    print(json.dumps(payload, ensure_ascii=False))
    return code


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv == ["--help"]:
        print("Offline: python sandbox.py check | audit | validate --input FILE --exit-code N --action ACTION")
        print("Device: python sandbox.py --adb PATH --state-file STATE --output-dir SESSION [--profile PROFILE] ACTION [ARGS]")
        print("Device forwarding requires a matching independently accepted backend revision.")
        return 0
    if argv[0] == "check":
        return check_delivery.main(argv[1:])
    if argv[0] == "audit":
        return verify_baseline.main(argv[1:])
    if argv[0] == "validate":
        return validate_response.main(argv[1:])
    return forward(argv)


if __name__ == "__main__":
    sys.exit(main())
