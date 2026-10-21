# Derived execution primitives; there is no fixed action sequence or executable main.
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import uuid
import hashlib
import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# Default constants
DEFAULT_PROJECT_ROOT = Path(r"C:\Users\LOCAL_USER\Documents\Codex\2026-10-14\new-chat-2")
DEFAULT_PYTHON = Path(r"C:\Users\LOCAL_USER\.workbuddy\binaries\python\envs\default\Scripts\python.exe")
DEFAULT_ADB = Path(r"C:\Users\LOCAL_USER\AppData\Local\Android\Sdk\platform-tools\adb.exe")
DEFAULT_MANIFEST = Path(r"C:\Users\LOCAL_USER\Documents\ChatGPT\手机沙箱\round5-memory\dispatch-r7\inputs\accepted-source-manifest.json")
DEFAULT_OUTPUT_BASE = Path(r"C:\Users\LOCAL_USER\Documents\ChatGPT\手机沙箱\round5-memory\dispatch-r7\deliveries\gemini-fdroid-single-case\\runs")
WIN_POWERSHELL_51 = Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe")

TOTAL_SESSION_TIMEOUT_S = 360.0
BOOT_TIMEOUT_S = 120.0
CLEANUP_BUDGET_S = 40.0
MAX_READINESS_OBSERVES = 2
READINESS_SHARED_BUDGET_S = 40.0
OBSERVE_BUDGET_S = 20.0
LAUNCH_CLI_CAP_S = 25.0
CLICK_BUDGET_S = 50.0
SET_TEXT_BUDGET_S = 90.0
SET_TEXT_MIN_REMAINING_S = 115.0
CMD_TIMEOUT_S = 8.0
CLEANUP_RM_TIMEOUT_S = 2.0
EXPECTED_AVD = "ai-sandbox-api35-google"
EXPECTED_API = "35"
EXPECTED_ABI = "x86_64"
TARGET_PACKAGE = "org.fdroid.basic"
ASCII_QUERY = "Aurora"
SERIAL_RE = re.compile(r"^emulator-(\d+)\Z")
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")
NONCE_RE = re.compile(r"^[0-9a-f]{32}\Z")

RECOVERABLE_LAUNCH_ERROR_MESSAGE = "Stable direct UI read failed; no input retried"
ALLOWED_LAUNCH_COLLECTOR_ERRORS = frozenset({
    "java.lang.IllegalStateException: null_root_after_bounded_readiness",
    "java.lang.IllegalStateException: window_changed",
})
ALLOWED_COLLECTOR_IDLE_RESULTS = frozenset({
    "short_idle_reached",
    "short_idle_timeout",
})


class RunnerError(Exception):
    """Base error for single-case runner."""
    def __init__(self, code: str, message: str, detail: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}


class ReadinessError(RunnerError):
    pass


class TargetAmbiguityError(RunnerError):
    pass


class TargetValidationError(RunnerError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def resolve_powershell_exe() -> str:
    return str(WIN_POWERSHELL_51) if WIN_POWERSHELL_51.is_file() else "powershell.exe"


def valid_emulator_serial(serial: Any) -> bool:
    if not isinstance(serial, str):
        return False
    m = SERIAL_RE.fullmatch(serial)
    if not m:
        return False
    port = int(m.group(1))
    return 0 < port <= 65534 and port % 2 == 0


def validate_stable_direct_trace(trace: Any, single_read_budget_s: float = OBSERVE_BUDGET_S, cmd_cap_s: float = CMD_TIMEOUT_S) -> Tuple[bool, Dict[str, Any]]:
    """Strictly audits one stable-direct-*.json trace file produced by stable_ui_read.collect()."""
    errors: List[str] = []
    if not isinstance(trace, dict):
        return False, {"errors": ["trace_not_dict"]}
    if trace.get("backend") != "stable-direct":
        errors.append(f"unexpected_backend:{trace.get('backend')!r}")
    tot_to = trace.get("total_timeout_s")
    if not isinstance(tot_to, (int, float)) or isinstance(tot_to, bool) or not (0 < tot_to <= single_read_budget_s):
        errors.append(f"invalid_total_timeout_s:{tot_to!r}")
    elapsed_s = trace.get("elapsed_s")
    if not isinstance(elapsed_s, (int, float)) or isinstance(elapsed_s, bool) or not (0 < elapsed_s <= single_read_budget_s):
        errors.append(f"elapsed_s_out_of_bound:{elapsed_s!r}")
    if trace.get("max_attempts") != 2:
        errors.append(f"unexpected_max_attempts:{trace.get('max_attempts')!r}")

    attempts = trace.get("attempts")
    if not isinstance(attempts, list) or not (1 <= len(attempts) <= 2):
        errors.append("invalid_attempts_list")
        return False, {"errors": errors}

    all_commands: List[Dict[str, Any]] = []
    cleanup_ok = True
    winning_proof: Optional[Dict[str, Any]] = None
    winning_nonce: Optional[str] = None

    for idx, att in enumerate(attempts, 1):
        if not isinstance(att, dict) or att.get("attempt") != idx:
            errors.append(f"attempt_{idx}_invalid_index")
            continue
        nonce = att.get("nonce")
        if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
            errors.append(f"attempt_{idx}_invalid_nonce")
        if att.get("cleanup_failure"):
            cleanup_ok = False
            errors.append(f"attempt_{idx}_cleanup_failure:{att.get('cleanup_failure')}")
        cmds = att.get("commands")
        if not isinstance(cmds, list) or not cmds:
            cleanup_ok = False
            errors.append(f"attempt_{idx}_missing_commands")
        else:
            all_commands.extend(c for c in cmds if isinstance(c, dict))
            for c in cmds:
                if not isinstance(c, dict):
                    errors.append(f"attempt_{idx}_command_not_dict")
                    continue
                c_to = c.get("timeout_s")
                if not isinstance(c_to, (int, float)) or isinstance(c_to, bool) or not (0 < c_to <= cmd_cap_s):
                    errors.append(f"attempt_{idx}_cmd_timeout_exceeded:{c_to!r}")
            last_cmd = cmds[-1] if isinstance(cmds[-1], dict) else {}
            last_args = last_cmd.get("args") or []
            if last_cmd.get("rc") != 0 or last_args[:3] != ["shell", "rm", "-f"]:
                cleanup_ok = False
                errors.append(f"attempt_{idx}_missing_or_failed_rm_cleanup")

        if "proof" in att:
            winning_proof = att["proof"]
            winning_nonce = nonce

    trace_ok = trace.get("ok") is True
    verified_sha256: Optional[str] = None
    if trace_ok:
        if not isinstance(winning_proof, dict):
            errors.append("ok_trace_missing_proof")
        else:
            p = winning_proof
            if p.get("version") != 1 or p.get("nonce") != winning_nonce:
                errors.append("proof_version_or_nonce_mismatch")
            if p.get("ok") is not True or p.get("stable") is not True or p.get("snapshot_count") != 2:
                errors.append("proof_stability_flags_invalid")
            gap = p.get("snapshot_gap_ms")
            if type(gap) is not int or gap < 300:
                errors.append(f"snapshots_not_separated:{gap!r}")
            if p.get("cache_clear") != [True, True]:
                errors.append(f"fresh_cache_reads_not_confirmed:{p.get('cache_clear')!r}")
            wid = p.get("window_id")
            if type(wid) is not int or wid < 0:
                errors.append(f"invalid_window_id:{wid!r}")
            pkg = p.get("package")
            if not isinstance(pkg, str) or not pkg or pkg == "null":
                errors.append(f"invalid_proof_package:{pkg!r}")
            h1, h2 = p.get("sha256_first"), p.get("sha256_second")
            if not (isinstance(h1, str) and SHA256_RE.fullmatch(h1) and h1 == h2):
                errors.append("proof_snapshot_hash_mismatch")
            else:
                verified_sha256 = h2

    max_cmd_s = max((c.get("timeout_s", 0.0) for c in all_commands), default=0.0)
    summary = {
        "ok": trace_ok,
        "elapsed_s": elapsed_s,
        "attempts": len(attempts),
        "cleanup_ok": cleanup_ok,
        "max_command_s": max_cmd_s,
        "verified_sha256": verified_sha256,
        "snapshot_gap_ms": winning_proof.get("snapshot_gap_ms") if isinstance(winning_proof, dict) else None,
        "cache_clear": winning_proof.get("cache_clear") if isinstance(winning_proof, dict) else None,
        "window_id": winning_proof.get("window_id") if isinstance(winning_proof, dict) else None,
        "package": winning_proof.get("package") if isinstance(winning_proof, dict) else None,
        "errors": errors,
    }
    return (len(errors) == 0 and cleanup_ok), summary


def classify_launch_post_read_recovery(
    launch_res: Any,
    cmd_row: Optional[Dict[str, Any]],
    single_read_budget_s: float = OBSERVE_BUDGET_S,
    cmd_cap_s: float = CMD_TIMEOUT_S,
) -> Tuple[bool, Dict[str, Any]]:
    """Strictly determines whether a failed launch CLI response is eligible for bounded
    read-only readiness recovery.

    Call-graph basis in phone.py:
    - PhoneController.cmd_launch validates package & live device, runs `adb shell monkey ... 1`.
    - If monkey returns non-zero, cmd_launch raises PhoneError("driver_timeout", "Failed to launch ...", detail=None).
    - Only after monkey returns rc==0 does cmd_launch invoke observe_live() -> dump_hierarchy_bytes() -> stable_ui_read.collect().
    - When stable_ui_read.collect() exhausts its 2 bounded read attempts, it raises DirectReadError(trace),
      which dump_hierarchy_bytes() maps to PhoneError("driver_timeout",
      "Stable direct UI read failed; no input retried", detail=trace).
    """
    reasons: List[str] = []
    collector_errors: List[str] = []
    nonces: List[str] = []

    if not isinstance(cmd_row, dict):
        reasons.append("missing_command_row")
    else:
        if cmd_row.get("label") != "launch-fdroid":
            reasons.append(f"unexpected_command_label:{cmd_row.get('label')!r}")
        if cmd_row.get("timed_out") is not False:
            reasons.append("outer_command_timed_out")
        if type(cmd_row.get("exit_code")) is not int or cmd_row.get("exit_code") != 1:
            reasons.append(f"unexpected_exit_code:{cmd_row.get('exit_code')!r}")
        c_to = cmd_row.get("timeout_s")
        c_el = cmd_row.get("elapsed_s")
        if not isinstance(c_to, (int, float)) or isinstance(c_to, bool) or not (0 < c_to <= LAUNCH_CLI_CAP_S):
            reasons.append(f"invalid_cmd_timeout_s:{c_to!r}")
        if not isinstance(c_el, (int, float)) or isinstance(c_el, bool) or not (0 < c_el <= (c_to if isinstance(c_to, (int, float)) else LAUNCH_CLI_CAP_S)):
            reasons.append(f"invalid_cmd_elapsed_s:{c_el!r}")

    if not isinstance(launch_res, dict):
        reasons.append("launch_response_not_dict")
        return False, {"recoverable": False, "reasons": reasons}

    if not {"ok", "action", "data", "error", "artifacts"}.issubset(launch_res):
        reasons.append("missing_launch_failure_envelope_fields")
    if launch_res.get("ok") is not False:
        reasons.append("launch_response_not_failed")
    if launch_res.get("action") != "launch":
        reasons.append(f"unexpected_action:{launch_res.get('action')!r}")
    if launch_res.get("data") is not None:
        reasons.append("failure_data_not_null")
    artifacts = launch_res.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 0:
        reasons.append("unexpected_launch_failure_artifacts")

    err = launch_res.get("error")
    if not isinstance(err, dict):
        reasons.append("missing_error_object")
        return False, {"recoverable": False, "reasons": reasons}

    if err.get("code") != "driver_timeout":
        reasons.append(f"unsupported_error_code:{err.get('code')!r}")
    if err.get("message") != RECOVERABLE_LAUNCH_ERROR_MESSAGE:
        reasons.append(f"unsupported_error_message:{err.get('message')!r}")

    detail = err.get("detail")
    if not isinstance(detail, dict):
        reasons.append("missing_error_detail")
        return False, {"recoverable": False, "reasons": reasons}

    if "reason" in detail:
        reasons.append(f"direct_reader_preflight_or_xml_rejection:{detail.get('reason')!r}")
    if detail.get("failure") is not None:
        reasons.append(f"trace_level_failure:{detail.get('failure')!r}")
    if detail.get("ok") is not False:
        reasons.append("trace_ok_not_false")

    trace_valid, trace_summary = validate_stable_direct_trace(detail, single_read_budget_s, cmd_cap_s)
    if not trace_valid:
        reasons.extend(f"trace_invalid:{e}" for e in trace_summary.get("errors", ["unknown"]))

    if trace_valid and detail["elapsed_s"] > detail["total_timeout_s"]:
        reasons.append("trace_elapsed_exceeds_declared_read_budget")
    trace_elapsed = detail.get("elapsed_s")
    if isinstance(cmd_row, dict) and isinstance(cmd_row.get("elapsed_s"), (int, float)) and isinstance(trace_elapsed, (int, float)):
        if trace_elapsed > cmd_row["elapsed_s"] + 0.5:
            reasons.append(f"trace_elapsed_exceeds_outer_elapsed:{trace_elapsed:.3f}>{cmd_row['elapsed_s']:.3f}")

    attempts = detail.get("attempts")
    if not isinstance(attempts, list) or len(attempts) != 2:
        reasons.append(f"expected_2_collector_attempts_got:{len(attempts) if isinstance(attempts, list) else None!r}")
    else:
        for idx, att in enumerate(attempts, 1):
            if not isinstance(att, dict):
                reasons.append(f"attempt_{idx}_not_dict")
                continue
            nonce = att.get("nonce")
            if not isinstance(nonce, str) or not NONCE_RE.fullmatch(nonce):
                reasons.append(f"attempt_{idx}_bad_nonce")
                continue
            if nonce in nonces:
                reasons.append(f"attempt_{idx}_duplicate_nonce")
            nonces.append(nonce)

            if "proof" in att:
                reasons.append(f"attempt_{idx}_unexpected_proof_on_failure")
            if att.get("cleanup_failure"):
                reasons.append(f"attempt_{idx}_cleanup_failure")
            att_fail = att.get("failure")
            if not isinstance(att_fail, str) or not att_fail.startswith("command failed: 1"):
                reasons.append(f"attempt_{idx}_unexpected_failure_marker:{att_fail!r}")

            cmds = att.get("commands")
            if not isinstance(cmds, list) or len(cmds) != 4:
                reasons.append(f"attempt_{idx}_expected_4_commands_got:{len(cmds) if isinstance(cmds, list) else None!r}")
                continue

            expected_jar = f"/data/local/tmp/ai-sandbox-direct-{nonce}.jar"
            expected_xml = f"/sdcard/Download/ai-sandbox-direct-{nonce}.xml"

            c_push, c_sha, c_app, c_rm = cmds[0], cmds[1], cmds[2], cmds[3]
            if not all(isinstance(c, dict) for c in (c_push, c_sha, c_app, c_rm)):
                reasons.append(f"attempt_{idx}_command_entry_not_dict")
                continue

            if any(type(c.get("rc")) is not int for c in (c_push, c_sha, c_app, c_rm)):
                reasons.append(f"attempt_{idx}_non_integer_command_rc")
            push_args = c_push.get("args") or []
            if (
                c_push.get("rc") != 0
                or len(push_args) != 3
                or push_args[0] != "push"
                or not str(push_args[1]).endswith("stable_ui_read.jar")
                or push_args[2] != expected_jar
            ):
                reasons.append(f"attempt_{idx}_invalid_push_command")

            sha_args = c_sha.get("args") or []
            if c_sha.get("rc") != 0 or sha_args != ["shell", "sha256sum", expected_jar]:
                reasons.append(f"attempt_{idx}_invalid_sha256sum_command")

            app_args = c_app.get("args") or []
            expected_cp = f"CLASSPATH={expected_jar}:/system/framework/uiautomator.jar"
            if (
                c_app.get("rc") != 1
                or len(app_args) != 11
                or app_args[:4] != ["shell", "timeout", "-s", "KILL"]
                or not str(app_args[4]).isdigit()
                or not (1 <= int(app_args[4]) <= int(cmd_cap_s))
                or app_args[5:] != ["env", expected_cp, "app_process", "/system/bin", "StableUiRead", expected_xml]
            ):
                reasons.append(f"attempt_{idx}_invalid_app_process_command")

            rm_args = c_rm.get("args") or []
            rm_to = c_rm.get("timeout_s")
            if (
                c_rm.get("rc") != 0
                or rm_args != ["shell", "rm", "-f", expected_jar, expected_xml]
                or not isinstance(rm_to, (int, float))
                or isinstance(rm_to, bool)
                or not (0 < rm_to <= CLEANUP_RM_TIMEOUT_S)
            ):
                reasons.append(f"attempt_{idx}_invalid_rm_cleanup_command")

            raw_stdout = c_app.get("failure_stdout")
            if not isinstance(raw_stdout, str) or not raw_stdout.strip():
                reasons.append(f"attempt_{idx}_missing_collector_failure_stdout")
                continue
            try:
                rep = json.loads(raw_stdout)
            except Exception:
                reasons.append(f"attempt_{idx}_collector_stdout_not_json")
                continue
            if not isinstance(rep, dict):
                reasons.append(f"attempt_{idx}_collector_report_not_dict")
                continue
            if type(rep.get("version")) is not int or rep.get("version") != 1 or rep.get("nonce") != nonce:
                reasons.append(f"attempt_{idx}_collector_version_or_nonce_mismatch")
            if rep.get("ok") is not False:
                reasons.append(f"attempt_{idx}_collector_ok_not_false")
            if rep.get("idle_result") not in ALLOWED_COLLECTOR_IDLE_RESULTS:
                reasons.append(f"attempt_{idx}_invalid_idle_result:{rep.get('idle_result')!r}")
            el_ms = rep.get("elapsed_ms")
            if type(el_ms) is not int or not (0 <= el_ms <= int(cmd_cap_s * 1000)):
                reasons.append(f"attempt_{idx}_invalid_collector_elapsed_ms:{el_ms!r}")
            c_err = rep.get("error")
            if c_err not in ALLOWED_LAUNCH_COLLECTOR_ERRORS:
                reasons.append(f"attempt_{idx}_disallowed_collector_error:{c_err!r}")
            else:
                collector_errors.append(c_err)

    recoverable = len(reasons) == 0 and len(collector_errors) == 2
    report = {
        "recoverable": recoverable,
        "classification": (
            "launch_post_monkey_stable_direct_read_refused_recoverable"
            if recoverable
            else "non_recoverable_launch_failure"
        ),
        "monkey_launch_inferred_rc_zero": recoverable,
        "outer_command_timed_out": cmd_row.get("timed_out") if isinstance(cmd_row, dict) else None,
        "outer_exit_code": cmd_row.get("exit_code") if isinstance(cmd_row, dict) else None,
        "outer_elapsed_s": cmd_row.get("elapsed_s") if isinstance(cmd_row, dict) else None,
        "trace_elapsed_s": detail.get("elapsed_s"),
        "attempts": len(attempts) if isinstance(attempts, list) else 0,
        "nonces": nonces,
        "collector_errors": collector_errors,
        "all_uuid_cleanup_rc_zero": trace_summary.get("cleanup_ok") is True,
        "max_device_command_timeout_s": trace_summary.get("max_command_s"),
        "reasons": reasons,
        "known_evidence_gap": (
            "Frozen StableUiRead.java reports java.lang.IllegalStateException: window_changed without "
            "emitting the two snapshots' individual window_id/package values on failure; recovery "
            "requires a subsequent independent full-tree stable-direct observation of org.fdroid.basic."
        ),
    }
    return recoverable, report


def is_fdroid_ready(obs_data: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """Strictly evaluates whether an observation demonstrates that org.fdroid.basic is ready.

    Rejects, in this order:
    - None or empty observation dictionary
    - Node list containing entries that are not objects
    - No package attributed to any node (e.g. a bare <hierarchy> wrapper, package=null)
    - No node from org.fdroid.basic (the foreign packages are named in the reason)
    - Fewer than 3 target nodes, or no target node with usable bounds
    - Target window present but its Search/EditText surface has no usable bounds, i.e.
      the surface is in the tree but not rendered (parse_bounds yields None for
      zero-height, zero-width, reversed or malformed bounds)
    Accepts only a rendered (bounds_valid) enabled EditText or Search semantic entry in
    the target package.

    Readiness is decision information only. It never relaxes the input contract: every
    click/set-text is still validated against the latest full observation.
    """
    if not isinstance(obs_data, dict):
        return False, "observation_not_dict"
    nodes = obs_data.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False, "empty_nodes"
    if not all(isinstance(n, dict) for n in nodes):
        return False, "malformed_nodes"

    observed_packages = sorted({n.get("package") for n in nodes if n.get("package")})
    fdroid_nodes = [n for n in nodes if n.get("package") == TARGET_PACKAGE]
    if not fdroid_nodes:
        if not observed_packages:
            return False, "no_package_in_observation"
        return False, f"foreign_package: {observed_packages}"

    if len(fdroid_nodes) < 3:
        return False, f"insufficient_fdroid_nodes: {len(fdroid_nodes)}"

    # A window whose every target node has unusable bounds is not laid out at all.
    if not any(n.get("bounds_valid") is True for n in fdroid_nodes):
        return False, "no_valid_bounds_in_package"

    surface = [
        n for n in fdroid_nodes
        if n.get("enabled") is True and (
            (n.get("class") or "").endswith("EditText")
            or (n.get("text") or "").casefold() == "search"
            or (n.get("content_desc") or "").casefold() == "search"
        )
    ]
    # The surface itself must be rendered. Other target nodes having valid bounds does
    # not prove that the field/label the caller would act on is on screen.
    if any(n.get("bounds_valid") is True for n in surface):
        return True, "fdroid_interactive_surface_ready"
    if surface:
        return False, "interactive_surface_not_rendered"

    return False, "fdroid_nodes_present_but_no_search_or_edit_surface"


class SingleCaseRunner:
    """Manages the lifecycle, execution, and verification of a single F-Droid ASCII input."""

    def __init__(
        self,
        project_root: Path = DEFAULT_PROJECT_ROOT,
        python_exe: Path = DEFAULT_PYTHON,
        adb_exe: Path = DEFAULT_ADB,
        manifest_path: Path = DEFAULT_MANIFEST,
        output_base: Path = DEFAULT_OUTPUT_BASE,
        dry_run: bool = False,
        mock_provider: Optional[Any] = None,
    ):
        self.project_root = Path(project_root).resolve()
        self.python_exe = Path(python_exe).resolve()
        self.adb_exe = Path(adb_exe).resolve()
        self.manifest_path = Path(manifest_path).resolve()
        self.output_base = Path(output_base).resolve()
        self.dry_run = dry_run
        self.mock_provider = mock_provider
        self.powershell_exe = resolve_powershell_exe()

        run_id = "live-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
        self.run_dir = self.output_base / run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)

        # In dry_run, isolate all runtime state/session writes inside a private mirror under run_dir
        self.runtime_base = (self.run_dir / "private-mirror") if self.dry_run else self.project_root
        self.state_file = self.runtime_base / "departments/workbuddy/runtime/round4/instances" / f"{run_id}-compat.json"
        self.session_dir = self.runtime_base / "departments/antigravity/runtime/round4" / f"{run_id}-compat"
        if self.dry_run:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.session_dir.mkdir(parents=True, exist_ok=True)
        self.launcher_script = self.project_root / "departments/workbuddy/round4-launcher/start-api35.ps1"
        self.boot_attempted = False
        self.unknown_input_state = False

        self.cli_base = [
            str(self.python_exe),
            str(self.project_root / "sandbox.py"),
            "--profile", "api35-google-x86_64",
            "--read-backend", "stable-direct",
            "--adb", str(self.adb_exe),
            "--state-file", str(self.state_file),
            "--output-dir", str(self.session_dir),
        ]

        self.start_time = time.monotonic()
        self.deadline = self.start_time + TOTAL_SESSION_TIMEOUT_S
        self.commands: List[Dict[str, Any]] = []

        self.verdict: Dict[str, Any] = {
            "runner": "SingleCaseRunner",
            "version": "1.1.0-fdroid-single-case-task84",
            "started_utc": utc_now(),
            "status": "running",
            "backend": "stable-direct",
            "profile": "api35-google-x86_64",
            "dry_run": self.dry_run,
            "target_package": TARGET_PACKAGE,
            "input_value": ASCII_QUERY,
            "state_file": str(self.state_file),
            "stopped": False,
            "launch_calls": 0,
            "launch_ok": None,
            "launch_error": None,
            "launch_recovery": None,
            "observe_readiness_calls": 0,
            "click_calls": 0,
            "set_text_calls": 0,
            "readback_observe_calls": 0,
            "ascii_verified": False,
            "read_contract_verified": False,
            "source_changed": False,
            "zero_real_adb_cim": True if self.dry_run else False,
            "unknown_input_state": False,
            "guarantees": {
                "click": "Full-tree live XML SHA-256 equality check before tap",
                "set_text": "Live target-node identity/bounds check, tap-to-focus check, clear check, and readback check (not full-tree hash equality)",
            },
        }
        save_json(self.run_dir / "verdict.json", self.verdict)

    def remaining_s(self, reserve_cleanup: bool = True) -> float:
        rem = self.deadline - time.monotonic()
        if reserve_cleanup:
            rem -= CLEANUP_BUDGET_S
        return max(0.0, rem)

    def log(self, msg: str) -> None:
        print(f"[{utc_now()}] {msg}", flush=True)

    def record_command(
        self,
        label: str,
        argv: List[str],
        timeout_s: float,
        exit_code: Optional[int],
        stdout_text: str,
        stderr_text: str,
        elapsed_s: float,
        timed_out: bool = False,
        stdout_bytes: Optional[bytes] = None,
        stderr_bytes: Optional[bytes] = None,
    ) -> Dict[str, Any]:
        idx = len(self.commands)
        so_file = self.run_dir / f"{idx:03d}-{label}.stdout"
        se_file = self.run_dir / f"{idx:03d}-{label}.stderr"
        if stdout_bytes is not None:
            so_file.write_bytes(stdout_bytes)
        else:
            so_file.write_text(stdout_text, encoding="utf-8", errors="replace")
        if stderr_bytes is not None:
            se_file.write_bytes(stderr_bytes)
        else:
            se_file.write_text(stderr_text, encoding="utf-8", errors="replace")

        row = {
            "index": idx,
            "ended_utc": utc_now(),
            "started_utc": datetime.fromtimestamp(time.time() - elapsed_s, timezone.utc).isoformat(),
            "cwd": str(self.project_root),
            "label": label,
            "argv": [str(a) for a in argv],
            "timeout_s": round(timeout_s, 3),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "elapsed_s": round(elapsed_s, 3),
            "stdout": str(so_file),
            "stderr": str(se_file),
        }
        self.commands.append(row)
        save_json(self.run_dir / "commands.json", self.commands)
        return row

    def execute_cmd(
        self,
        label: str,
        argv: List[str],
        timeout: float = CMD_TIMEOUT_S,
        check: bool = True,
        cleanup: bool = False,
    ) -> str:
        if cleanup:
            rem = self.cleanup_deadline - time.monotonic()
            if rem <= 0:
                raise RunnerError("cleanup_timeout", "Shared cleanup budget exhausted")
            timeout = min(timeout, rem)
        if not cleanup:
            rem = self.remaining_s(reserve_cleanup=True)
            if rem <= 0:
                raise RunnerError("session_timeout", f"Total operational budget exhausted before {label}")
            timeout = min(timeout, rem)

        self.log(f"EXEC {label}: {' '.join(map(str, argv))[:120]}")
        t0 = time.monotonic()

        if self.dry_run:
            if self.mock_provider is not None:
                exit_code, stdout, stderr = self.mock_provider.handle_command(label, argv, timeout)
            else:
                exit_code, stdout, stderr = 0, '{"ok": true}', ""
            elapsed = time.monotonic() - t0
            row = self.record_command(label, argv, timeout, exit_code, stdout, stderr, elapsed, False)
            if check and exit_code != 0:
                raise RunnerError("command_failed", f"Mock command {label} failed with exit code {exit_code}", row)
            return stdout

        # File handles avoid waiting for EOF on pipes inherited by launcher descendants.
        raw_out = self.run_dir / (f"{len(self.commands):03d}-" + label + ".raw.stdout")
        raw_err = self.run_dir / (f"{len(self.commands):03d}-" + label + ".raw.stderr")
        try:
            with raw_out.open("xb") as so, raw_err.open("xb") as se:
                cp = subprocess.run(
                    argv,
                    cwd=str(self.project_root),
                    stdin=subprocess.DEVNULL,
                    stdout=so,
                    stderr=se,
                    timeout=timeout,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                    env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"},
                )
            out_bytes = raw_out.read_bytes()
            err_bytes = raw_err.read_bytes()
            stdout_str = out_bytes.decode("utf-8-sig", errors="replace")
            stderr_str = err_bytes.decode("utf-8-sig", errors="replace")
            row = self.record_command(
                label, argv, timeout, cp.returncode, stdout_str, stderr_str, time.monotonic() - t0, False, out_bytes, err_bytes
            )
        except subprocess.TimeoutExpired:
            out_bytes = raw_out.read_bytes() if raw_out.is_file() else b""
            err_bytes = raw_err.read_bytes() if raw_err.is_file() else b""
            stdout_str = out_bytes.decode("utf-8-sig", errors="replace")
            stderr_str = err_bytes.decode("utf-8-sig", errors="replace")
            row = self.record_command(
                label, argv, timeout, None, stdout_str, stderr_str, time.monotonic() - t0, True, out_bytes, err_bytes
            )
            row.update(raw_stdout=str(raw_out), raw_stderr=str(raw_err))
            save_json(self.run_dir / "commands.json", self.commands)
            raise RunnerError("driver_timeout", f"Command {label} timed out after {timeout:.1f}s", row)
        row.update(raw_stdout=str(raw_out), raw_stderr=str(raw_err))
        save_json(self.run_dir / "commands.json", self.commands)
        if check and row["exit_code"] != 0:
            raise RunnerError("command_failed", f"Command {label} exited {row['exit_code']}", row)
        return stdout_str

    def verify_manifest(self, stage_label: str) -> None:
        """Verifies all 109 accepted source manifest files."""
        self.log(f"Checking 109 manifest at stage: {stage_label}")
        if not self.manifest_path.is_file():
            raise RunnerError("manifest_missing", f"Manifest not found at {self.manifest_path}")

        m = load_json(self.manifest_path)
        expected_files: Dict[str, str] = m.get("files", {})
        if len(expected_files) != 109:
            raise RunnerError("manifest_invalid", f"Expected exactly 109 files, found {len(expected_files)}")

        differences = []
        for rel_path, expected_sha in expected_files.items():
            target_p = self.project_root / rel_path
            if not target_p.is_file():
                differences.append(f"missing: {rel_path}")
                continue
            actual_sha = sha256_file(target_p)
            if actual_sha != expected_sha:
                differences.append(f"hash_mismatch: {rel_path} (expected {expected_sha}, got {actual_sha})")

        report = {
            "stage": stage_label,
            "file_count": len(expected_files),
            "differences": differences,
            "manifest_sha256": sha256_file(self.manifest_path),
            "verified": len(differences) == 0,
        }
        save_json(self.run_dir / f"{stage_label}.json", report)
        if differences:
            self.verdict["source_changed"] = True
            raise RunnerError("source_drift", f"Source files drifted at {stage_label}: {differences[:5]}", report)

    def check_preflight_absent(self, label: str, settle: bool = False) -> None:
        """Verifies absence of devices and emulator processes."""
        max_tries = 7 if settle else 1
        for i in range(max_tries):
            dev_out = self.execute_cmd(
                f"{label}-devices-{i}",
                [str(self.adb_exe), "devices"],
                timeout=CMD_TIMEOUT_S,
                cleanup=settle,
            )
            proc_out = self.execute_cmd(
                f"{label}-processes-{i}",
                [
                    self.powershell_exe, "-NoProfile", "-Command",
                    'ConvertTo-Json -Depth 3 -InputObject @(Get-CimInstance Win32_Process -Filter "Name=\'emulator.exe\' OR Name=\'qemu-system-x86_64.exe\' OR Name=\'qemu-system-x86_64-headless.exe\'" | Select-Object ProcessId,ParentProcessId,Name,CreationDate,ExecutablePath,CommandLine)'
                ],
                timeout=CMD_TIMEOUT_S,
                cleanup=settle,
            )
            # Parse processes
            procs = ["unparsed process inventory"]
            if proc_out.strip():
                try:
                    parsed = json.loads(proc_out)
                    procs = parsed if isinstance(parsed, list) else ["invalid process-list schema"]
                except Exception:
                    procs = [proc_out]

            dev_lines = [line.strip() for line in dev_out.splitlines()[1:] if line.strip()]
            if not dev_lines and not procs:
                self.log(f"Device/Process preflight clean ({label}, try {i})")
                return
            if settle and i < max_tries - 1:
                if time.monotonic() + 5 >= self.cleanup_deadline:
                    raise RunnerError("cleanup_timeout", "No cleanup budget for another sweep")
                time.sleep(5)
            else:
                raise RunnerError(
                    "environment_unclean",
                    f"Foreign device or emulator processes present during {label}; no foreign termination permitted",
                    {"devices": dev_lines, "processes": procs},
                )

    def call_cli(self, label: str, action: str, *args: str, check: bool = True) -> Dict[str, Any]:
        """Calls sandbox.py with stable-direct backend."""
        is_input = action in ("click", "set-text")
        if is_input and self.unknown_input_state:
            raise RunnerError("unknown_input_no_replay", f"Refusing {action} ({label}): previous input left unknown state")

        argv = list(self.cli_base) + [action] + list(args)
        # CLI actions contain multiple independent bounded reads; never kill set-text at 20s.
        cap = SET_TEXT_BUDGET_S if action == "set-text" else (CLICK_BUDGET_S if action == "click" else LAUNCH_CLI_CAP_S)
        rem_op = self.remaining_s(reserve_cleanup=True)
        if action == "set-text" and rem_op < SET_TEXT_MIN_REMAINING_S:
            raise RunnerError("session_timeout", "Insufficient complete input+readback budget; no input started")
        if action == "click" and rem_op < CLICK_BUDGET_S:
            raise RunnerError("session_timeout", "Insufficient budget for click; no input started")
        if action not in ("click", "set-text") and rem_op < OBSERVE_BUDGET_S:
            raise RunnerError("session_timeout", f"Insufficient operational budget (<{OBSERVE_BUDGET_S}s) before {label}")
        if label.startswith("fdroid-observe-readiness-"):
            cap = min(cap, self.readiness_deadline - time.monotonic())
            if cap < OBSERVE_BUDGET_S:
                raise ReadinessError("readiness_timeout", "Insufficient remaining shared readiness budget")
        try:
            out = self.execute_cmd(label, argv, timeout=cap, check=False)
        except Exception:
            if is_input:
                self.unknown_input_state = True
                self.verdict["unknown_input_state"] = True
            raise
        try:
            data = json.loads(out)
        except Exception as exc:
            if is_input:
                self.unknown_input_state = True
                self.verdict["unknown_input_state"] = True
            raise RunnerError("invalid_cli_json", f"CLI output for {label} was not valid JSON: {exc}", {"raw": out[:500]})

        save_json(self.run_dir / f"{label}.json", data)
        cmd_row = self.commands[-1] if self.commands else {}
        if data.get("ok") is True and cmd_row.get("exit_code") != 0:
            if is_input:
                self.unknown_input_state = True
                self.verdict["unknown_input_state"] = True
            raise RunnerError(
                "invalid_cli_response",
                f"CLI {action} ({label}) reported ok=true with non-zero exit code {cmd_row.get('exit_code')}",
                data,
            )
        if data.get("ok") is False and cmd_row.get("exit_code") == 0:
            if is_input:
                self.unknown_input_state = True
                self.verdict["unknown_input_state"] = True
            raise RunnerError(
                "invalid_cli_response",
                f"CLI {action} ({label}) reported ok=false with zero exit code",
                data,
            )
        if check and not data.get("ok"):
            if is_input:
                self.unknown_input_state = True
                self.verdict["unknown_input_state"] = True
            err = data.get("error") or {}
            raise RunnerError(err.get("code", "cli_failed"), f"CLI {action} failed: {err.get('message')}", data)
        return data

    def observe(self, label: str) -> Dict[str, Any]:
        res = self.call_cli(label, "observe")
        return res["data"]






