#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""phone.py -- Unified Phone CLI v0.1 for Sandboxed Android Emulator.

Implements phone CLI contract v0.1 (docs/phone-cli-contract-v0.1.md)
for departments/antigravity.

Usage:
    python departments/antigravity/phone.py --adb <adb.exe> --state-file <instance.json> --output-dir <session_dir> <command> [args]

Commands:
    status            Check device connectivity, boot status and AVD ownership
    launch            Launch approved package (--package org.fdroid.basic)
    observe           Dump current UI hierarchy into structured observation
    click             Click resolved target node with staleness check (--observation-id, --node-id)
    set-text          Clear, focus, input and verify ASCII text in EditText (--observation-id, --node-id, --value)
    evaluate-search   Evaluate F-Droid search results on a saved observation (--observation-id, --query)
    screenshot        Take diagnostic screenshot with explicit reason (--reason)
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

import xml.etree.ElementTree as ET

# Locate and import existing approved departments
PROJECT_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)

DEEPSEEK_DIR = os.path.join(PROJECT_ROOT, "departments", "deepseek")
if DEEPSEEK_DIR not in sys.path:
    sys.path.insert(0, DEEPSEEK_DIR)
import content_adapter as ca  # noqa: E402

ZCODE_DIR = os.path.join(PROJECT_ROOT, "departments", "zcode")
if ZCODE_DIR not in sys.path:
    sys.path.insert(0, ZCODE_DIR)
import adb_control  # noqa: E402

# Fixed instance-profile definitions live with the integration validators so
# phone, sandbox and the response validator all read the same table.
INTEGRATION_DIR = os.path.join(PROJECT_ROOT, "integration")
if INTEGRATION_DIR not in sys.path:
    sys.path.insert(0, INTEGRATION_DIR)
import instance_profiles as profiles  # noqa: E402

CLI_VERSION = "0.1.3-codex-r4profile"
# Kept for backward compatibility: the baseline profile's AVD, which is what
# the single-instance contract always meant before profiles existed.
EXPECTED_AVD = profiles.default().avd
ALLOWED_PACKAGE = "org.fdroid.basic"
ALLOWED_PACKAGES = (ALLOWED_PACKAGE, "com.android.settings")
DIRECT_READER_PY_SHA256 = "b12379feffa64aff7d4d771b613627e5dc809e5e3911636fba4329d13c473f56"
DIRECT_READER_JAR_SHA256 = "714dd8c8bc02b6b1be49c3748942685334bb8e6805278ac5432b1207baf80b77"
SERIAL_RE = re.compile(r"^emulator-(\d{1,5})$")
TEXT_RE = re.compile(r"^[A-Za-z0-9]{0,200}$")
# Clear budget for the ASCII[0-200] contract.  cmd_set_text refuses any existing
# content longer than 200 characters before it sends a single keyevent, so 200
# is the largest provable budget for content this tool accepts: one backspace
# per character and one forward-delete for the mirror path.  The historical
# fixed 220 carried 20 events of unconditional slack that no bound in the code
# ever justified.
CLEAR_LIMIT = 200
# Budget kept verbatim for existing content the ASCII contract cannot bound
# (Unicode, IME composition, dynamic text): the candidate deliberately does not
# narrow behaviour there.
LEGACY_CLEAR_COUNT = 220

APPROVED_OUTPUT_ROOT = os.path.realpath(
    os.path.join(PROJECT_ROOT, "departments", "antigravity", "runtime")
)
APPROVED_STATE_ROOT = os.path.realpath(
    os.path.join(PROJECT_ROOT, "departments", "workbuddy", "runtime")
)

# Observation recovery and bounded dump constants
DUMP_TOTAL_TIMEOUT_S = 20.0
DUMP_CMD_TIMEOUT_S = 8.0
PULL_CMD_TIMEOUT_S = 5.0
CLEANUP_CMD_TIMEOUT_S = 2.0
DUMP_RETRY_DELAY_S = 0.5
MAX_DUMP_ATTEMPTS = 2

# --- click stage diagnostics (dispatch 78) ----------------------------------
# The live second-app failure reported "UIAutomator read failed twice" for a
# click, and that envelope could not say whether the failing read was the one
# taken *before* the tap or the one taken *after* it. cmd_click now labels the
# stage it was in when an error surfaced. This is labelling only: the dump
# budget (20s total / 8s per command / two read-only attempts), the order of
# operations and the number of input events are all unchanged, and a failure
# never triggers a retry.
CLICK_STAGES = (
    "verify_live_device",   # device/boot/AVD(/API/ABI) preflight
    "load_observation",     # session observation + metadata binding checks
    "pre_input_read",       # staleness dump taken BEFORE any input
    "resolve_target",       # node lookup and tap-plan resolution
    "send_input",           # the single adb input tap
    "post_input_read",      # re-observation AFTER the tap returned
)
CLICK_PHASE_BEFORE_INPUT = "before_input"
CLICK_PHASE_INPUT_UNKNOWN = "input_unknown"
CLICK_PHASE_AFTER_INPUT = "after_input"
# input_attempted is three-valued on purpose: False = proven no tap was sent,
# "unknown" = an ADB send was entered and its device-side outcome is not
# provable from the client, True = the tap call returned success. A client-side
# timeout is never reported as False, and "the tap call returned" is never
# reported as proof that the app reacted.
INPUT_ATTEMPTED_FALSE = False
INPUT_ATTEMPTED_UNKNOWN = "unknown"
INPUT_ATTEMPTED_TRUE = True


def send_failure_kind(exc: BaseException) -> str:
    """Name the tap-call failure without changing how it is reported."""
    if isinstance(exc, adb_control.AdbTimeout):
        return "timeout"
    if isinstance(exc, adb_control.AdbNotFound):
        return "missing_binary"
    if isinstance(exc, adb_control.AdbError):
        return "error"
    return "exception"


class ClickTrace:
    """Records which cmd_click stage was running when an error surfaced."""

    def __init__(self, profile_id: str, serial: str):
        self.profile_id = profile_id
        self.serial = serial
        self.stage = "not_started"
        self.stages_entered: List[str] = []
        self.input_attempted: Any = INPUT_ATTEMPTED_FALSE
        self.input_phase = CLICK_PHASE_BEFORE_INPUT
        self.send = {
            "requested": False,
            "coordinates": None,
            "command_returned": False,
            "returned_ok": None,
            "error_kind": None,
            "error_message": None,
        }
        self.app_state_after_input = "not_attempted"
        self.started = time.monotonic()

    def enter(self, stage: str) -> None:
        self.stage = stage
        self.stages_entered.append(stage)

    def input_send_started(self, x: int, y: int) -> None:
        """A tap is about to be handed to ADB; its outcome is unprovable."""
        self.send["requested"] = True
        self.send["coordinates"] = [x, y]
        self.input_attempted = INPUT_ATTEMPTED_UNKNOWN
        self.input_phase = CLICK_PHASE_INPUT_UNKNOWN
        self.app_state_after_input = "unknown"

    def input_send_unconfirmed(self, kind: str, message: str) -> None:
        """The tap call raised: the device may or may not have received it."""
        self.send["error_kind"] = kind
        self.send["error_message"] = message
        self.input_attempted = INPUT_ATTEMPTED_UNKNOWN
        self.input_phase = CLICK_PHASE_INPUT_UNKNOWN
        self.app_state_after_input = "unknown"

    def input_send_returned(self) -> None:
        """The tap call returned success. That is delivery, not app state."""
        self.send["command_returned"] = True
        self.send["returned_ok"] = True
        self.input_attempted = INPUT_ATTEMPTED_TRUE
        self.input_phase = CLICK_PHASE_AFTER_INPUT
        self.app_state_after_input = "unknown"

    def snapshot(self) -> Dict[str, Any]:
        return {
            "stage": self.stage,
            "stages_entered": list(self.stages_entered),
            "input_attempted": self.input_attempted,
            "input_phase": self.input_phase,
            "input_send": dict(self.send),
            "app_state_after_input": self.app_state_after_input,
            "elapsed_s": round(time.monotonic() - self.started, 3),
            "profile_id": self.profile_id,
            "device_serial": self.serial,
            "note": (
                "A successful input tap call only proves ADB accepted the command; "
                "it never proves the application reached the intended state. "
                "'unknown' means the client cannot decide whether the tap reached "
                "the device, so no input may be replayed on this evidence."
            ),
        }


def attach_click_stage(detail: Optional[Dict[str, Any]], trace: ClickTrace) -> Dict[str, Any]:
    """Return ``detail`` with stage fields added, original fields untouched."""
    if detail is None:
        merged: Dict[str, Any] = {}
    elif isinstance(detail, dict):
        merged = dict(detail)
    else:  # never happens today; keep the original value rather than drop it
        merged = {"original_detail": detail}
    merged["stage"] = trace.stage
    merged["click_stage"] = trace.snapshot()
    return merged


def validate_hierarchy_xml(data: bytes) -> Tuple[bool, str]:
    """Validate hierarchy XML bytes using standard xml.etree.ElementTree parser.

    Accepts both declaration-free and declared valid XML hierarchies.
    Rejects empty, truncated, malformed XML and XML with root tag other than 'hierarchy'.
    """
    if not data or len(data.strip()) == 0:
        return False, "empty_xml_bytes"
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        return False, f"malformed_xml: {exc}"
    except Exception as exc:
        return False, f"xml_parse_error: {exc}"
    if root.tag != "hierarchy":
        return False, f"wrong_root_tag: expected 'hierarchy', got {root.tag!r}"
    return True, "valid"



class PhoneError(Exception):
    """Exception carrying a stable error code and optional details."""

    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


def log(msg: str) -> None:
    """Diagnostic messages go to stderr; stdout carries strictly one JSON line."""
    sys.stderr.write("[phone_cli] %s\n" % msg)
    sys.stderr.flush()


def emit(action: Optional[str], ok: bool, data: Optional[Dict[str, Any]] = None,
         error: Optional[Dict[str, Any]] = None, artifacts: Optional[List[str]] = None) -> int:
    payload = {
        "ok": ok,
        "action": action,
        "data": data,
        "error": error,
        "artifacts": artifacts if artifacts is not None else [],
    }
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return 0 if ok else 1


def emit_success(action: str, data: Dict[str, Any], artifacts: Optional[List[str]] = None) -> int:
    return emit(action=action, ok=True, data=data, error=None, artifacts=artifacts)


def emit_failure(action: Optional[str], code: str, message: str,
                 detail: Optional[Dict[str, Any]] = None,
                 artifacts: Optional[List[str]] = None) -> int:
    err_dict: Dict[str, Any] = {"code": code, "message": message}
    if detail is not None:
        err_dict["detail"] = detail
    return emit(action=action, ok=False, data=None, error=err_dict, artifacts=artifacts)


def check_output_dir(path_str: str) -> str:
    """Validate that output directory is inside APPROVED_OUTPUT_ROOT."""
    if not path_str or not str(path_str).strip():
        raise PhoneError("invalid_output_path", "--output-dir is missing or empty")
    try:
        resolved = os.path.realpath(os.path.abspath(path_str))
    except OSError as exc:
        raise PhoneError("invalid_output_path", f"Cannot resolve output dir: {exc}")

    if resolved != APPROVED_OUTPUT_ROOT and not resolved.startswith(APPROVED_OUTPUT_ROOT + os.sep):
        raise PhoneError(
            "invalid_output_path",
            f"output-dir {resolved} escapes approved root {APPROVED_OUTPUT_ROOT}",
            detail={"output_dir": resolved, "approved_root": APPROVED_OUTPUT_ROOT},
        )
    try:
        os.makedirs(resolved, exist_ok=True)
    except OSError as exc:
        raise PhoneError("invalid_output_path", f"Cannot create output dir {resolved}: {exc}")
    return resolved


def check_adb_path(path_str: str) -> str:
    """Validate that adb exists."""
    if not path_str or not str(path_str).strip():
        raise PhoneError("adb_not_found", "--adb path is missing or empty")
    resolved = os.path.abspath(path_str)
    if not os.path.isfile(resolved):
        raise PhoneError("adb_not_found", f"adb executable not found at: {resolved}")
    return resolved


def load_and_verify_state(path_str: str, profile: profiles.Profile) -> Tuple[Dict[str, Any], str, str]:
    """Validate and read instance state file from APPROVED_STATE_ROOT.

    The state must match the *selected fixed profile*: its AVD and, for a
    profile that carries new identity fields, its profile_id / api_level / abi
    / avd_home / image_dir. A state belonging to another profile is refused
    rather than reinterpreted.
    """
    if not path_str or not str(path_str).strip():
        raise PhoneError("invalid_argument", "--state-file is missing or empty")
    try:
        resolved = os.path.realpath(os.path.abspath(path_str))
    except OSError as exc:
        raise PhoneError("invalid_argument", f"Cannot resolve state-file path: {exc}")

    if resolved != APPROVED_STATE_ROOT and not resolved.startswith(APPROVED_STATE_ROOT + os.sep):
        raise PhoneError(
            "ownership_mismatch",
            f"state-file {resolved} escapes approved root {APPROVED_STATE_ROOT}",
            detail={"state_file": resolved, "approved_root": APPROVED_STATE_ROOT},
        )
    if not os.path.isfile(resolved):
        raise PhoneError("device_unavailable", f"state-file does not exist: {resolved}")

    try:
        with open(resolved, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception as exc:
        raise PhoneError("invalid_argument", f"Failed to parse state-file JSON: {exc}")

    if not isinstance(state, dict):
        raise PhoneError("invalid_argument", "state-file must contain a JSON object")

    serial = state.get("serial")
    avd = state.get("avd")
    if not serial or not isinstance(serial, str):
        raise PhoneError("invalid_argument", "state-file does not contain a valid serial")
    m = SERIAL_RE.match(serial)
    if not m:
        raise PhoneError("invalid_argument", f"Invalid emulator serial format: {serial}")
    port = int(m.group(1))
    if port <= 0 or port > 65535 or port % 2 != 0:
        raise PhoneError("invalid_argument", f"Port in serial is not a valid even port: {port}")
    if avd != profile.avd:
        raise PhoneError(
            "ownership_mismatch",
            f"State file AVD is '{avd}', expected '{profile.avd}' for profile '{profile.profile_id}'",
            detail={"state_avd": avd, "expected_avd": profile.avd, "profile_id": profile.profile_id},
        )
    problems = profiles.state_identity_problems(state, profile)
    if problems:
        raise PhoneError(problems[0]["code"], problems[0]["message"], detail=problems[0].get("detail"))
    return state, serial, avd


class PhoneController:
    """Unified phone controller wrapping AdbControl and content_adapter."""

    def __init__(self, adb_path: str, state_file: str, output_dir: str, runner: Optional[Any] = None,
                 profile_id: Optional[str] = None, read_backend: str = "legacy"):
        try:
            self.profile = profiles.resolve(profile_id)
        except profiles.ProfileError as exc:
            raise PhoneError(exc.code, exc.message)
        self.profile_id = self.profile.profile_id
        if read_backend not in ("legacy", "stable-direct"):
            raise PhoneError("invalid_argument", "Unknown read backend")
        if read_backend == "stable-direct" and self.profile_id != "api35-google-x86_64":
            raise PhoneError("invalid_argument", "stable-direct is accepted only for the explicit API35 profile")
        self.read_backend = read_backend
        self.adb_path = check_adb_path(adb_path)
        self.output_dir = check_output_dir(output_dir)
        self.state, self.serial, self.avd = load_and_verify_state(state_file, self.profile)
        self.adb = adb_control.AdbControl(self.adb_path, self.serial, runner=runner)

    def _live_getprop(self, prop: str) -> str:
        rc, out, err = self.adb.run(["shell", "getprop", prop])
        if rc != 0:
            raise PhoneError("device_unavailable",
                             f"Failed to read {prop} on {self.serial}: {err.strip() or out.strip()}")
        return out.strip()

    def verify_live_device(self) -> None:
        """Confirm device connection, boot state, and verify live 'emu avd name'.

        A profile that pins the Android version additionally confirms the live
        API level and ABI, so a device that merely *claims* to be API 35 in a
        state file cannot be operated. All checks run before any input action.
        """
        state_kind, state = self.adb.device_state()
        if state_kind is None:
            raise PhoneError("device_unavailable", f"Serial {self.serial} is not present in adb devices")
        if state_kind == "offline":
            raise PhoneError("device_unavailable", f"Serial {self.serial} is offline")

        boot = self.adb.boot_completed()
        if boot != "1":
            raise PhoneError("device_unavailable", f"Serial {self.serial} boot_completed is not 1 (got {boot!r})")

        rc, out, err = self.adb.run(["emu", "avd", "name"])
        if rc != 0:
            raise PhoneError(
                "ownership_mismatch",
                f"Failed to query emu avd name on {self.serial}: {err.strip() or out.strip()}"
            )
        reported_lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        reported_avd = reported_lines[0] if reported_lines else ""
        if reported_avd != self.avd:
            raise PhoneError(
                "ownership_mismatch",
                f"emu avd name on {self.serial} reports '{reported_avd}', expected '{self.avd}'",
                detail={"reported_avd": reported_avd, "expected_avd": self.avd},
            )

        if self.profile.enforce_live_api:
            live_level = self._live_getprop("ro.build.version.sdk")
            if not live_level.isdigit():
                raise PhoneError(
                    "ownership_mismatch",
                    f"Live ro.build.version.sdk on {self.serial} is not numeric: {live_level!r}",
                    detail={"reported_api_level": live_level, "expected_api_level": self.profile.api_level},
                )
            if int(live_level) != self.profile.api_level:
                raise PhoneError(
                    "ownership_mismatch",
                    f"Live device API level on {self.serial} is {int(live_level)}, profile "
                    f"'{self.profile_id}' requires {self.profile.api_level}",
                    detail={"reported_api_level": int(live_level),
                            "expected_api_level": self.profile.api_level,
                            "profile_id": self.profile_id},
                )
            abi_list = self._live_getprop("ro.product.cpu.abilist") or self._live_getprop("ro.product.cpu.abi")
            supported = [part.strip() for part in abi_list.replace(",", " ").split() if part.strip()]
            if self.profile.abi not in supported:
                raise PhoneError(
                    "ownership_mismatch",
                    f"Live device ABIs {supported} on {self.serial} do not include profile "
                    f"'{self.profile_id}' ABI '{self.profile.abi}'",
                    detail={"reported_abis": supported, "expected_abi": self.profile.abi,
                            "profile_id": self.profile_id},
                )

    def dump_hierarchy_bytes(self, total_timeout: float = DUMP_TOTAL_TIMEOUT_S, is_launch: bool = False) -> Tuple[bytes, str]:
        """Dump UIAutomator hierarchy to remote temp file and read bytes.

        Performs at most 2 read-only collection attempts with bounded timeouts,
        individual remote/local file paths per attempt, full ElementTree XML validation,
        and guaranteed cleanup of temporary files strictly bounded by the total deadline.
        Never issues remote calls after deadline expiration. Never retries input actions.
        """
        if getattr(self, "read_backend", "legacy") == "stable-direct":
            read_started = time.monotonic()
            folder = os.path.dirname(os.path.abspath(__file__))
            module_path = os.path.join(folder, "stable_ui_read.py")
            jar_path = os.path.join(folder, "stable_ui_read.jar")
            try:
                with open(module_path, "rb") as fh:
                    if hashlib.sha256(fh.read()).hexdigest() != DIRECT_READER_PY_SHA256:
                        raise PhoneError("ownership_mismatch", "Stable reader module source hash changed")
                spec = importlib.util.spec_from_file_location("phone_stable_ui_read", module_path)
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                data, evidence = module.collect(self.adb, jar_path, DIRECT_READER_JAR_SHA256,
                                                self.output_dir, total_timeout - (time.monotonic() - read_started),
                                                is_launch=is_launch)
            except PhoneError:
                raise
            except Exception as exc:
                raise PhoneError("driver_timeout", "Stable direct UI read failed; no input retried",
                                 getattr(exc, "detail", {"error": str(exc)}))
            valid, reason = validate_hierarchy_xml(data)
            if not valid:
                raise PhoneError("driver_timeout", "Stable reader XML rejected", {"reason": reason})
            if time.monotonic() - read_started > total_timeout:
                raise PhoneError("driver_timeout", "Stable reader total deadline exceeded")
            self.latest_read_evidence = evidence
            return data, evidence

        if not hasattr(self.adb, "run") or not callable(self.adb.run):
            raise PhoneError("driver_error", "ADB controller must provide a bounded 'run' callable with timeout support")

        start_time = time.monotonic()
        deadline = start_time + total_timeout
        attempts_detail: List[Dict[str, Any]] = []

        last_rc: Optional[int] = None
        last_stdout: str = ""
        last_stderr: str = ""
        success_result: Optional[Tuple[bytes, str]] = None

        for attempt in range(1, MAX_DUMP_ATTEMPTS + 1):
            now = time.monotonic()
            if now >= deadline:
                break

            remote = f"{adb_control.REMOTE_PREFIX}-dump-{uuid.uuid4().hex}.xml"
            local_tmp = os.path.join(self.output_dir, f".tmp-dump-{uuid.uuid4().hex}.xml")

            attempt_record: Dict[str, Any] = {
                "attempt": attempt,
                "remote": remote,
                "local_tmp": local_tmp,
                "dump_rc": None,
                "pull_rc": None,
                "failure_reason": None,
            }

            try:
                # 1. Bounded uiautomator dump
                rem_dump = min(DUMP_CMD_TIMEOUT_S, deadline - time.monotonic())
                if rem_dump <= 0 or time.monotonic() >= deadline:
                    attempt_record["failure_reason"] = "deadline_exceeded_before_dump"
                    attempts_detail.append(attempt_record)
                    break

                rc, out, err = self.adb.run(["shell", "uiautomator", "dump", remote], timeout=rem_dump)
                attempt_record["dump_rc"] = rc
                attempt_record["dump_stdout"] = out
                attempt_record["dump_stderr"] = err
                last_rc = rc
                last_stdout = out
                last_stderr = err

                if rc != 0:
                    attempt_record["failure_reason"] = f"dump_exit_code_{rc}"
                else:
                    # 2. Bounded pull
                    rem_pull = min(PULL_CMD_TIMEOUT_S, deadline - time.monotonic())
                    if rem_pull <= 0 or time.monotonic() >= deadline:
                        attempt_record["failure_reason"] = "deadline_exceeded_before_pull"
                        attempts_detail.append(attempt_record)
                        break

                    pull_rc = 0
                    pull_out = ""
                    pull_err = ""
                    try:
                        res = self.adb.run(["pull", remote, local_tmp], timeout=rem_pull)
                        if isinstance(res, tuple) and len(res) >= 3:
                            pull_rc, pull_out, pull_err = res[0], res[1], res[2]
                    except Exception as exc:
                        pull_rc = 1
                        pull_err = str(exc)

                    attempt_record["pull_rc"] = pull_rc
                    attempt_record["pull_stdout"] = pull_out
                    attempt_record["pull_stderr"] = pull_err

                    if pull_rc != 0:
                        attempt_record["failure_reason"] = f"pull_failed: {pull_err.strip() or pull_out.strip() or pull_rc}"
                    elif not os.path.exists(local_tmp):
                        attempt_record["failure_reason"] = "pulled_file_missing_locally"
                    else:
                        try:
                            with open(local_tmp, "rb") as fh:
                                data = fh.read()
                        except OSError as exc:
                            data = b""
                            attempt_record["failure_reason"] = f"local_read_error: {exc}"

                        if not attempt_record["failure_reason"]:
                            # 3. Validate XML structure and root tag
                            valid, reason = validate_hierarchy_xml(data)
                            if not valid:
                                attempt_record["failure_reason"] = reason
                            else:
                                # Validate deadline before accepting success
                                if time.monotonic() > deadline:
                                    attempt_record["failure_reason"] = "deadline_exceeded_on_success"
                                else:
                                    success_result = (data, local_tmp)
                                    attempts_detail.append(attempt_record)
                                    break

                if attempt_record not in attempts_detail:
                    attempts_detail.append(attempt_record)

            except adb_control.AdbTimeout as exc:
                attempt_record["failure_reason"] = f"adb_timeout: {exc}"
                if attempt_record not in attempts_detail:
                    attempts_detail.append(attempt_record)
            except Exception as exc:
                attempt_record["failure_reason"] = f"unexpected_exception: {exc}"
                if attempt_record not in attempts_detail:
                    attempts_detail.append(attempt_record)
            finally:
                # Guaranteed cleanup of this attempt's temporary files
                if os.path.exists(local_tmp):
                    try:
                        os.remove(local_tmp)
                    except OSError:
                        pass
                rem_cleanup = deadline - time.monotonic()
                if rem_cleanup > 0:
                    cleanup_timeout = min(CLEANUP_CMD_TIMEOUT_S, rem_cleanup)
                    try:
                        self.adb.run(["shell", "rm", remote], timeout=cleanup_timeout)
                    except Exception as exc:
                        attempt_record["cleanup_error"] = str(exc)
                else:
                    attempt_record["cleanup_skipped"] = "deadline_exceeded"

            # Check deadline after cleanup if candidate success
            if success_result is not None:
                if time.monotonic() <= deadline:
                    return success_result
                attempt_record["failure_reason"] = "deadline_exceeded_after_cleanup"
                success_result = None

            # Log failure diagnostics for this attempt
            if attempt_record.get("failure_reason"):
                log_detail = {
                    "operation": "ui_dump",
                    "attempt": attempt,
                    "returncode": attempt_record.get("dump_rc") if attempt_record.get("dump_rc") is not None else 1,
                    "stdout": attempt_record.get("dump_stdout") or "",
                    "stderr": attempt_record.get("dump_stderr") or "",
                    "failure_reason": attempt_record["failure_reason"],
                }
                log(json.dumps(log_detail, ensure_ascii=False))

            # Bounded sleep before next attempt
            if attempt < MAX_DUMP_ATTEMPTS:
                rem_sleep = deadline - time.monotonic()
                if rem_sleep <= 0:
                    break
                sleep_duration = min(DUMP_RETRY_DELAY_S, rem_sleep)
                if sleep_duration > 0:
                    time.sleep(sleep_duration)

        # Final check if candidate success remained within deadline
        if success_result is not None and time.monotonic() <= deadline:
            return success_result

        # Failure handling after bounded retry exhaustion
        detail = {
            "operation": "ui_dump",
            "attempt": len(attempts_detail),
            "max_attempts": MAX_DUMP_ATTEMPTS,
            "total_timeout_s": total_timeout,
            "elapsed_s": round(time.monotonic() - start_time, 3),
            "returncode": last_rc if last_rc is not None else 1,
            "stdout": last_stdout,
            "stderr": last_stderr,
            "attempts": attempts_detail,
        }
        log(json.dumps(detail, ensure_ascii=False))
        raise PhoneError("driver_timeout", "UIAutomator read failed twice; no input retried", detail)

    def save_observation(self, xml_bytes: bytes, observed_at_utc: str) -> Tuple[ca.Observation, str, str]:
        """Persist observation XML and metadata index in session output_dir."""
        sha256 = hashlib.sha256(xml_bytes).hexdigest()
        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        xml_filename = f"hierarchy-{stamp}-{sha256[:8]}.xml"
        xml_path = os.path.join(self.output_dir, xml_filename)

        with open(xml_path, "wb") as fh:
            fh.write(xml_bytes)

        obs = ca.Observation(xml_bytes, source_name=xml_filename)

        meta = {
            "observation_id": obs.observation_id,
            "device_serial": self.serial,
            "avd": self.avd,
            "profile_id": self.profile_id,
            "observed_at": observed_at_utc,
            "xml_filename": xml_filename,
            "xml_path": os.path.abspath(xml_path),
            "sha256": sha256,
            "cli_version": CLI_VERSION,
            "read_backend": getattr(self, "read_backend", "legacy"),
        }
        with open(__file__, "rb") as source:
            meta["cli_source_sha256"] = hashlib.sha256(source.read()).hexdigest()
        meta_filename = f"observation-{obs.observation_id[:12]}.json"
        meta_path = os.path.join(self.output_dir, meta_filename)
        with open(meta_path, "w", encoding="utf-8") as fh:
            json.dump(meta, fh, ensure_ascii=False, indent=2)

        return obs, os.path.abspath(xml_path), meta_path

    def load_session_observation(self, observation_id: str) -> Tuple[ca.Observation, Dict[str, Any]]:
        """Retrieve a previously saved observation within this session with strict binding checks."""
        if not isinstance(observation_id, str) or not re.fullmatch(r"[0-9a-f]{64}", observation_id):
            raise PhoneError("invalid_argument", f"observation_id must be a full 64-char hex SHA-256 hash, got: {observation_id!r}")

        out_dir_real = os.path.realpath(self.output_dir)
        meta_filename = f"observation-{observation_id[:12]}.json"
        meta_path = os.path.realpath(os.path.join(out_dir_real, meta_filename))

        # Output dir boundary check for meta_path
        if meta_path != out_dir_real and not meta_path.startswith(out_dir_real + os.sep):
            raise PhoneError("ownership_mismatch", f"Observation metadata escapes session directory: {meta_path}")
        if not os.path.isfile(meta_path):
            raise PhoneError(
                "stale_observation",
                f"Observation {observation_id} not found in this session",
                detail={"observation_id": observation_id, "output_dir": self.output_dir},
            )
        try:
            with open(meta_path, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception as exc:
            raise PhoneError("invalid_argument", f"Failed to read observation metadata: {exc}")

        if meta.get("read_backend", "legacy") != getattr(self, "read_backend", "legacy"):
            raise PhoneError("ownership_mismatch", "Observation belongs to another read backend")

        # Check metadata IDs
        if meta.get("observation_id") != observation_id:
            raise PhoneError(
                "stale_observation",
                f"Observation ID mismatch in meta file: expected {observation_id}, found {meta.get('observation_id')}",
            )
        if meta.get("sha256") != observation_id:
            raise PhoneError(
                "stale_observation",
                f"Observation sha256 mismatch in meta file: expected {observation_id}, found {meta.get('sha256')}",
            )

        # Check device and AVD binding to current session
        if meta.get("device_serial") != self.serial:
            raise PhoneError(
                "ownership_mismatch",
                f"Observation device_serial '{meta.get('device_serial')}' does not match session serial '{self.serial}'",
                detail={"expected_serial": self.serial, "meta_serial": meta.get("device_serial")},
            )
        if meta.get("avd") != self.avd:
            raise PhoneError(
                "ownership_mismatch",
                f"Observation avd '{meta.get('avd')}' does not match session avd '{self.avd}'",
                detail={"expected_avd": self.avd, "meta_avd": meta.get("avd")},
            )
        # Profile binding: an observation taken under one fixed profile must not
        # be replayed under another. A baseline observation saved before
        # profiles existed has no profile_id and is still baseline-only.
        meta_profile = meta.get("profile_id")
        if meta_profile is None:
            if self.profile_id != profiles.BASELINE_PROFILE_ID:
                raise PhoneError(
                    "ownership_mismatch",
                    f"Observation has no profile_id and cannot be replayed under profile '{self.profile_id}'",
                    detail={"expected_profile": self.profile_id, "meta_profile": None},
                )
        elif meta_profile != self.profile_id:
            raise PhoneError(
                "ownership_mismatch",
                f"Observation profile_id '{meta_profile}' does not match session profile '{self.profile_id}'",
                detail={"expected_profile": self.profile_id, "meta_profile": meta_profile},
            )

        # Check xml path and filename binding
        xml_filename = meta.get("xml_filename")
        if not xml_filename or not isinstance(xml_filename, str):
            raise PhoneError("ownership_mismatch", "Observation meta missing valid xml_filename")
        expected_xml_path = os.path.realpath(os.path.join(out_dir_real, xml_filename))
        raw_xml_path = meta.get("xml_path")
        if not raw_xml_path or not isinstance(raw_xml_path, str):
            raise PhoneError("ownership_mismatch", "Observation meta missing valid xml_path")
        real_xml_path = os.path.realpath(raw_xml_path)
        if real_xml_path != expected_xml_path:
            raise PhoneError(
                "ownership_mismatch",
                f"Observation xml_path '{raw_xml_path}' does not match expected session path '{expected_xml_path}'",
                detail={"expected_xml_path": expected_xml_path, "meta_xml_path": raw_xml_path},
            )
        if not (real_xml_path == out_dir_real or real_xml_path.startswith(out_dir_real + os.sep)):
            raise PhoneError("ownership_mismatch", f"Observation XML escapes session directory: {real_xml_path}")
        if not os.path.isfile(real_xml_path):
            raise PhoneError("stale_observation", f"Stored XML file missing for observation: {real_xml_path}")

        # Check XML bytes hash matches observation_id
        with open(real_xml_path, "rb") as fh:
            xml_bytes = fh.read()
        actual_hash = hashlib.sha256(xml_bytes).hexdigest()
        if actual_hash != observation_id:
            raise PhoneError(
                "stale_observation",
                f"Stored XML bytes hash {actual_hash} does not match observation_id {observation_id}",
                detail={"expected_observation_id": observation_id, "actual_xml_hash": actual_hash},
            )

        obs = ca.Observation(xml_bytes, source_name=xml_filename)
        if obs.observation_id != observation_id:
            raise PhoneError(
                "stale_observation",
                f"Parsed observation ID {obs.observation_id} does not match requested {observation_id}",
            )
        return obs, meta

    def observe_live(self, is_launch: bool = False) -> Tuple[Dict[str, Any], str]:
        """Perform full live observe: dump, parse, persist, and return contract data."""
        self.verify_live_device()
        observed_at = datetime.now(timezone.utc).isoformat()
        xml_bytes, _ = self.dump_hierarchy_bytes(is_launch=is_launch)
        obs, abs_xml_path, _ = self.save_observation(xml_bytes, observed_at)

        data = {
            "observation_id": obs.observation_id,
            "nodes": [obs.node_dict(it) for it in obs.items],
            "device_serial": self.serial,
            "avd": self.avd,
            "profile_id": self.profile_id,
            "observed_at": observed_at,
            "coverage": "partial",
            "xml_path": abs_xml_path,
        }
        data["read_backend"] = getattr(self, "read_backend", "legacy")
        if data["read_backend"] == "stable-direct":
            data["read_evidence"] = self.latest_read_evidence
        return data, abs_xml_path

    # -- Commands --

    def cmd_status(self) -> Tuple[Dict[str, Any], List[str]]:
        self.verify_live_device()
        data = {
            "ready": True,
            "device_serial": self.serial,
            "avd": self.avd,
            "profile_id": self.profile_id,
        }
        return data, []

    def cmd_launch(self, package: str) -> Tuple[Dict[str, Any], List[str]]:
        if package not in ALLOWED_PACKAGES:
            raise PhoneError(
                "invalid_argument",
                f"Package '{package}' is not in the approved package list",
                detail={"package": package, "allowed_packages": list(ALLOWED_PACKAGES)},
            )
        self.verify_live_device()
        # Launch using monkey launcher intent with host monotonic timing recording
        monkey_t0 = time.monotonic()
        monkey_t1 = None
        rc = -1
        out = ""
        err = ""
        adb_exc = None
        try:
            rc, out, err = self.adb.run([
                "shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"
            ])
            monkey_t1 = time.monotonic()
        except Exception as exc:
            monkey_t1 = time.monotonic()
            adb_exc = exc
            err = str(exc)
        finally:
            if monkey_t1 is None:
                monkey_t1 = time.monotonic()
            self.latest_launch_monkey = {
                "start_monotonic": monkey_t0,
                "end_monotonic": monkey_t1,
                "elapsed_s": round(monkey_t1 - monkey_t0, 4),
                "rc": rc,
                "exception": repr(adb_exc) if adb_exc is not None else None,
            }
        if adb_exc is not None:
            if isinstance(adb_exc, adb_control.AdbNotFound):
                raise PhoneError("device_unavailable", str(adb_exc),
                                 detail={"monkey_telemetry": self.latest_launch_monkey}) from adb_exc
            elif isinstance(adb_exc, (adb_control.AdbTimeout, adb_control.AdbError)):
                raise PhoneError("driver_timeout", str(adb_exc),
                                 detail={"monkey_telemetry": self.latest_launch_monkey}) from adb_exc
            elif isinstance(adb_exc, PhoneError):
                if isinstance(adb_exc.detail, dict):
                    adb_exc.detail.setdefault("monkey_telemetry", self.latest_launch_monkey)
                raise adb_exc
            else:
                raise PhoneError("verification_failed", f"Unexpected failure: {adb_exc}",
                                 detail={"monkey_telemetry": self.latest_launch_monkey}) from adb_exc
        if rc != 0:
            raise PhoneError("driver_timeout", f"Failed to launch {package}: {err.strip() or out.strip()}",
                             detail={"monkey_telemetry": self.latest_launch_monkey})

        # Observe screen after launch with launch pacing enabled
        try:
            after_obs_data, xml_path = self.observe_live(is_launch=True)
        except PhoneError as exc:
            if hasattr(self, "latest_launch_monkey") and isinstance(exc.detail, dict):
                exc.detail["monkey_telemetry"] = self.latest_launch_monkey
            raise
        data = {
            "package": package,
            "after_observation": after_obs_data,
            "monkey_telemetry": self.latest_launch_monkey,
        }
        return data, [xml_path]

    def cmd_observe(self) -> Tuple[Dict[str, Any], List[str]]:
        data, xml_path = self.observe_live(is_launch=False)
        return data, [xml_path]

    def cmd_click(self, observation_id: str, node_id: str) -> Tuple[Dict[str, Any], List[str]]:
        """Click a resolved node, labelling the stage of any failure.

        The success envelope is byte-identical to the previous revision. Only
        the failure envelope gains ``detail.stage`` and ``detail.click_stage``;
        code, message, artifacts and every pre-existing detail field are kept
        verbatim, and no automatic click retry is added.

        Every exception that the frozen ``phone.main`` would have handled is
        re-raised here with **the same code and message that main produces**,
        so adding a stage can never change the error contract:

            PhoneError    -> its own code / message / detail
            AdbTimeout    -> driver_timeout, str(exc)
            AdbNotFound   -> device_unavailable, str(exc)
            AdbError      -> driver_timeout, str(exc)
            anything else -> verification_failed, "Unexpected failure: {exc}"

        A native exception raised inside the device preflight is therefore
        reported exactly as before, now with a stage attached.
        """
        trace = ClickTrace(self.profile_id, self.serial)
        try:
            return self._cmd_click_traced(trace, observation_id, node_id)
        except PhoneError as exc:
            raise PhoneError(exc.code, exc.message, attach_click_stage(exc.detail, trace))
        except adb_control.AdbTimeout as exc:
            raise PhoneError("driver_timeout", str(exc), attach_click_stage(None, trace))
        except adb_control.AdbNotFound as exc:
            raise PhoneError("device_unavailable", str(exc), attach_click_stage(None, trace))
        except adb_control.AdbError as exc:
            raise PhoneError("driver_timeout", str(exc), attach_click_stage(None, trace))
        except Exception as exc:
            raise PhoneError("verification_failed", "Unexpected failure: %s" % exc,
                             attach_click_stage(None, trace))

    def _save_pre_input_mismatch_evidence(
        self,
        observation_id: str,
        meta: Dict[str, Any],
        node_id: str,
        current_bytes: bytes,
        current_hash: str,
        read_artifact: str,
        trace: "ClickTrace",
    ) -> Dict[str, Any]:
        """Persist raw pre-input XML bytes and verifiable binding metadata on click hash mismatch.

        Called ONLY when pre_input_read detects current_hash != observation_id before any input.
        Writes raw current_bytes verbatim using exclusive creation ('xb') inside self.output_dir
        without re-serializing XML or overwriting any observation-*.json / hierarchy-*.xml file.
        """
        out_dir_real = os.path.realpath(self.output_dir)
        if out_dir_real != APPROVED_OUTPUT_ROOT and not out_dir_real.startswith(APPROVED_OUTPUT_ROOT + os.sep):
            raise OSError(f"Session output_dir escapes approved root: {out_dir_real}")

        captured_at_utc = datetime.now(timezone.utc).isoformat()
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        unique_token = uuid.uuid4().hex[:8]
        base_stem = f"pre-input-mismatch-{stamp}-{current_hash[:12]}-{unique_token}"
        raw_xml_filename = f"{base_stem}.xml"
        meta_filename = f"{base_stem}.json"
        raw_xml_path = os.path.realpath(os.path.join(out_dir_real, raw_xml_filename))
        meta_path = os.path.realpath(os.path.join(out_dir_real, meta_filename))

        for candidate_path in (raw_xml_path, meta_path):
            if not candidate_path.startswith(out_dir_real + os.sep):
                raise OSError(f"Evidence path escapes session directory: {candidate_path}")

        with open(raw_xml_path, "xb") as xml_fh:
            xml_fh.write(current_bytes)

        read_backend = getattr(self, "read_backend", "legacy")
        read_evidence = getattr(self, "latest_read_evidence", None) if read_backend == "stable-direct" else read_artifact
        evidence_record: Dict[str, Any] = {
            "schema": "pre_input_mismatch_evidence_v1",
            "captured_at_utc": captured_at_utc,
            "stage": trace.stage,
            "input_attempted": trace.input_attempted,
            "input_phase": trace.input_phase,
            "input_send_requested": bool(trace.send.get("requested")),
            "expected_observation_id": observation_id,
            "expected_xml_filename": meta.get("xml_filename"),
            "expected_xml_path": meta.get("xml_path"),
            "current_hash": current_hash,
            "current_bytes_len": len(current_bytes),
            "current_xml_filename": raw_xml_filename,
            "current_xml_path": raw_xml_path,
            "requested_node_id": node_id,
            "read_backend": read_backend,
            "read_evidence": read_evidence,
            "profile_id": self.profile_id,
            "avd": self.avd,
            "device_serial": self.serial,
            "evidence_meta_filename": meta_filename,
            "evidence_meta_path": meta_path,
        }
        with open(meta_path, "xb") as meta_fh:
            meta_fh.write((json.dumps(evidence_record, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
        return evidence_record

    def _cmd_click_traced(self, trace: "ClickTrace", observation_id: str,
                          node_id: str) -> Tuple[Dict[str, Any], List[str]]:
        trace.enter("verify_live_device")
        self.verify_live_device()
        trace.enter("load_observation")
        obs, meta = self.load_session_observation(observation_id)

        # Staleness check: dump current UI and compare hash
        trace.enter("pre_input_read")
        current_bytes, read_artifact = self.dump_hierarchy_bytes()
        current_hash = hashlib.sha256(current_bytes).hexdigest()
        if current_hash != observation_id:
            mismatch_detail: Dict[str, Any] = {
                "expected_observation_id": observation_id,
                "current_hash": current_hash,
            }
            try:
                evidence_record = self._save_pre_input_mismatch_evidence(
                    observation_id=observation_id,
                    meta=meta,
                    node_id=node_id,
                    current_bytes=current_bytes,
                    current_hash=current_hash,
                    read_artifact=read_artifact,
                    trace=trace,
                )
                mismatch_detail["pre_input_mismatch_evidence"] = evidence_record
            except Exception as exc:
                mismatch_detail["pre_input_mismatch_evidence"] = None
                mismatch_detail["pre_input_mismatch_evidence_error"] = f"{type(exc).__name__}: {exc}"
            raise PhoneError(
                "stale_observation",
                "UI changed since observation (dump hash mismatch)",
                detail=mismatch_detail,
            )

        # Resolve node and action plan using DS-01
        trace.enter("resolve_target")
        try:
            it = obs.item_by_node_id(node_id)
        except ca.AdapterError as exc:
            if exc.code in ("invalid_node_id", "stale_node_id"):
                raise PhoneError("stale_observation", exc.message, detail=exc.detail)
            if exc.code == "unknown_node_id":
                raise PhoneError("unknown_node_id", exc.message, detail=exc.detail)
            raise PhoneError("unsupported_target", exc.message, detail=exc.detail)

        # Plan click via ancestor chain
        sio = io.StringIO()
        try:
            with contextlib.redirect_stdout(sio):
                ca.cmd_resolve(argparse.Namespace(
                    xml=meta["xml_path"], node_id=node_id, action="click", direction=None
                ))
            resolve_out = json.loads(sio.getvalue())
        except ca.AdapterError as exc:
            raise PhoneError("unsupported_target", exc.message, detail=exc.detail)

        plan = resolve_out.get("plan")
        target_node_id = resolve_out.get("target_node_id")
        if not plan or plan.get("action") != "tap":
            raise PhoneError("unsupported_target", "Resolved plan is not an executable tap")

        x, y = plan["x"], plan["y"]
        # Entering the ADB send. Whatever happens next, this fact is recorded
        # before the call, because a client-side timeout cannot prove the tap
        # never reached the device. No retry is ever attempted on this path,
        # and the original exception is re-raised untouched so that the code
        # and message stay exactly what the frozen main maps them to.
        trace.enter("send_input")
        trace.input_send_started(x, y)
        try:
            self.adb.input_tap(x, y)
        except Exception as exc:
            trace.input_send_unconfirmed(send_failure_kind(exc), str(exc))
            raise
        # The tap command returned success. That is proof of delivery only, not
        # proof that the application reached the intended state.
        trace.input_send_returned()

        # Re-observe post-execution
        trace.enter("post_input_read")
        after_obs_data, new_xml_path = self.observe_live()
        data = {
            "target_node_id": target_node_id,
            "plan": plan,
            "after_observation": after_obs_data,
        }
        return data, [new_xml_path]

    def _find_target_in_observation(self, obs: ca.Observation, orig_item: ca.Item, orig_bounds: List[int], allow_focused_resize: bool = False) -> ca.Item:
        """Strictly locate the exact target EditText in an observation snapshot.

        Identity requirements:
        1. Exact path/class/package/resource identity; normally identical bounds.
           Readback may allow a focused field's right edge to resize while its
           left/top/bottom anchors stay fixed (clear-icon layout transition).
        2. Or exact resource-id (if non-null) + exact bounds: must be an enabled EditText.
        Refuses ambiguous matches, missing nodes, disabled nodes, or non-EditText nodes.
        Never falls back to fuzzy rectangle centers or 'sole remaining EditText'.
        """
        target_path = orig_item.path
        orig_res_id = ca.norm_str(orig_item.attr("resource-id"))

        # 1. Exact path match
        for it in obs.items:
            if it.path == target_path:
                cls_name = ca.norm_str(it.attr("class")) or ""
                if cls_name != ca.norm_str(orig_item.attr("class")) or not cls_name.endswith("EditText"):
                    raise PhoneError("unsupported_target", f"Target node at path {target_path} is not an EditText (class={cls_name})")
                if ca.norm_bool(it.attr("enabled")) is not True:
                    raise PhoneError("unsupported_target", f"Target EditText at path {target_path} is not enabled")
                if ca.norm_str(it.attr("package")) != ca.norm_str(orig_item.attr("package")) or ca.norm_str(it.attr("resource-id")) != orig_res_id:
                    raise PhoneError("unsupported_target", "Target package/resource identity changed after input")
                b = ca.parse_bounds(it.attr("bounds"))
                if b != orig_bounds:
                    # Clearing F-Droid's query removes its adjacent clear icon:
                    # the same focused field expands at the right edge. This
                    # is readback identification, never a new coordinate tap.
                    same_anchor = bool(b and (b[0], b[1], b[3]) == (orig_bounds[0], orig_bounds[1], orig_bounds[3]))
                    if not (allow_focused_resize and same_anchor and ca.norm_bool(it.attr("focused")) is True):
                        raise PhoneError("unsupported_target", f"Target EditText at path {target_path} changed bounds from {orig_bounds} to {b}")
                return it

        # 2. Exact resource-id + exact bounds match
        if orig_res_id is not None:
            matches = []
            for it in obs.items:
                if (ca.norm_str(it.attr("resource-id")) == orig_res_id and
                        ca.parse_bounds(it.attr("bounds")) == orig_bounds):
                    matches.append(it)
            if len(matches) == 1:
                cand = matches[0]
                cls_name = ca.norm_str(cand.attr("class")) or ""
                if not cls_name.endswith("EditText"):
                    raise PhoneError("unsupported_target", f"Matching node is not an EditText (class={cls_name})")
                if ca.norm_bool(cand.attr("enabled")) is not True:
                    raise PhoneError("unsupported_target", "Matching EditText is not enabled")
                return cand
            if len(matches) > 1:
                raise PhoneError("unsupported_target", f"Ambiguous matches for target EditText with resource-id '{orig_res_id}'")

        raise PhoneError("unsupported_target", f"Target EditText lost or structural change detected for node {orig_item.node_id}")

    def cmd_set_text(self, observation_id: str, node_id: str, value: str) -> Tuple[Dict[str, Any], List[str]]:
        if not isinstance(value, str) or not TEXT_RE.fullmatch(value):
            raise PhoneError(
                "invalid_argument",
                f"Value must be ASCII [A-Za-z0-9] up to 200 chars, got: {value!r}",
                detail={"value": value},
            )
        self.verify_live_device()
        obs, meta = self.load_session_observation(observation_id)

        # Verify target node is an enabled EditText in requested observation
        try:
            it = obs.item_by_node_id(node_id)
        except ca.AdapterError as exc:
            if exc.code == "unknown_node_id":
                raise PhoneError("unknown_node_id", exc.message, detail=exc.detail)
            raise PhoneError("stale_observation", exc.message, detail=exc.detail)

        node_dict = obs.node_dict(it)
        cls_name = node_dict.get("class") or ""
        enabled = node_dict.get("enabled")
        if not cls_name.endswith("EditText") or enabled is not True:
            raise PhoneError(
                "unsupported_target",
                f"Target node {node_id} is not an enabled EditText (class={cls_name}, enabled={enabled})",
                detail={"node_id": node_id, "class": cls_name, "enabled": enabled},
            )
        bounds = node_dict.get("bounds")
        if not bounds:
            raise PhoneError("unsupported_target", f"Target node {node_id} has invalid bounds")

        # Live target check: before touching device, dump current UI and confirm target node exists and is enabled EditText
        pre_dump_bytes, _ = self.dump_hierarchy_bytes()
        pre_obs = ca.Observation(pre_dump_bytes, source_name="pre_input_verification.xml")
        live_target = self._find_target_in_observation(pre_obs, it, bounds)
        old_text = live_target.attr("text") or ""
        if len(old_text) > 200:
            raise PhoneError("unsupported_target", "Existing input exceeds the supported 200-character clearing limit")
        cx, cy = ca.center(bounds)

        # 1. Tap to focus EditText
        self.adb.input_tap(cx, cy)

        # A successful tap does not prove where keyboard events will go.
        # Check the actual focus before sending destructive clear events.
        focus_bytes, _ = self.dump_hierarchy_bytes()
        focus_obs = ca.Observation(focus_bytes, source_name="focus_verification.xml")
        focused_target = self._find_target_in_observation(focus_obs, it, bounds, allow_focused_resize=True)
        if ca.norm_bool(focused_target.attr("focused")) is not True:
            raise PhoneError("unsupported_target", "Target EditText did not receive focus; no clear or text sent")

        # 2. Clear old value with a budget derived from what this field is
        #    proven to contain, instead of a fixed 220 keyevents per direction.
        #
        #    Both pre-clear observations are already in hand: `old_text` comes
        #    from the fresh pre-input dump of this exact node, and the
        #    focus-verified dump was just taken for the same node.  Nothing
        #    destructive has been sent yet.
        #
        #      * If both prove the field is already empty, no keyevent can
        #        change anything, so the whole clear sequence is skipped.  The
        #        skip is the only new decision point and it is still proven
        #        afterwards by the unchanged independent emptiness dump below:
        #        a wrong decision fails closed before any text is typed.
        #      * Otherwise, when the verified content satisfies the same
        #        ASCII[0-200] contract this command accepts from callers, one
        #        keyevent removes exactly one character, so the budget is the
        #        verified length, capped at CLEAR_LIMIT.
        #      * Anything else (Unicode, IME composition, dynamic text) keeps
        #        the historical LEGACY_CLEAR_COUNT verbatim: the candidate does
        #        not claim safety there and does not narrow behaviour there.
        focus_text = focused_target.attr("text") or ""
        if len(focus_text) > 200:
            raise PhoneError("unsupported_target", "Existing input exceeds the supported 200-character clearing limit")
        if not old_text and not focus_text:
            clear_count = 0
        elif TEXT_RE.fullmatch(old_text) and TEXT_RE.fullmatch(focus_text):
            clear_count = min(CLEAR_LIMIT, max(len(old_text), len(focus_text)))
        else:
            clear_count = LEGACY_CLEAR_COUNT

        if clear_count:
            rc, out, err = self.adb.run(["shell", "input", "keyevent", "123"])  # KEYCODE_MOVE_END
            if rc != 0:
                raise PhoneError("driver_timeout", f"Failed to send MOVE_END: {err.strip() or out.strip()}")
            rc, out, err = self.adb.run(["shell", "input", "keyevent"] + ["67"] * clear_count)  # KEYCODE_DEL
            if rc != 0:
                raise PhoneError("driver_timeout", f"Failed to send DEL keyevents: {err.strip() or out.strip()}")
            rc, out, err = self.adb.run(["shell", "input", "keyevent", "122"])  # KEYCODE_MOVE_HOME
            if rc != 0:
                raise PhoneError("driver_timeout", f"Failed to send MOVE_HOME: {err.strip() or out.strip()}")
            rc, out, err = self.adb.run(["shell", "input", "keyevent"] + ["112"] * clear_count)  # KEYCODE_FORWARD_DEL
            if rc != 0:
                raise PhoneError("driver_timeout", f"Failed to send FORWARD_DEL keyevents: {err.strip() or out.strip()}")

        # 3. Intermediate dump: verify field is confirmed EMPTY before typing value
        clear_bytes, _ = self.dump_hierarchy_bytes()
        clear_obs = ca.Observation(clear_bytes, source_name="clear_verification.xml")
        target_after_clear = self._find_target_in_observation(clear_obs, it, bounds, allow_focused_resize=True)
        if ca.norm_bool(target_after_clear.attr("focused")) is not True:
            raise PhoneError("unsupported_target", "Target EditText lost focus after clearing; no text sent")
        cleared_text = target_after_clear.attr("text") or ""
        if cleared_text != "":
            raise PhoneError(
                "verification_failed",
                f"Failed to clear EditText before input: field still contains text {cleared_text!r}",
                detail={"remaining_text": cleared_text, "clear_count": clear_count},
            )

        # 4. Input new value if non-empty
        if value:
            self.adb.input_text(value)

        # 5. Post-execution observe and exact readback verification
        after_obs_data, new_xml_path = self.observe_live()
        with open(new_xml_path, "rb") as fh:
            final_xml_bytes = fh.read()
        final_obs = ca.Observation(final_xml_bytes, source_name=os.path.basename(new_xml_path))
        final_target = self._find_target_in_observation(final_obs, it, bounds, allow_focused_resize=True)

        readback_text = final_target.attr("text") or ""
        if readback_text != value:
            raise PhoneError(
                "verification_failed",
                f"Verification failed: expected {value!r}, read back {readback_text!r}",
                detail={"expected_text": value, "readback_text": readback_text},
            )

        data = {
            "input_text": value,
            "readback_text": readback_text,
            "verified": True,
            "after_observation": after_obs_data,
        }
        return data, [new_xml_path]

    def cmd_evaluate_search(self, observation_id: str, query: str) -> Tuple[Dict[str, Any], List[str]]:
        if not query or not query.strip():
            raise PhoneError("invalid_argument", "--query must not be empty")
        obs, meta = self.load_session_observation(observation_id)

        sio = io.StringIO()
        try:
            with contextlib.redirect_stdout(sio):
                ca.cmd_evaluate_search(argparse.Namespace(
                    xml=meta["xml_path"], query=query.strip()
                ))
            eval_payload = json.loads(sio.getvalue())
        except ca.AdapterError as exc:
            raise PhoneError(exc.code, exc.message, detail=exc.detail)

        eval_payload["device_serial"] = self.serial
        eval_payload["profile_id"] = self.profile_id
        eval_payload["observed_at"] = meta.get("observed_at")
        return eval_payload, [meta["xml_path"]]

    def cmd_screenshot(self, reason: str) -> Tuple[Dict[str, Any], List[str]]:
        if not reason or not reason.strip():
            raise PhoneError("invalid_argument", "--reason must be provided and non-empty")
        self.verify_live_device()

        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        remote_png = f"{adb_control.REMOTE_PREFIX}-screenshot-{uuid.uuid4().hex}.png"
        local_png = os.path.join(self.output_dir, f"screenshot-{stamp}-{uuid.uuid4().hex[:8]}.png")

        try:
            self.adb.screencap(remote_png)
            try:
                self.adb.pull(remote_png, local_png)
            finally:
                self.adb.remove_remote(remote_png)
        except Exception as exc:
            raise PhoneError("driver_timeout", f"Screenshot failed: {exc}")

        data = {
            "reason": reason.strip(),
            "artifact": os.path.abspath(local_png),
        }
        return data, [os.path.abspath(local_png)]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phone.py",
        description="Unified Phone CLI v0.1 for Sandboxed Android Emulator.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + CLI_VERSION)
    parser.add_argument("--adb", required=True, help="Path to adb.exe")
    parser.add_argument("--state-file", required=True, dest="state_file", help="Path to instance.json")
    parser.add_argument("--output-dir", required=True, dest="output_dir", help="Session output directory")
    parser.add_argument("--profile", dest="profile_id", default=profiles.DEFAULT_PROFILE_ID,
                        help="Fixed instance profile id (default: %s). "
                             "A non-baseline instance must be selected explicitly." % profiles.DEFAULT_PROFILE_ID)

    parser.add_argument("--read-backend", choices=("legacy", "stable-direct"), default="legacy",
                        help="UI collector (stable-direct is explicit API35 only)")

    sub = parser.add_subparsers(dest="action", metavar="<command>", required=True)

    # status
    sub.add_parser("status", help="Check emulator status and ownership")

    # launch
    p_launch = sub.add_parser("launch", help="Launch approved app")
    p_launch.add_argument("--package", required=True, help="Package name (only org.fdroid.basic)")

    # observe
    sub.add_parser("observe", help="Capture structured UI observation")

    # click
    p_click = sub.add_parser("click", help="Click target node")
    p_click.add_argument("--observation-id", required=True, dest="observation_id", help="Full hash of observation")
    p_click.add_argument("--node-id", required=True, dest="node_id", help="Node ID to click")

    # set-text
    p_text = sub.add_parser("set-text", help="Input text into EditText")
    p_text.add_argument("--observation-id", required=True, dest="observation_id", help="Full hash of observation")
    p_text.add_argument("--node-id", required=True, dest="node_id", help="Node ID of EditText")
    p_text.add_argument("--value", required=True, help="ASCII text [A-Za-z0-9]{0,200}")

    # evaluate-search
    p_eval = sub.add_parser("evaluate-search", help="Evaluate F-Droid search results")
    p_eval.add_argument("--observation-id", required=True, dest="observation_id", help="Full hash of observation")
    p_eval.add_argument("--query", required=True, help="Search query string")

    # screenshot
    p_shot = sub.add_parser("screenshot", help="Capture diagnostic screenshot")
    p_shot.add_argument("--reason", required=True, help="Reason for taking screenshot")

    return parser


def main(argv: Optional[List[str]] = None, runner: Optional[Any] = None) -> int:
    parser = build_parser()
    args: Optional[argparse.Namespace] = None
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as exc:
        return emit_failure(None, "invalid_argument", str(exc))
    except SystemExit as exc:
        if exc.code in (0, None):
            return 0
        return emit_failure(None, "invalid_argument", "Missing or invalid arguments")

    action = args.action
    try:
        controller = PhoneController(
            adb_path=args.adb,
            state_file=args.state_file,
            output_dir=args.output_dir,
            runner=runner,
            profile_id=args.profile_id,
            read_backend=args.read_backend,
        )
    except PhoneError as exc:
        return emit_failure(action, exc.code, exc.message, exc.detail)
    except Exception as exc:
        return emit_failure(action, "device_unavailable", f"Controller initialization failed: {exc}")

    try:
        if action == "status":
            data, artifacts = controller.cmd_status()
            return emit_success(action, data, artifacts)
        elif action == "launch":
            data, artifacts = controller.cmd_launch(args.package)
            return emit_success(action, data, artifacts)
        elif action == "observe":
            data, artifacts = controller.cmd_observe()
            return emit_success(action, data, artifacts)
        elif action == "click":
            data, artifacts = controller.cmd_click(args.observation_id, args.node_id)
            return emit_success(action, data, artifacts)
        elif action == "set-text":
            data, artifacts = controller.cmd_set_text(args.observation_id, args.node_id, args.value)
            return emit_success(action, data, artifacts)
        elif action == "evaluate-search":
            data, artifacts = controller.cmd_evaluate_search(args.observation_id, args.query)
            return emit_success(action, data, artifacts)
        elif action == "screenshot":
            data, artifacts = controller.cmd_screenshot(args.reason)
            return emit_success(action, data, artifacts)
        else:
            return emit_failure(action, "invalid_argument", f"Unknown action: {action}")
    except PhoneError as exc:
        return emit_failure(action, exc.code, exc.message, exc.detail)
    except adb_control.AdbTimeout as exc:
        return emit_failure(action, "driver_timeout", str(exc))
    except adb_control.AdbNotFound as exc:
        return emit_failure(action, "device_unavailable", str(exc))
    except adb_control.AdbError as exc:
        return emit_failure(action, "driver_timeout", str(exc))
    except Exception as exc:
        return emit_failure(action, "verification_failed", f"Unexpected failure: {exc}")


if __name__ == "__main__":
    sys.exit(main())
