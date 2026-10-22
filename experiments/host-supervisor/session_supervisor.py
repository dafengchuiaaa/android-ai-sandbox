"""Task 97 Gemini Session Supervisor and Lifecycle Glue.

Manages the bounded device lifecycle, budgets, and rollback while hosting
turn-by-turn external AI decisions for application_refresh:
- Preflight device and directory claim checks
- Single-invocation lifecycle management with pre-staged initial launch support
- Turnaround compact briefing generation after each step submit
- Exact budget checking (click requires >= 70.0s operational budget)
- Robust logging and exception preservation (UTF-8 file write; console output)
- Windows 64-bit process liveness probing via ctypes with tri-state ('alive', 'dead', 'unknown')
- Safe ADB inspection distinguishing check failure from clean empty state
- Strict recovery of worker PID from session claims
- Strict UTF-8 decoding for Python machine JSON
- Fixed budget propagation: TOTAL_S=360, CLEANUP_S=40, operational_budget=320s
- Per-action client submit timeout:
    * launch: 25s
    * observe: 20s
    * click: 50s
    * finish: 20s
    (bounded strictly by remaining operational time; no client retry)
- Authoritative final result resolution:
    * Distinguishes authoritative results (result.json / stepwise-verdict.json)
      from unfinalized initialization stubs (verdict.json).
    * Re-probes authoritative final result after worker process exit in finally block
      to eliminate false 'no_result_json' and 'cli_calls=0'.
    * Accurately tracks and logs network evidence extraction without false claims.
- Turn-by-turn AI decision dispatch via decisions/ directory with step briefing integration
- Fault-isolated finally block ensuring verified Stop, post-session verification,
  after-hash verification, transfer.py rollback, and SESSION-SUMMARY.json preservation.
"""
import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import hashlib
import json
import locale
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import traceback
from typing import Any, Dict, List, Optional, Tuple

# ----------------- Win32 API Definitions for 64-bit Windows -----------------
kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.WaitForSingleObject.restype = wintypes.DWORD

SYNCHRONIZE = 0x00100000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WAIT_TIMEOUT = 258
ERROR_ACCESS_DENIED = 5
ERROR_INVALID_PARAMETER = 87

# ----------------- Default Paths & Budgets -----------------
WORKSPACE = Path(r"C:\Users\LOCAL_USER\Documents\ChatGPT\手机沙箱").resolve()
PROD_ROOT = Path(r"C:\Users\LOCAL_USER\Documents\Codex\2026-10-14\new-chat-2").resolve()
PYTHON_EXE = Path(r"C:\Users\LOCAL_USER\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe").resolve()
ADB_EXE = Path(r"C:\Users\LOCAL_USER\AppData\Local\Android\Sdk\platform-tools\adb.exe").resolve()
CLI_SCRIPT = WORKSPACE / "opensource/android-ai-sandbox/tools/stepwise_session.py"

TASK_DIR = Path(__file__).resolve().parent
TASK96_DIR = TASK_DIR
MANIFEST = TASK_DIR / "session-source-manifest.json"
INSTALL_PLAN = TASK_DIR / "install-plan.json"
TRANSFER = TASK_DIR / "transfer.py"

OUT_DIR = TASK_DIR / "session"
LOG_FILE = TASK_DIR / "controller.log"
SUMMARY_FILE = TASK_DIR / "SESSION-SUMMARY.json"
DECISIONS_DIR = TASK_DIR / "decisions"
STEP_RESULTS_DIR = TASK_DIR / "step_results"

TOTAL_S = 360
CLEANUP_S = 40
BOOT_TIMEOUT_S = 140
SINGLE_CMD_TIMEOUT_S = 8.0

TARGET_PACKAGE = "org.fdroid.basic"

ACTION_BUDGET_CAPS = {
    "launch": 25.0,
    "observe": 20.0,
    "click": 50.0,
    "set-text": 90.0,
    "finish": 20.0,
}


def _get_stepwise_session_exports():
    tools_dir = WORKSPACE / "opensource/android-ai-sandbox/tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    from stepwise_session import is_permitted_refresh_click_target, observation
    return is_permitted_refresh_click_target, observation


def _get_permitted_target_fn():
    target_fn, _ = _get_stepwise_session_exports()
    return target_fn


def _get_observation_fn():
    _, obs_fn = _get_stepwise_session_exports()
    return obs_fn


def _get_content_adapter():
    for candidate_root in [
        PROD_ROOT,
        WORKSPACE,
        Path(__file__).resolve().parent.parent.parent.parent,
    ]:
        ca_file = candidate_root / "departments/deepseek/content_adapter.py"
        if ca_file.is_file():
            if str(candidate_root) not in sys.path:
                sys.path.insert(0, str(candidate_root))
            try:
                from departments.deepseek import content_adapter as ca
                return ca
            except ImportError:
                pass
    raise ImportError("Cannot import departments.deepseek.content_adapter")



class WorkerChain:
    def __init__(self, tag: str, tid: int, start_time: str, line_no: int):
        self.tag = tag
        self.tid = tid
        self.start_time = start_time
        self.start_line_no = line_no
        self.entry_jar_requested = False
        self.downloader_progress = False
        self.positive_bytes_transferred = False
        self.bytes_transferred = 0
        self.repo_update_result = None
        self.finished_do_work = False
        self.finish_time = None
        self.finish_line_no = None
        self.wm_success = False
        self.wm_work_id = None
        self.wm_success_time = None
        self.wm_success_line_no = None
        self.matched_lines: list[str] = []

    def is_complete(self) -> bool:
        return (
            self.entry_jar_requested is True and
            self.downloader_progress is True and
            self.positive_bytes_transferred is True and
            self.bytes_transferred > 0 and
            self.repo_update_result in ("IndexUpdateResult$Unchanged", "IndexUpdateResult$Updated") and
            self.finished_do_work is True and
            self.wm_success is True
        )


def parse_log_time_to_ms(t_str: str) -> tuple[int, bool]:
    """Parse MM-DD HH:MM:SS.mmm into integer milliseconds and has_ms flag."""
    m = re.match(r"^(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,3}))?", (t_str or "").strip())
    if not m:
        return 0, False
    month, day, h, minute, s, frac = m.groups()
    has_ms = frac is not None
    ms = int(frac.ljust(3, "0")[:3]) if has_ms else 0
    total_ms = (int(day) * 86400 + int(h) * 3600 + int(minute) * 60 + int(s)) * 1000 + ms
    return total_ms, has_ms


# ----------------- Robust Logging -----------------
def log_message(msg: str, log_file: Optional[Path] = None) -> None:
    """Log to file first in UTF-8, then output to console safely."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())
    line = f"[{ts}] {msg}"

    target_log = log_file or LOG_FILE
    if target_log is not None:
        try:
            target_log.parent.mkdir(parents=True, exist_ok=True)
            with target_log.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        try:
            enc = sys.stdout.encoding or locale.getpreferredencoding() or "ascii"
            safe_text = line.encode(enc, errors="backslashreplace").decode(enc)
            sys.stdout.write(safe_text + "\n")
            sys.stdout.flush()
        except Exception:
            pass


def load_json_safe(path: Path) -> Optional[Any]:
    try:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        pass
    return None


def probe_win32_pid(pid: int) -> Tuple[str, Optional[int]]:
    if pid <= 0:
        return "dead", None
    h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not h_proc:
        err = ctypes.GetLastError()
        if err in (ERROR_INVALID_PARAMETER, 0):
            return "dead", None
        if err == ERROR_ACCESS_DENIED:
            return "alive", None
        return "unknown", None

    try:
        exit_code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(h_proc, ctypes.byref(exit_code)):
            if exit_code.value == 259:  # STILL_ACTIVE
                return "alive", None
            else:
                return "dead", int(exit_code.value)
        return "unknown", None
    finally:
        kernel32.CloseHandle(h_proc)


def check_adb_devices(adb_exe: Path, timeout: float = 8.0) -> Tuple[str, List[str], str]:
    if not adb_exe.is_file():
        return "error", [], f"adb_not_found: {adb_exe}"
    try:
        proc = subprocess.run(
            [str(adb_exe), "devices"],
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        out_text = proc.stdout.decode("utf-8", errors="replace")
        err_text = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            return "error", [], f"adb_devices_failed_exit_{proc.returncode}: {err_text.strip()}"

        lines = [line.strip() for line in out_text.splitlines() if line.strip()]
        devs = []
        for line in lines[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devs.append(parts[0])

        if not devs:
            return "clean", [], ""
        return "devices_present", devs, f"active_devices: {devs}"
    except subprocess.TimeoutExpired:
        return "timeout", [], f"adb_devices_timed_out_{timeout}s"
    except Exception as e:
        return "error", [], f"adb_devices_exception: {e}"


def verify_candidate_after_state(prod_root: Path, install_plan_file: Path) -> Tuple[bool, str]:
    if not install_plan_file.is_file():
        return False, "install_plan_file_missing"
    plan = load_json_safe(install_plan_file)
    if not plan or "changes" not in plan:
        return False, "install_plan_invalid"
    for change in plan["changes"]:
        rel = change["path"]
        expected_after = change["after"]
        target = prod_root / rel
        if expected_after is None:
            if target.is_file():
                return False, f"file_should_be_absent_in_after_state: {rel}"
        else:
            if not target.is_file():
                return False, f"file_missing_in_after_state: {rel}"
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            if actual != expected_after:
                return False, f"hash_mismatch_in_after_state: {rel} (expected {expected_after}, got {actual})"
    return True, "after_state_strictly_matches"



# ----------------- Supervisor Class -----------------
class Task101Supervisor:
    def __init__(self,
                 workspace: Path = WORKSPACE,
                 prod_root: Path = PROD_ROOT,
                 python_exe: Path = PYTHON_EXE,
                 adb_exe: Path = ADB_EXE,
                 cli_script: Path = CLI_SCRIPT,
                 task_dir: Path = TASK_DIR,
                 total_s: float = TOTAL_S,
                 cleanup_s: float = CLEANUP_S,
                 enable_startup_logcat_diagnostic: bool = False,
                 mode: str = "application_refresh",
                 low_ram: bool = False):
        if mode not in ("application_refresh", "stepwise_input"):
            raise ValueError(f"unsupported mode: {mode}")
        if type(low_ram) is not bool:
            raise ValueError("low_ram must be a boolean")
        self.mode = mode
        self.low_ram = low_ram
        self.workspace = workspace
        self.prod_root = prod_root
        self.python_exe = python_exe
        self.adb_exe = adb_exe
        self.cli_script = cli_script
        self.task_dir = task_dir
        self.total_s = total_s
        self.cleanup_s = cleanup_s
        self.operational_budget = total_s - cleanup_s
        self.enable_startup_logcat_diagnostic = enable_startup_logcat_diagnostic

        self.manifest = self.task_dir / "session-source-manifest.json"
        self.install_plan = self.task_dir / "install-plan.json"
        self.transfer = self.task_dir / "transfer.py"

        self.out_dir = self.task_dir / "session"
        self.log_file = self.task_dir / "controller.log"
        self.summary_file = self.task_dir / "SESSION-SUMMARY.json"
        self.decisions_dir = self.task_dir / "decisions"
        self.step_results_dir = self.task_dir / "step_results"

        self.start_time = 0.0
        self.operational_deadline = 0.0
        self.overall_deadline = 0.0

        self.worker_proc: Optional[subprocess.Popen] = None
        self.worker_pid: Optional[int] = None
        self.worker_run_dir: Optional[Path] = None
        self.emulator_pid: Optional[int] = None
        self.emulator_serial: Optional[str] = None
        self.timestamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        self.session_id = f"task101-session-{self.timestamp}"
        self.refresh_click_device_time: Optional[str] = None
        self.refresh_action_seq: Optional[int] = None
        self.refresh_dispatched: bool = False
        self.seen_in_progress: bool = False
        self.ui_verified_completed: bool = False
        self.total_observes_used: int = 0
        self.refresh_observes_used: int = 0
        self.session_start_epoch: float = 0.0
        self.operational_deadline_epoch: float = 0.0
        self.network_extraction_performed: bool = False
        self.finish_submit_started: bool = False
        self.submit_pending: bool = False
        self.set_text_monotonic_envelope: Optional[Tuple[float, float]] = None

        self.summary: Dict[str, Any] = {
            "task": "task101-gemini-supervisor-defense",
            "mode": self.mode,
            "enable_startup_logcat_diagnostic": self.enable_startup_logcat_diagnostic,
            "low_ram": self.low_ram,
            "environment": "Gemini 3.8 Flash / Antigravity harness",
            "started_utc": "",
            "ended_utc": "",
            "elapsed_s": 0.0,
            "status": "pending",
            "control_status": "pending",
            "observation_status": "unverified",
            "network_status": "unverified",
            "cleanup_status": "unverified",
            "rollback_status": "pending",
            "worker_pid": None,
            "emulator_pid": None,
            "emulator_serial": None,
            "cli_calls": 0,
            "goal_verified": False,
            "refresh_triggered": False,
            "refresh_ui_completed": False,
            "text_backend_verified": False,
            "independent_readback": False,
            "network_verified": False,
            "step_records": [],
            "cleanup_errors": [],
            "fatal_error": None,
            "transfer": None,
            "rollback": None,
            "result": None,
            "post_rollback_manifest": None,
        }

    def _validate_verdict_schema(self, res: Any, source_path: Path) -> Tuple[bool, str]:
        """Validate authoritative final verdict schema.

        Requires:
        - dict
        - mode == self.mode (e.g. "application_refresh")
        - decision_mode == "external_stepwise"
        - status in ("completed", "incomplete", "failed")
        - stopped is bool (cannot be omitted)
        - cli_calls is int >= 0 (and not bool)
        - goal_verified is bool
        - refresh_triggered is bool
        """
        if not isinstance(res, dict):
            return False, "not_a_dict"

        if res.get("mode") != self.mode:
            return False, f"mode_mismatch: expected '{self.mode}', got '{res.get('mode')}'"

        if res.get("decision_mode") != "external_stepwise":
            return False, f"decision_mode_mismatch: expected 'external_stepwise', got '{res.get('decision_mode')}'"

        st = res.get("status")
        if st not in ("completed", "incomplete", "failed"):
            return False, f"invalid_or_non_terminal_status: '{st}'"

        if "stopped" not in res or not isinstance(res["stopped"], bool):
            return False, "missing_or_non_bool_stopped"

        cli_calls = res.get("cli_calls")
        if type(cli_calls) is not int or cli_calls < 0:
            return False, f"missing_or_invalid_cli_calls: {cli_calls}"

        if "goal_verified" not in res or not isinstance(res["goal_verified"], bool):
            return False, "missing_or_non_bool_goal_verified"

        if "refresh_triggered" not in res or not isinstance(res["refresh_triggered"], bool):
            return False, "missing_or_non_bool_refresh_triggered"

        return True, "valid"

    def save_turnaround_context(self, seq: int, response_sha256: Optional[str] = None) -> Dict[str, Any]:
        """Save authoritative turnaround context sidecar (turnaround-context.json) into session directory.

        Binds session_id, clock_domain ("windows_monotonic"), seq, and response_sha256
        to ensure independent CLI and embedded inspection operate on the exact same authoritative state.
        Monotonic deadlines strictly prevent wall-clock manipulation from altering budget.
        """
        now_mono = time.monotonic()
        now_epoch = time.time()
        sc = {
            "contract_version": "task97-turnaround-context-v2",
            "clock_domain": "windows_monotonic",
            "session_id": self.session_id,
            "mode": self.mode,
            "seq": seq,
            "response_sha256": response_sha256,
            "refresh_dispatched": self.refresh_dispatched,
            "seen_in_progress": self.seen_in_progress,
            "ui_verified_completed": self.ui_verified_completed,
            "total_observes_used": self.total_observes_used,
            "refresh_observes_used": self.refresh_observes_used,
            "max_total_observes": 3,
            "max_refresh_observes": 2,
            "session_start_monotonic": self.start_time,
            "operational_deadline_monotonic": self.operational_deadline,
            "last_updated_monotonic": now_mono,
            "session_start_epoch": self.session_start_epoch,
            "operational_deadline_epoch": self.operational_deadline_epoch,
            "last_updated_epoch": now_epoch,
            "last_updated_utc": datetime.now(timezone.utc).isoformat(),
        }
        self.out_dir.mkdir(parents=True, exist_ok=True)
        sc_file = self.out_dir / "turnaround-context.json"
        sc_file.write_text(json.dumps(sc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return sc

    def load_authoritative_result(self) -> Optional[Dict[str, Any]]:
        """Load the authoritative final result from session result.json or stepwise-verdict.json.

        Strictly limits candidates to the current session out_dir and worker_run_dir.
        Enforces strict schema validation. Detects and reports conflicts without favoring success.
        """
        res_cand = None
        sv_cand = None

        # 1. Check out_dir / result.json
        res_path = self.out_dir / "result.json"
        if res_path.is_file():
            loaded = load_json_safe(res_path)
            valid, reason = self._validate_verdict_schema(loaded, res_path)
            if valid:
                res_cand = loaded
            else:
                self.log(f"Candidate {res_path} rejected: {reason}")

        # 2. Check worker_run_dir / stepwise-verdict.json
        if self.worker_run_dir:
            sv_path = Path(self.worker_run_dir) / "stepwise-verdict.json"
            if sv_path.is_file():
                loaded = load_json_safe(sv_path)
                valid, reason = self._validate_verdict_schema(loaded, sv_path)
                if valid:
                    sv_cand = loaded
                else:
                    self.log(f"Candidate {sv_path} rejected: {reason}")

        # 3. Check for conflict if both exist and are valid
        if res_cand and sv_cand:
            conflict_fields = []
            for k in ("status", "goal_verified", "refresh_ui_completed", "network_verified", "stopped", "cli_calls", "refresh_triggered"):
                if res_cand.get(k) != sv_cand.get(k):
                    conflict_fields.append((k, res_cand.get(k), sv_cand.get(k)))
            if conflict_fields:
                err_msg = f"CONFLICT between result.json and stepwise-verdict.json on fields: {conflict_fields}"
                self.log(f"CRITICAL: {err_msg}")
                self.summary["result_conflict"] = {
                    "fields": conflict_fields,
                    "result_json": res_cand,
                    "stepwise_verdict": sv_cand
                }
                self.summary["control_status"] = "result_conflict"
                self.summary.setdefault("cleanup_errors", []).append(f"dual_verdict_conflict: {conflict_fields}")
                return None
            return res_cand
        elif res_cand:
            return res_cand
        elif sv_cand:
            return sv_cand

        return None

    def _apply_authoritative_result(self, res: Dict[str, Any]) -> None:
        """Apply authoritative result fields to supervisor summary."""
        self.summary["result"] = res
        self.summary["control_status"] = "failed" if self.summary.get("fatal_error") else res["status"]
        self.summary["goal_verified"] = res["goal_verified"]
        self.summary["refresh_triggered"] = res["refresh_triggered"]
        self.summary["refresh_in_progress"] = res.get("refresh_in_progress", False)
        self.summary["refresh_ui_completed"] = res.get("refresh_ui_completed", False)
        self.summary["cli_calls"] = res["cli_calls"]
        self.summary["worker_stopped"] = res["stopped"]
        if "text_backend_verified" in res:
            self.summary["text_backend_verified"] = bool(res["text_backend_verified"])
        if "independent_readback" in res:
            self.summary["independent_readback"] = bool(res["independent_readback"])
        if res.get("error"):
            self.summary["error"] = res["error"]
        if res.get("error_code"):
            self.summary["error_code"] = res["error_code"]

        if self.mode == "application_refresh":
            # Reconcile network_status
            if res.get("network_verified") is not None:
                if self.network_extraction_performed:
                    if res["network_verified"] != self.summary.get("network_verified"):
                        self.log(f"CRITICAL: verdict network_verified ({res['network_verified']}) != supervisor extraction ({self.summary.get('network_verified')})")
                        self.summary["network_verified"] = False
                        self.summary["network_status"] = "conflict_worker_unverified" if not res["network_verified"] else "conflict_supervisor_unverified"
                        self.summary.setdefault("cleanup_errors", []).append(
                            f"network_verification_contradiction: supervisor={self.summary.get('network_verified')}, worker={res['network_verified']}"
                        )
                    else:
                        self.summary["network_verified"] = bool(res["network_verified"])
                        self.summary["network_status"] = "verified" if res["network_verified"] else "unverified"
                else:
                    self.summary["network_verified"] = bool(res["network_verified"])
                    if self.summary.get("network_status") in ("unverified", "pending", ""):
                        self.summary["network_status"] = "verified" if res["network_verified"] else "unverified"
            if not self.summary.get("refresh_triggered", False) and not self.network_extraction_performed:
                self.summary["network_status"] = "skipped_refresh_not_triggered"
        else:
            self.summary["network_status"] = "skipped_mode_not_refresh"

    def run_step6_network_evidence_verification(self) -> None:
        """Step 6: Verify logcat network evidence.

        Prioritizes actual extraction record (network_extraction_performed).
        Never captures logcat if worker is already stopping or dead.
        Skipped entirely when mode is not application_refresh.
        """
        if self.mode != "application_refresh":
            self.log(f"Step 6: Mode is {self.mode}; skipping logcat network evidence verification.")
            if self.summary.get("network_status") in ("unverified", "pending", ""):
                self.summary["network_status"] = "skipped_mode_not_refresh"
            return
        # Check if worker generated a startup-logcat-diagnostic.json on failure path
        diag_path = self.out_dir / "startup-logcat-diagnostic.json"
        if diag_path.is_file():
            diag_data = load_json_safe(diag_path)
            if diag_data:
                self.summary["startup_diagnostic"] = diag_data
                self.log(f"Step 6: Attached worker startup Logcat diagnostic (status={diag_data.get('status')}).")

        # 1. Prioritize actual extraction record if already performed during session finish
        if self.network_extraction_performed:
            self.log(f"Step 6: Logcat network evidence was already extracted during session finish (status={self.summary.get('network_status')}).")
            return

        # 2. If not yet extracted, check if refresh was dispatched or triggered
        if not (self.refresh_dispatched or self.summary.get("refresh_triggered", False)):
            self.log("Step 6: Refresh was not dispatched/triggered during session; skipping logcat network evidence extraction.")
            self.summary["network_status"] = "skipped_refresh_not_triggered"
            return

        # 3. Refresh was dispatched, but finish extraction was not done (e.g. error/crash before finish)
        # Check if worker is already stopping, dead, or state unknown
        worker_st, _ = probe_win32_pid(self.worker_pid) if self.worker_pid else ("dead", 1)
        cur_phase = ""
        session_file = self.out_dir / "current-session.json"
        if session_file.is_file():
            sess = load_json_safe(session_file)
            if sess and isinstance(sess, dict):
                cur_phase = sess.get("phase", "")

        if worker_st == "dead" or cur_phase in ("stopping", "stopped", "completed", "failed") or not cur_phase:
            self.log(f"Step 6: Worker is already stopping, terminated, or state unknown (phase={cur_phase}, state={worker_st}); cannot capture logcat evidence after worker shutdown.")
            self.summary["network_status"] = "skipped_worker_stopping_or_dead"
            self.summary["network_verified"] = False
            return

        self.log("Step 6: Extracting logcat network evidence after loop...")
        net_evidence = self.extract_logcat_network_evidence()
        self.summary["network_verified"] = net_evidence.get("network_verified", False)
        self.summary["network_check"] = net_evidence
        self.summary["network_status"] = "verified" if self.summary["network_verified"] else "unverified"

    def wait_for_worker_initial_result(self, timeout_s: float = 15.0, poll_interval_s: float = 0.3) -> Optional[Dict[str, Any]]:
        """Step 7: Await worker final result within initial timeout."""
        self.log(f"Step 7: Awaiting worker final result (timeout={timeout_s}s)...")
        wait_deadline = time.monotonic() + timeout_s
        while time.monotonic() < wait_deadline:
            res = self.load_authoritative_result()
            if res:
                self._apply_authoritative_result(res)
                self.log(f"Step 7: Authoritative result acquired during initial wait: status={self.summary.get('control_status')}, cli_calls={self.summary.get('cli_calls')}")
                return res
            if self.summary.get("control_status") == "result_conflict":
                return None
            time.sleep(poll_interval_s)

        if not self.summary.get("result"):
            self.log(f"Step 7: Authoritative result not yet found within initial {timeout_s}s wait (worker may still be executing Stop/cleanup); will re-probe after worker termination.")
        return None

    def handle_worker_exit_and_reload(self, wait_worker_exit_s: float = 15.0) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """Wait for worker process termination and reload authoritative result.

        Returns (worker_alive, authoritative_result).
        """
        worker_alive = True
        if self.worker_pid:
            self.log(f"Probing worker process (PID={self.worker_pid})...")
            w_wait_deadline = time.monotonic() + wait_worker_exit_s
            while time.monotonic() < w_wait_deadline:
                st, code = probe_win32_pid(self.worker_pid)
                if st == "dead":
                    self.log(f"Worker process confirmed terminated (exit={code}).")
                    worker_alive = False
                    break
                time.sleep(0.5)
            if worker_alive:
                self.log(f"WARNING: Worker PID {self.worker_pid} did not exit within budget.")
        else:
            worker_alive = False

        # Re-probe authoritative final result after worker process exit
        auth_res = self.load_authoritative_result()
        if auth_res:
            self._apply_authoritative_result(auth_res)
            self.log(f"Authoritative result resolved after worker exit: status={self.summary.get('control_status')}, cli_calls={self.summary.get('cli_calls')}, error_code={self.summary.get('error_code')}")
        elif self.summary.get("control_status") == "result_conflict":
            self.log("Result conflict previously detected; maintaining result_conflict status.")
        elif not self.summary.get("result"):
            self.log("Warning: Authoritative result not found even after worker termination.")
            self.summary["control_status"] = "no_result_json"

        return worker_alive, auth_res


    def compute_final_verdict(self) -> str:
        """Compute final session status ('success' or 'failed')."""
        if self.mode == "application_refresh":
            mode_criteria = (
                self.summary.get("refresh_ui_completed") is True and
                self.summary.get("network_verified") is True and
                self.summary.get("network_status") == "verified"
            )
        elif self.mode == "stepwise_input":
            res = self.summary.get("result") or {}
            mode_criteria = (
                res.get("text_backend_verified") is True and
                res.get("independent_readback") is True and
                self.summary.get("text_backend_verified") is True and
                self.summary.get("independent_readback") is True
            )
        else:
            mode_criteria = False

        common_criteria = (
            self.summary.get("control_status") == "completed" and
            self.summary.get("goal_verified") is True and
            self.summary.get("result") is not None and
            self.summary.get("cleanup_status") == "clean" and
            self.summary.get("rollback_status") in ("rolled_back", "not_needed") and
            not self.summary.get("cleanup_errors") and
            not self.summary.get("fatal_error")
        )
        all_clean = common_criteria and mode_criteria
        status = "success" if all_clean else "failed"
        self.summary["status"] = status
        return status

    def finalize_post_loop_aggregation(self, wait_result_s: float = 15.0, wait_worker_exit_s: float = 15.0) -> None:
        """Execute full post-decision-loop aggregation lifecycle."""
        # 1. Step 6: network evidence
        self.run_step6_network_evidence_verification()
        # 2. Step 7: initial result wait
        self.wait_for_worker_initial_result(timeout_s=wait_result_s)
        # 3. Step 8: worker exit wait and re-probe
        self.handle_worker_exit_and_reload(wait_worker_exit_s=wait_worker_exit_s)
        # 5. Compute final verdict
        self.compute_final_verdict()

    def log(self, msg: str) -> None:
        log_message(msg, self.log_file)

    def elapsed(self) -> float:
        return time.monotonic() - self.start_time

    def remaining_operational(self) -> float:
        return max(0.0, self.operational_deadline - time.monotonic())

    def remaining_overall(self) -> float:
        return max(0.0, self.overall_deadline - time.monotonic())

    def check_budget_for_call(self, needed_s: float, operational: bool = True) -> Tuple[bool, float, str]:
        rem = self.remaining_operational() if operational else self.remaining_overall()
        if rem < needed_s:
            scope = "operational" if operational else "overall"
            return False, rem, f"insufficient_{scope}_budget: rem={round(rem, 2)}s < needed={needed_s}s"
        return True, rem, "ok"

    def cli_runner(self, cmd: List[str], capture_json: bool = True, timeout: float = 8.0) -> Any:
        self.log(f"[EXEC] {' '.join(cmd)} (timeout={timeout}s)")
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )
        out_text = proc.stdout.decode("utf-8", errors="replace")
        err_text = proc.stderr.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError(f"Command failed (exit={proc.returncode}): {err_text.strip() or out_text.strip()}")
        if capture_json:
            try:
                return json.loads(out_text)
            except Exception as e:
                raise ValueError(f"Failed to parse JSON output: {e}\nOutput was: {out_text[:300]}")
        return out_text

    def get_observation_from_storage(self, obs_id: str, cur_session_file: Path) -> Optional[dict]:
        """Retrieve full observation matching obs_id from responses or session observation storage."""
        if not isinstance(obs_id, str) or not re.fullmatch(r"^[0-9a-fA-F]{64}$", obs_id):
            return None

        # 1. Search in responses directory (contains validated complete responses with nodes)
        resp_dir = self.out_dir / "responses"
        extract_obs_fn = _get_observation_fn()
        if resp_dir.is_dir():
            for p in sorted(resp_dir.glob("*.json"), reverse=True):
                data = load_json_safe(p)
                if not data or not isinstance(data, dict):
                    continue
                payload = data.get("payload") if (isinstance(data, dict) and "payload" in data) else data
                if isinstance(payload, dict):
                    obs = extract_obs_fn(payload)
                    if not obs and isinstance(payload.get("nodes"), list) and payload.get("observation_id") == obs_id:
                        obs = payload
                    if isinstance(obs, dict) and obs.get("observation_id") == obs_id and isinstance(obs.get("nodes"), list):
                        return obs

        # 2. Search in session observation directory (phone.py format: observation-<id[:12]>.json)
        cur_state = load_json_safe(cur_session_file)
        session_dirs = []
        if cur_state and cur_state.get("session_dir"):
            session_dirs.append(Path(cur_state["session_dir"]))
        session_dirs.extend([
            self.out_dir / "session",
            self.out_dir,
            self.task_dir / "session"
        ])
        for s_dir in session_dirs:
            if not s_dir.is_dir():
                continue
            s_dir_real = os.path.realpath(str(s_dir))
            meta_file = s_dir / f"observation-{obs_id[:12]}.json"
            if meta_file.is_file():
                meta_real = os.path.realpath(str(meta_file))
                if not (meta_real == s_dir_real or meta_real.startswith(s_dir_real + os.sep)):
                    continue
                meta = load_json_safe(meta_file)
                if not meta or not isinstance(meta, dict):
                    continue
                if meta.get("sha256") != obs_id and meta.get("observation_id") != obs_id:
                    continue
                if "nodes" in meta and isinstance(meta["nodes"], list):
                    return {"observation_id": obs_id, "nodes": meta["nodes"]}

                raw_xml_path = meta.get("xml_path")
                if not raw_xml_path or not isinstance(raw_xml_path, str):
                    continue
                real_xml_path = os.path.realpath(raw_xml_path)
                if not (real_xml_path == s_dir_real or real_xml_path.startswith(s_dir_real + os.sep)):
                    continue
                if not os.path.isfile(real_xml_path):
                    continue

                try:
                    xml_bytes = Path(real_xml_path).read_bytes()
                    if hashlib.sha256(xml_bytes).hexdigest().lower() != obs_id.lower():
                        continue
                except Exception:
                    continue

                try:
                    ca = _get_content_adapter()
                    obs_obj = ca.Observation(xml_bytes, source_name=meta.get("xml_filename", Path(real_xml_path).name))
                    if obs_obj and obs_obj.observation_id.lower() == obs_id.lower() and hasattr(obs_obj, "items"):
                        nodes = [obs_obj.node_dict(it) for it in obs_obj.items]
                        return {
                            "observation_id": obs_id,
                            "nodes": nodes
                        }
                except Exception:
                    pass

        return None

    def run_device_cmd(self, cmd: list[str], base_timeout: float = 8.0, reserve_s: float = 0.0) -> subprocess.CompletedProcess:
        """Run device command with timeout dynamically constrained by remaining operational budget and reserve."""
        rem_op = self.remaining_operational() - reserve_s
        if rem_op <= 0:
            raise TimeoutError(f"insufficient operational budget for device command ({cmd[:3]}): remaining={rem_op:.2f}s, reserve={reserve_s:.2f}s")
        cmd_timeout = min(base_timeout, rem_op)
        self.log(f"Running device command: {' '.join(str(c) for c in cmd)} (timeout={cmd_timeout:.2f}s, rem_budget={rem_op:.2f}s)")
        return subprocess.run(
            cmd, capture_output=True, timeout=cmd_timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        )

    def _save_network_check(self, res: Dict[str, Any]) -> None:
        network_check_file = self.task_dir / "network-check.json"
        tmp = network_check_file.with_name(network_check_file.name + ".tmp")
        try:
            self.task_dir.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(res, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, network_check_file)
        except Exception as exc:
            res["error"] = f"failed to save network-check.json: {exc}"
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            raise RuntimeError(f"critical_evidence_save_failure: {exc}") from exc

    def extract_logcat_network_evidence(self, reserve_s: float = 25.0) -> Dict[str, Any]:
        """Extract application logcat network refresh evidence via ADB with strict binding."""
        self.network_extraction_performed = True
        evidence_res = {
            "network_verified": False,
            "session_id": getattr(self, "session_id", None),
            "refresh_seq": getattr(self, "refresh_action_seq", None),
            "device_serial": self.emulator_serial,
            "raw_log_sha256": None,
            "app": "org.fdroid.basic",
            "app_pid": None,
            "pid_matched": False,
            "action_window_matched": False,
            "same_chain_matched": False,
            "entry_jar_requested": False,
            "downloader_progress": False,
            "positive_bytes_transferred": False,
            "bytes_transferred": 0,
            "repo_update_result": None,
            "workmanager_success": False,
            "worker_tag": None,
            "worker_id": None,
            "worker_thread": None,
            "application_log": "fdroid-logcat.txt",
            "extracted_evidence": "network-app-evidence.txt",
            "matching_lines": [],
            "error": None
        }

        # 0. Check budget reservation
        if hasattr(self, "remaining_operational"):
            rem_op = self.remaining_operational() - reserve_s
            if rem_op < 4.0:
                evidence_res["error"] = f"insufficient_operational_budget_for_logcat_extraction: remaining={rem_op:.2f}s, reserve={reserve_s:.2f}s"
                self._save_network_check(evidence_res)
                return evidence_res

        # 1. Check refresh window time anchor (fail closed if missing or malformed)
        window_start = getattr(self, "refresh_click_device_time", None)
        if not window_start:
            evidence_res["error"] = "missing refresh_click_device_time anchor; fail closed"
            self._save_network_check(evidence_res)
            return evidence_res

        window_start_ms, win_has_ms = parse_log_time_to_ms(window_start)
        if window_start_ms == 0:
            evidence_res["error"] = f"malformed refresh_click_device_time '{window_start}'; fail closed"
            self._save_network_check(evidence_res)
            return evidence_res

        if not self.emulator_serial or not self.adb_exe.is_file():
            evidence_res["error"] = "serial or adb not available"
            self._save_network_check(evidence_res)
            return evidence_res

        # 2. Get Application PID (timeout dynamically constrained)
        try:
            pid_cmd = [str(self.adb_exe), "-s", self.emulator_serial, "shell", "pidof", "org.fdroid.basic"]
            self.log(f"Querying org.fdroid.basic PID via: {' '.join(pid_cmd)}")
            res_pid = self.run_device_cmd(pid_cmd, base_timeout=8.0, reserve_s=reserve_s)
            pid_str = res_pid.stdout.decode("utf-8", errors="replace").strip()
            if res_pid.returncode != 0 or not pid_str:
                evidence_res["error"] = f"pidof org.fdroid.basic failed or returned empty (exit={res_pid.returncode}, out='{pid_str}')"
                self.log(f"ERROR: {evidence_res['error']}")
                self._save_network_check(evidence_res)
                return evidence_res
            target_pid = int(pid_str.split()[0])
            evidence_res["app_pid"] = target_pid
            evidence_res["pid_matched"] = True
            self.log(f"Matched target PID={target_pid} for org.fdroid.basic")
        except Exception as e:
            evidence_res["error"] = f"failed to query pidof: {e}"
            self.log(f"ERROR: {evidence_res['error']}")
            self._save_network_check(evidence_res)
            return evidence_res

        # 3. Extract Logcat threadtime dump
        raw_log = ""
        try:
            log_cmd = [str(self.adb_exe), "-s", self.emulator_serial, "logcat", "-v", "threadtime", "-d"]
            self.log(f"Extracting logcat via: {' '.join(log_cmd)}")
            res_log = self.run_device_cmd(log_cmd, base_timeout=8.0, reserve_s=reserve_s)
            if res_log.returncode != 0:
                evidence_res["error"] = f"adb logcat -d failed (exit={res_log.returncode})"
                self._save_network_check(evidence_res)
                return evidence_res
            raw_bytes = res_log.stdout
            raw_log = raw_bytes.decode("utf-8", errors="replace")
            logcat_path = self.task_dir / "fdroid-logcat.txt"
            logcat_path.write_bytes(raw_bytes)
            raw_hash = hashlib.sha256(raw_bytes).hexdigest()
            evidence_res["raw_log_path"] = str(logcat_path)
            evidence_res["raw_log_sha256"] = raw_hash
            evidence_res["device_serial"] = self.emulator_serial
        except Exception as e:
            evidence_res["error"] = f"failed to dump logcat: {e}"
            self.log(f"ERROR: {evidence_res['error']}")
            self._save_network_check(evidence_res)
            return evidence_res

        # 4. Parse and match logcat lines with strict chain isolation and time filtering
        line_regex = re.compile(r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\.\d{3})?)\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+([^:]+):\s+(.*)$")
        candidate_chains: list[WorkerChain] = []
        active_chain: Optional[WorkerChain] = None

        for line_idx, line in enumerate(raw_log.splitlines(), start=1):
            m = line_regex.match(line.strip())
            if not m:
                continue
            t_stamp, pid_s, tid_s, prio, tag, msg = m.groups()
            pid = int(pid_s)
            tid = int(tid_s)

            if pid != target_pid:
                continue

            line_ms, line_has_ms = parse_log_time_to_ms(t_stamp)
            # Time boundary filter: must be strictly after window_start
            if win_has_ms and line_has_ms:
                if line_ms <= window_start_ms:
                    continue
            else:
                # If either side lacks sub-second precision, require strictly later second to eliminate ambiguity
                if line_ms <= window_start_ms:
                    continue

            if "Starting RepoUpdateWorker..." in msg:
                m_w = re.search(r"(org\.fdroid\.repo\.RepoUpdateWorker@[0-9a-fA-F]+)", msg)
                if m_w:
                    w_tag = m_w.group(1)
                    chain = WorkerChain(tag=w_tag, tid=tid, start_time=t_stamp, line_no=line_idx)
                    chain.matched_lines.append(line.strip())
                    candidate_chains.append(chain)
                    active_chain = chain

            elif "GET https://f-droid.org/repo/entry.jar" in msg:
                if active_chain is not None and (tid == active_chain.tid or not active_chain.tid):
                    active_chain.entry_jar_requested = True
                    active_chain.matched_lines.append(line.strip())

            elif "onProgress:" in msg:
                if active_chain is not None and (tid == active_chain.tid or not active_chain.tid):
                    m_p = re.search(r"onProgress:\s*(\d+)/(-?\d+)", msg)
                    if m_p:
                        bytes_read = int(m_p.group(1))
                        if bytes_read > 0:
                            active_chain.downloader_progress = True
                            active_chain.positive_bytes_transferred = True
                            active_chain.bytes_transferred = max(active_chain.bytes_transferred, bytes_read)
                            active_chain.matched_lines.append(line.strip())

            elif "Update repo result:" in msg:
                if active_chain is not None and (tid == active_chain.tid or not active_chain.tid):
                    if "IndexUpdateResult$Unchanged" in msg:
                        active_chain.repo_update_result = "IndexUpdateResult$Unchanged"
                        active_chain.matched_lines.append(line.strip())
                    elif "IndexUpdateResult$Updated" in msg:
                        active_chain.repo_update_result = "IndexUpdateResult$Updated"
                        active_chain.matched_lines.append(line.strip())

            elif "finished doWork" in msg:
                if active_chain is not None and (active_chain.tag in msg if active_chain.tag else True):
                    active_chain.finished_do_work = True
                    active_chain.finish_time = t_stamp
                    active_chain.finish_line_no = line_idx
                    active_chain.matched_lines.append(line.strip())
                    active_chain = None  # doWork finished

            elif "Worker result SUCCESS" in msg and "RepoUpdateWorker" in msg:
                if active_chain is not None:
                    # A worker is currently running and has not finished doWork.
                    # SUCCESS cannot be unambiguously bound while another worker is active!
                    continue
                if len(candidate_chains) == 1 and candidate_chains[0].finished_do_work and not candidate_chains[0].wm_success:
                    c = candidate_chains[0]
                    if c.finish_line_no is not None and c.finish_line_no < line_idx:
                        m_id = re.search(r"id=([0-9a-fA-F-]+)", msg)
                        c.wm_work_id = m_id.group(1) if m_id else None
                        c.wm_success = True
                        c.wm_success_time = t_stamp
                        c.wm_success_line_no = line_idx
                        c.matched_lines.append(line.strip())

        if len(candidate_chains) > 1:
            evidence_res["same_chain_matched"] = False
            evidence_res["action_window_matched"] = False
            evidence_res["error"] = f"multiple_worker_chains_observed_in_action_window ({len(candidate_chains)}); ambiguous chain attribution rejected"
            evidence_res["matching_lines"] = [l for c in candidate_chains for l in c.matched_lines]
        elif len(candidate_chains) == 0:
            evidence_res["same_chain_matched"] = False
            evidence_res["action_window_matched"] = False
            evidence_res["error"] = "no RepoUpdateWorker started in action window"
        else:
            c = candidate_chains[0]
            if c.is_complete():
                evidence_res["worker_tag"] = c.tag
                evidence_res["worker_thread"] = c.tid
                evidence_res["worker_id"] = c.wm_work_id
                evidence_res["entry_jar_requested"] = c.entry_jar_requested
                evidence_res["downloader_progress"] = c.downloader_progress
                evidence_res["positive_bytes_transferred"] = c.positive_bytes_transferred
                evidence_res["bytes_transferred"] = c.bytes_transferred
                evidence_res["repo_update_result"] = c.repo_update_result
                evidence_res["workmanager_success"] = c.wm_success
                evidence_res["same_chain_matched"] = True
                evidence_res["action_window_matched"] = True
                evidence_res["matching_lines"] = c.matched_lines
            else:
                evidence_res["same_chain_matched"] = False
                evidence_res["action_window_matched"] = False
                evidence_res["matching_lines"] = c.matched_lines
                if not c.entry_jar_requested:
                    evidence_res["error"] = "entry_jar_not_requested_in_worker_chain"
                elif not c.positive_bytes_transferred:
                    evidence_res["error"] = "no_positive_bytes_transferred"
                elif not c.repo_update_result:
                    evidence_res["error"] = "missing_repo_update_result"
                elif not c.finished_do_work:
                    evidence_res["error"] = "worker_do_work_not_finished"
                elif not c.wm_success:
                    evidence_res["error"] = "workmanager_success_not_matched_to_worker_chain"
                else:
                    evidence_res["error"] = "worker_chain_incomplete"

        evidence_file = self.task_dir / "network-app-evidence.txt"
        evidence_file.write_text("\n".join(evidence_res["matching_lines"]) + "\n", encoding="utf-8")

        all_ok = (
            evidence_res["pid_matched"] is True and
            evidence_res["action_window_matched"] is True and
            evidence_res["same_chain_matched"] is True and
            evidence_res["entry_jar_requested"] is True and
            evidence_res["downloader_progress"] is True and
            evidence_res["positive_bytes_transferred"] is True and
            evidence_res["bytes_transferred"] > 0 and
            evidence_res["repo_update_result"] in ("IndexUpdateResult$Unchanged", "IndexUpdateResult$Updated") and
            evidence_res["workmanager_success"] is True and
            evidence_res["error"] is None
        )
        evidence_res["network_verified"] = all_ok
        self._save_network_check(evidence_res)
        return evidence_res

    def capture_refresh_timestamp(self, seq: int) -> str:
        """One spaceless remote format token; preserve raw output even on refusal."""
        argv = [str(self.adb_exe), "-s", self.emulator_serial, "shell", "date", "+%m-%d_%H:%M:%S.%3N"]
        stem = self.task_dir / f"refresh-time-{seq:04d}"
        # Reserve this query's evidence before dispatch; never overwrite an earlier query.
        with stem.with_suffix(".stdout").open("xb") as stdout, stem.with_suffix(".stderr").open("xb") as stderr:
            record = {"argv": argv, "cwd": str(Path.cwd()), "exit_code": None}
            try:
                result = self.run_device_cmd(argv, base_timeout=8.0, reserve_s=25.0)
                stdout.write(result.stdout)
                stderr.write(result.stderr)
                record["exit_code"] = result.returncode
                if result.returncode != 0:
                    raise RuntimeError(f"device date command exited with code {result.returncode}; raw evidence: {stem}")
                value = result.stdout.decode("ascii").strip().replace("_", " ", 1)
                if not re.fullmatch(r"\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}", value):
                    raise RuntimeError(f"malformed device timestamp; raw evidence: {stem}")
                record["normalized"] = value
                return value
            except Exception as exc:
                if isinstance(exc, subprocess.TimeoutExpired):
                    for stream, data in ((stdout, exc.output), (stderr, exc.stderr)):
                        if data is not None:
                            stream.write(data if isinstance(data, bytes) else data.encode("utf-8"))
                    record["timed_out"] = True
                record["error"] = repr(exc)
                raise
            finally:
                with stem.with_suffix(".json").open("x", encoding="utf-8") as handle:
                    json.dump(record, handle, ensure_ascii=False, indent=2)
                    handle.write("\n")

    def request_cleanup_finish_once(self) -> None:
        """Close only a verified idle owned session; never replay an uncertain submit."""
        record: Dict[str, Any] = {"status": "skipped"}
        self.summary["cleanup_finish"] = record
        if self.finish_submit_started or self.submit_pending:
            record["reason"] = "finish_already_started_or_submit_unknown"
            return
        if (self.out_dir / "result.json").exists():
            record["reason"] = "result_already_present"
            return
        if not self.worker_pid or probe_win32_pid(self.worker_pid)[0] != "alive":
            record["reason"] = "worker_not_confirmed_alive"
            return
        state = load_json_safe(self.out_dir / "current-session.json")
        if not isinstance(state, dict) or state.get("phase") != "ready":
            record["reason"] = "session_not_ready"
            return
        seq = state.get("next_seq")
        if type(seq) is not int or seq < 1:
            record["reason"] = "invalid_sequence"
            return
        try:
            worker = load_json_safe(self.out_dir / "worker.json")
            claim = load_json_safe(self.out_dir / "worker-claimed.json")
            cfg = load_json_safe(self.out_dir / "config.json")
            owned = (
                isinstance(worker, dict) and isinstance(claim, dict) and isinstance(cfg, dict)
                and type(worker.get("pid")) is int and worker["pid"] == self.worker_pid
                and type(claim.get("pid")) is int and claim["pid"] == self.worker_pid
                and Path(worker.get("out", "")).resolve() == self.out_dir.resolve()
                and cfg.get("session_id") == self.session_id and cfg.get("mode") == self.mode
                and Path(cfg.get("project_root", "")).resolve() == self.prod_root.resolve()
                and Path(cfg.get("python", "")).resolve() == self.python_exe.resolve()
                and Path(cfg.get("adb", "")).resolve() == self.adb_exe.resolve()
                and cfg.get("manifests") == [str(self.manifest.resolve())]
                and cfg.get("tool_hashes", {}).get(str(self.cli_script.resolve())) == hashlib.sha256(self.cli_script.read_bytes()).hexdigest()
                and self.worker_run_dir is not None
                and Path(state.get("run_dir", "")).resolve() == self.worker_run_dir.resolve()
                and self.worker_run_dir.resolve().is_relative_to(self.out_dir.resolve())
                and state.get("serial") == self.emulator_serial and bool(self.emulator_serial)
            )
            if not owned:
                raise RuntimeError("cleanup_finish_ownership_mismatch")
            for suffix in ("claim", "json"):
                if (self.out_dir / "requests" / f"{seq:04d}.{suffix}").exists():
                    record["reason"] = "current_request_already_claimed"
                    return
            if (self.out_dir / "responses" / f"{seq:04d}.json").exists():
                record["reason"] = "current_response_not_committed"
                return
            if seq > 1:
                previous = self.out_dir / "responses" / f"{seq-1:04d}.json"
                if not previous.is_file() or hashlib.sha256(previous.read_bytes()).hexdigest() != state.get("last_response_sha256"):
                    raise RuntimeError("cleanup_finish_commit_mismatch")
            for request in (self.out_dir / "requests").glob("*.json"):
                data = load_json_safe(request)
                if not isinstance(data, dict):
                    raise RuntimeError("cleanup_finish_unreadable_request")
                if data.get("action") == "finish":
                    record["reason"] = "finish_request_already_present"
                    return
            # Keep time for worker Stop and final verification/rollback. This cap
            # bounds the client wait, not an in-flight worker Stop/Binder call.
            timeout = min(5.0, self.remaining_overall() - 10.0)
            if timeout <= 0:
                record["reason"] = "insufficient_cleanup_budget"
                return
            if load_json_safe(self.out_dir / "current-session.json") != state:
                record["reason"] = "session_changed_before_finish"
                return
            argv = [str(self.python_exe), "-u", "-B", str(self.cli_script), "submit", "--out", str(self.out_dir),
                    "--action", "finish", "--reason", "Supervisor aborted before finish; close this owned session without input replay."]
            record.update(status="submitted", seq=seq, argv=argv, timeout_s=timeout)
            with (self.out_dir / "cleanup-finish-attempt.json").open("x", encoding="utf-8") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            self.finish_submit_started = True
            self.submit_pending = True
            record["response"] = self.cli_runner(argv, capture_json=True, timeout=timeout)
            self.submit_pending = False
        except Exception as exc:
            record.update(status="failed_or_unknown", error=repr(exc))
            self.summary["cleanup_errors"].append(f"cleanup_finish_error: {exc}")

    def forward_decision_loop(self) -> None:
        """Step 5: Turn-by-Turn AI Decision Forwarding Loop."""
        self.log("Step 5: Entering turn-by-turn AI decision forwarding loop...")
        self.decisions_dir.mkdir(parents=True, exist_ok=True)
        self.step_results_dir.mkdir(parents=True, exist_ok=True)
        cur_session_file = self.out_dir / "current-session.json"

        while True:
            rem_op = self.remaining_operational()
            if rem_op <= 0:
                raise TimeoutError(f"operational_budget_exhausted ({self.operational_budget}s reached)")

            st, _ = probe_win32_pid(self.worker_pid) if self.worker_pid else ("dead", 1)
            if st == "dead":
                self.log("Worker process has exited.")
                break

            cur_state = load_json_safe(cur_session_file)
            if not cur_state:
                time.sleep(0.05)
                continue

            phase = cur_state.get("phase")
            if phase in ("stopping", "stopped"):
                self.log(f"Session entered {phase} phase.")
                break

            seq = cur_state.get("next_seq")
            if seq is None:
                time.sleep(0.05)
                continue

            decision_file = self.decisions_dir / f"{seq:04d}.json"
            result_file = self.step_results_dir / f"{seq:04d}.json"

            if seq == 1 and not hasattr(self, "_logged_prestaged_launch"):
                self._logged_prestaged_launch = True
                if decision_file.is_file():
                    self.log("Pre-staged initial launch decision detected (#0001); immediately dispatching upon ready phase.")

            # Process decision
            dec_data = load_json_safe(decision_file)
            if not dec_data or dec_data.get("seq") != seq:
                self.log(f"Skipping invalid decision file: {decision_file}")
                time.sleep(0.05)
                continue

            is_refresh_click = False
            action = dec_data.get("action")
            reason = dec_data.get("reason", "")
            cap_s = ACTION_BUDGET_CAPS.get(action, 20.0)

            has_b, rem, b_msg = self.check_budget_for_call(min(cap_s, 20.0), operational=True)
            if not has_b:
                raise TimeoutError(f"insufficient_budget_for_action_{action}: {b_msg}")

            if action == "click" and rem < 70.0:
                self.log(f"Notice: remaining operational budget is {rem:.1f}s (worker requires >= 70.0s for click: 50s click + 20s observe).")

            timeout_for_submit = min(cap_s, rem)
            self.log(f"Dispatching AI Decision #{seq:04d}: action='{action}', reason='{reason[:50]}...' (timeout={round(timeout_for_submit, 1)}s)")

            submit_cmd = [
                str(self.python_exe), "-u", "-B", str(self.cli_script), "submit",
                "--out", str(self.out_dir),
                "--action", action,
                "--reason", reason
            ]
            if dec_data.get("observation_id"):
                submit_cmd.extend(["--observation-id", dec_data["observation_id"]])
            if dec_data.get("node_id"):
                submit_cmd.extend(["--node-id", dec_data["node_id"]])

            if action == "set-text":
                val = dec_data.get("value")
                if val != "Aurora":
                    raise ValueError(f"set-text requires strictly value='Aurora', got {val!r}")
                submit_cmd.extend(["--value", "Aurora"])
            else:
                if dec_data.get("value") is not None:
                    raise ValueError(f"--value is only permitted for action 'set-text', not '{action}'")

            if action == "click":
                is_refresh_click = False
                cur_state = load_json_safe(cur_session_file)
                obs_id = dec_data.get("observation_id")
                node_id = dec_data.get("node_id")

                if not obs_id or not node_id:
                    raise ValueError(f"click decision #{seq:04d} missing observation_id or node_id")

                obs = self.get_observation_from_storage(obs_id, cur_session_file)
                if obs is None:
                    raise RuntimeError(f"observation_not_found_or_hash_mismatch: {obs_id}")

                matching_nodes = [n for n in obs.get("nodes", []) if n.get("node_id") == node_id]
                if len(matching_nodes) != 1:
                    raise RuntimeError(f"target node {node_id} not unique in observation {obs_id[:12]} (found {len(matching_nodes)})")
                target_node = matching_nodes[0]

                if self.mode == "application_refresh":
                    classify_fn = _get_permitted_target_fn()
                    permitted, nav_reason, semantic_node = classify_fn(target_node, obs)
                    if permitted and nav_reason.startswith("check_for_updates"):
                        is_refresh_click = True

                    if is_refresh_click:
                        self.log(f"Refresh click detected for decision #{seq:04d} (target={node_id}, reason='{nav_reason}'). Querying device timestamp before dispatching...")
                        try:
                            self.refresh_click_device_time = self.capture_refresh_timestamp(seq)
                            self.refresh_action_seq = seq
                            self.log(f"Recorded device timestamp before refresh click: {self.refresh_click_device_time} (seq={seq})")
                        except Exception as e:
                            self.refresh_click_device_time = None
                            self.log(f"CRITICAL ERROR: Failed to obtain device timestamp for refresh click: {e}")
                            raise RuntimeError(f"device_timestamp_query_failed: {e}") from e

                        # Dynamically recalculate submit budget after time query
                        has_b, rem, b_msg = self.check_budget_for_call(min(cap_s, 20.0), operational=True)
                        if not has_b:
                            raise TimeoutError(f"insufficient_budget_for_submit_after_time_query: {b_msg}")
                        timeout_for_submit = min(cap_s, rem)
                        self.log(f"Recalculated submit timeout after time query: {round(timeout_for_submit, 1)}s")

            if action == "finish":
                self.log("Action 'finish' requested. Checking worker health...")
                st, _ = probe_win32_pid(self.worker_pid) if self.worker_pid else ("dead", 1)
                if st == "dead":
                    raise RuntimeError("worker_died_before_finish")
                cur_state = load_json_safe(cur_session_file)
                if cur_state and cur_state.get("phase") in ("stopping", "stopped"):
                    raise RuntimeError(f"worker_already_in_phase_{cur_state.get('phase')}")

                if self.mode == "application_refresh":
                    self.log("Extracting logcat network evidence BEFORE submit...")
                    net_evidence = self.extract_logcat_network_evidence(reserve_s=25.0)
                    self.summary["network_verified"] = net_evidence.get("network_verified", False)
                    self.summary["network_check"] = net_evidence
                    self.summary["network_status"] = "verified" if self.summary["network_verified"] else "unverified"
                    submit_cmd.extend(["--network-check", str(self.task_dir / "network-check.json")])

                    # Recalculate submit budget after evidence extraction!
                    has_b, rem, b_msg = self.check_budget_for_call(min(cap_s, 20.0), operational=True)
                    if not has_b:
                        raise TimeoutError(f"insufficient_budget_for_submit_finish_after_evidence: {b_msg}")
                    timeout_for_submit = min(cap_s, rem)
                else:
                    self.log(f"Mode is {self.mode}; skipping logcat network evidence extraction on finish.")
                    self.summary["network_status"] = "skipped_mode_not_refresh"

            t_action_start = time.monotonic()
            step_record = {
                "seq": seq,
                "action": action,
                "reason": reason,
                "observation_id": dec_data.get("observation_id"),
                "node_id": dec_data.get("node_id"),
                "submitted_utc": dec_data.get("submitted_utc"),
                "finished_utc": "",
                "elapsed_s": 0.0,
                "submit_response": None,
                "error": None
            }
            if action == "set-text":
                step_record["value"] = dec_data.get("value")
            response_file = self.out_dir / "responses" / f"{seq:04d}.json"
            response_sha = None

            try:
                self.submit_pending = True
                if action == "finish":
                    self.finish_submit_started = True
                submit_res = self.cli_runner(submit_cmd, capture_json=True, timeout=timeout_for_submit)
                self.submit_pending = False
                t_action_end = time.monotonic()
                step_record["submit_response"] = submit_res
                step_record["elapsed_s"] = round(t_action_end - t_action_start, 3)
                step_record["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                step_record["submit_monotonic_start"] = t_action_start
                step_record["submit_monotonic_end"] = t_action_end
                if action == "set-text" and not step_record.get("error"):
                    self.set_text_monotonic_envelope = (t_action_start, t_action_end)
                    self.summary["set_text_monotonic_start"] = t_action_start
                    self.summary["set_text_monotonic_end"] = t_action_end

                # Verify worker business outcome
                fail_reason = None
                if not isinstance(submit_res, dict):
                    fail_reason = f"malformed_submit_response: {repr(submit_res)[:200]}"
                elif submit_res.get("failed") is True:
                    fail_reason = str(submit_res.get("error") or submit_res.get("error_code") or "worker_reported_failure")
                elif submit_res.get("status") in ("failed", "error"):
                    fail_reason = str(submit_res.get("error") or submit_res.get("status"))
                elif submit_res.get("error"):
                    fail_reason = str(submit_res.get("error"))
                elif "session_ended" in submit_res:
                    se = submit_res["session_ended"]
                    if isinstance(se, dict) and (se.get("status") in ("failed", "error") or se.get("error")):
                        fail_reason = f"session_ended_with_failure: {se.get('error') or se.get('status')}"

                if fail_reason is not None:
                    step_record["error"] = f"worker_business_failure: {fail_reason}"
                    self.log(f"AI Decision #{seq:04d} ({action}) worker business failed: {fail_reason}")
                else:
                    if submit_res.get('recovery'):
                        step_record['recovery'] = submit_res['recovery']
                        self.log(f"AI Decision #{seq:04d}: navigation rejected before input; new observation required.")
                    else:
                        self.log(f"AI Decision #{seq:04d} ({action}) completed in {step_record['elapsed_s']}s.")
                    if action == "observe":
                        self.total_observes_used += 1
                        if self.refresh_dispatched:
                            self.refresh_observes_used += 1
                    elif is_refresh_click:
                        self.refresh_dispatched = True

                    # Finish has no next UI decision; terminal aggregation handles its result.
                    if action != "finish":
                        # Track progress and UI completion from authoritative formal response file
                        try:
                            rf_path = None
                            if isinstance(submit_res, dict) and submit_res.get("response_file"):
                                rf_path = Path(submit_res["response_file"])
                            else:
                                rf_path = response_file

                            obs = None
                            if rf_path.is_file():
                                resp_bytes = rf_path.read_bytes()
                                resp_sha = hashlib.sha256(resp_bytes).hexdigest()
                                if rf_path == response_file:
                                    response_sha = resp_sha
                                cur_state = load_json_safe(cur_session_file) or {}
                                committed_sha = cur_state.get("last_response_sha256")
                                next_seq = cur_state.get("next_seq")

                                if committed_sha == resp_sha and next_seq is not None and next_seq >= (seq + 1):
                                    resp_data = json.loads(resp_bytes.decode("utf-8"))
                                    if isinstance(resp_data, dict) and resp_data.get("seq") == seq:
                                        extract_obs_fn = _get_observation_fn()
                                        payload = resp_data.get("payload")
                                        obs = extract_obs_fn(payload) if isinstance(payload, dict) else None
                                else:
                                    self.log(f"Warning: Commit boundary verification failed for response #{seq:04d} (committed_sha={committed_sha}, resp_sha={resp_sha}, next_seq={next_seq}); progress state will NOT advance.")
                            else:
                                self.log(f"Warning: Response file missing for step #{seq:04d}: {rf_path}; progress state will NOT advance.")

                            if obs and isinstance(obs, dict):
                                from inspect_turnaround import detect_progress_and_refresh_state
                                prog = detect_progress_and_refresh_state(obs.get("nodes", []))
                                is_ui_in_progress = prog.get("ui_refresh_in_progress", False)

                                from inspect_turnaround import refresh_control_state
                                control = refresh_control_state(obs)
                                has_enabled_cfu = control['enabled']
                                has_disabled_cfu = control['disabled']

                                if self.mode == "application_refresh" and self.refresh_dispatched:
                                    if is_ui_in_progress or has_disabled_cfu:
                                        self.seen_in_progress = True
                                        self.log(f"Refresh in-progress state confirmed on step #{seq:04d} (progress_bars={prog.get('progress_count', 0)}, disabled_cfu={has_disabled_cfu})")

                                    # Completion verification is strictly bound to an observe response
                                    if action == "observe" and self.seen_in_progress and has_enabled_cfu and not is_ui_in_progress and not has_disabled_cfu:
                                        self.ui_verified_completed = True
                                        self.log(f"UI refresh completion independently verified via observe on step #{seq:04d} (CFU button enabled, progress bars clear)")
                        except Exception as tr_exc:
                            self.log(f"Warning: Failed to evaluate progress transition: {tr_exc}")
            except Exception as e:
                step_record["error"] = str(e)
                step_record["elapsed_s"] = round(time.monotonic() - t_action_start, 3)
                step_record["finished_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                self.log(f"AI Decision #{seq:04d} ({action}) failed: {e}")
            finally:
                if action != "finish":
                    # Task 97 Turnaround Compact Briefing Integration
                    try:
                        # 1. Update turnaround context sidecar with committed response SHA256
                        if response_sha is None and response_file.is_file():
                            response_sha = hashlib.sha256(response_file.read_bytes()).hexdigest()
                        self.save_turnaround_context(seq=seq, response_sha256=response_sha)

                        # 2. Generate step turnaround briefing
                        if str(self.task_dir) not in sys.path:
                            sys.path.insert(0, str(self.task_dir))
                        from inspect_turnaround import inspect_turnaround
                        briefing = inspect_turnaround(
                            session_dir=self.out_dir,
                            now_monotonic=time.monotonic(),
                            now_epoch=time.time()
                        )
                        if briefing and isinstance(briefing, dict):
                            if "budget" in briefing and isinstance(briefing["budget"], dict):
                                briefing["budget"]["is_snapshot"] = True
                            step_record["briefing"] = briefing
                            b_file = self.out_dir / "step_briefing.json"
                            b_file.write_text(json.dumps(briefing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                            b_step_file = self.out_dir / f"step_briefing_{seq:04d}.json"
                            b_step_file.write_text(json.dumps(briefing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                            cand_cnt = len(briefing.get("candidates", []))
                            rem_s = briefing.get("budget", {}).get("current_remaining_operational_s")
                            can_c = briefing.get("budget", {}).get("can_click")
                            self.log(f"[STEP BRIEFING #{seq:04d}] Screen={briefing.get('screen_type')}, Status={briefing.get('status')}, Candidates={cand_cnt}, Budget={rem_s}s (can_click={can_c})")
                    except Exception as b_exc:
                        self.log(f"Warning: Failed to generate step turnaround briefing: {b_exc}")

                result_file.write_text(json.dumps(step_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                self.summary["step_records"].append(step_record)

            if action == "finish" or step_record.get("error"):
                self.log(f"Termination action or error reached on step #{seq:04d}. Exiting decision loop.")
                break

    def build_worker_begin_cmd(self) -> List[str]:
        begin_cmd = [
            str(self.python_exe), "-u", "-B", str(self.cli_script), "begin",
            "--project-root", str(self.prod_root),
            "--adb", str(self.adb_exe),
            "--manifest", str(self.manifest),
            "--out", str(self.out_dir),
            "--mode", self.mode,
            "--session-id", self.session_id,
        ]
        if self.enable_startup_logcat_diagnostic:
            begin_cmd.append("--enable-startup-logcat-diagnostic")
        if self.low_ram:
            begin_cmd.append("--low-ram")
        return begin_cmd

    def run(self) -> int:
        self.start_time = time.monotonic()
        self.overall_deadline = self.start_time + self.total_s
        self.operational_deadline = self.start_time + self.operational_budget
        self.session_start_epoch = time.time()
        self.operational_deadline_epoch = self.session_start_epoch + self.operational_budget

        start_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self.summary["started_utc"] = start_utc
        self.log(f"=== Task 101 Gemini Session Supervisor Starting (TOTAL={self.total_s}s, OP={self.operational_budget}s, CLEANUP={self.cleanup_s}s) ===")

        session_claimed = False
        try:
            # Step 1: Preflight Claims and Hygiene Checks
            self.log("Step 1: Running preflight assertions...")
            dev_status, active_devs, dev_err = check_adb_devices(self.adb_exe, timeout=SINGLE_CMD_TIMEOUT_S)
            if dev_status != "clean":
                raise RuntimeError(f"preflight_device_check_failed: {dev_status} ({dev_err})")
            self.log("Preflight: ADB device state is clean (no running emulators).")

            if self.out_dir.exists():
                raise RuntimeError(f"preflight_session_dir_exists: {self.out_dir} must not pre-exist")

            # Step 2: Apply 5-file Candidate Installation
            self.log("Step 2: Transferring candidate files to production root...")
            has_b, _, b_msg = self.check_budget_for_call(15.0, operational=True)
            if not has_b:
                raise TimeoutError(b_msg)

            install_cmd = [
                str(self.python_exe), "-B", str(self.transfer), "install",
                "--project-root", str(self.prod_root)
            ]
            install_res = self.cli_runner(install_cmd, capture_json=True, timeout=10.0)
            self.summary["transfer"] = install_res
            self.log(f"Candidate installed successfully: {install_res}")

            # Step 3: Launch Worker via stepwise_session.py begin --mode application_refresh
            self.log(f"Step 3: Launching stepwise worker (mode={self.mode})...")
            has_b, _, b_msg = self.check_budget_for_call(BOOT_TIMEOUT_S + 20.0, operational=True)
            if not has_b:
                raise TimeoutError(b_msg)

            begin_cmd = self.build_worker_begin_cmd()
            begin_res = self.cli_runner(begin_cmd, capture_json=True, timeout=15.0)
            self.log(f"Worker begin returned: {begin_res}")

            session_claimed = True
            worker_json = self.out_dir / "worker.json"
            if not worker_json.is_file():
                raise RuntimeError("worker.json not produced after begin")
            w_data = load_json_safe(worker_json)
            self.worker_pid = int(w_data.get("pid", 0))
            self.summary["worker_pid"] = self.worker_pid
            self.log(f"Worker process launched with PID={self.worker_pid}")

            # Step 4: Wait for Ready State & Recover State
            self.log("Step 4: Awaiting worker ready phase and recovering emulator state...")
            cur_session_file = self.out_dir / "current-session.json"
            boot_deadline = time.monotonic() + BOOT_TIMEOUT_S
            ready_state = None
            while time.monotonic() < boot_deadline:
                rem_op = self.remaining_operational()
                if rem_op <= 0:
                    raise TimeoutError("operational_budget_exhausted_during_boot")
                st, _ = probe_win32_pid(self.worker_pid)
                if st == "dead":
                    raise RuntimeError("worker_died_prematurely_during_boot")

                if cur_session_file.is_file():
                    cur = load_json_safe(cur_session_file)
                    if cur and cur.get("phase") == "ready":
                        ready_state = cur
                        break
                    elif cur and cur.get("phase") in ("failed", "stopping", "stopped"):
                        raise RuntimeError(f"worker_entered_error_phase: {cur.get('phase')}")
                time.sleep(0.5)

            if not ready_state:
                raise TimeoutError("timeout_waiting_for_worker_ready_state")

            self.emulator_serial = ready_state.get("serial")
            self.summary["emulator_serial"] = self.emulator_serial
            if ready_state.get("run_dir"):
                self.worker_run_dir = Path(ready_state["run_dir"])
            self.log(f"Worker is READY. Serial={self.emulator_serial}, RunDir={self.worker_run_dir}")

            state_start_file = Path(ready_state.get("run_dir", "")) / "state-start.json"
            if state_start_file.is_file():
                state_data = load_json_safe(state_start_file)
                if state_data:
                    self.emulator_pid = state_data.get("pid")
                    self.summary["emulator_pid"] = self.emulator_pid
                    self.log(f"Recovered emulator PID={self.emulator_pid}")

            # Save initial ready turnaround context and generate initial briefing
            self.save_turnaround_context(seq=0, response_sha256=None)
            try:
                from inspect_turnaround import inspect_turnaround
                init_briefing = inspect_turnaround(
                    session_dir=self.out_dir,
                    now_monotonic=time.monotonic(),
                    now_epoch=time.time()
                )
                if init_briefing and isinstance(init_briefing, dict):
                    if "budget" in init_briefing and isinstance(init_briefing["budget"], dict):
                        init_briefing["budget"]["is_snapshot"] = True
                    (self.out_dir / "step_briefing.json").write_text(json.dumps(init_briefing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    (self.out_dir / "step_briefing_0000.json").write_text(json.dumps(init_briefing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    self.log(f"Pre-launch initial turnaround briefing generated: action={init_briefing.get('recommended_action')}")
            except Exception as ib_exc:
                self.log(f"Warning: Failed to generate pre-launch turnaround briefing: {ib_exc}")

            # Step 5: Turn-by-Turn AI Decision Forwarding Loop
            self.forward_decision_loop()

            # Step 6: Verify logcat network evidence
            self.run_step6_network_evidence_verification()

            # Step 7: Wait for Worker Final Result
            self.wait_for_worker_initial_result(timeout_s=15.0)

        except Exception as e:
            err_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
            self.log(f"CRITICAL EXCEPTION IN SUPERVISOR MAIN FLOW:\n{err_msg}")
            self.summary["fatal_error"] = err_msg
            self.summary["control_status"] = "failed"
        finally:
            # Step 8: Strict Stop, Post-Verification, and Rollback Flow
            self.log("=== Entering Guaranteed Post-Session & Rollback Cleanup Block ===")
            rem_cleanup = self.remaining_overall()
            self.log(f"Time remaining for cleanup: {round(rem_cleanup, 2)}s")

            worker_alive = True
            emulator_alive = True
            dev_status = "unknown"

            try:
                if session_claimed:
                    self.request_cleanup_finish_once()
                # Wait for worker process to terminate and reload authoritative result
                worker_alive, auth_res = self.handle_worker_exit_and_reload(wait_worker_exit_s=min(30.0, max(0.0, self.remaining_overall() - 10.0)))

                # Probe emulator process
                if self.emulator_pid:
                    self.log(f"Probing emulator process (PID={self.emulator_pid})...")
                    e_wait_deadline = time.monotonic() + min(2.0, max(0.0, self.remaining_overall() - 10.0))
                    emulator_alive = probe_win32_pid(self.emulator_pid)[0] != "dead"
                    while time.monotonic() < e_wait_deadline:
                        st, code = probe_win32_pid(self.emulator_pid)
                        if st == "dead":
                            self.log(f"Emulator process confirmed terminated (exit={code}).")
                            emulator_alive = False
                            break
                        time.sleep(0.5)
                    if emulator_alive:
                        self.log(f"WARNING: Emulator PID {self.emulator_pid} still alive.")
                else:
                    st_final = self.out_dir / "state-final.json"
                    if st_final.is_file():
                        st_dat = load_json_safe(st_final)
                        if st_dat and st_dat.get("pid"):
                            self.emulator_pid = st_dat["pid"]
                            st, _ = probe_win32_pid(self.emulator_pid)
                            emulator_alive = (st != "dead")

                # Check ADB devices
                self.log("Probing ADB device list...")
                if self.remaining_overall() <= 0:
                    raise TimeoutError("overall_deadline_exhausted_before_adb_check")
                dev_status, active_devs, dev_desc = check_adb_devices(self.adb_exe, timeout=min(2.0, self.remaining_overall()))
                self.log(f"ADB check: status={dev_status}, active={active_devs}")

            except Exception as e:
                err_msg = f"cleanup_probing_error: {e}"
                self.log(err_msg)
                self.summary["cleanup_errors"].append(err_msg)

            # Rollback check
            can_rollback = True
            refusal_reasons = []

            if not session_claimed:
                self.summary["rollback_status"] = "not_needed"
                self.log("Candidate was never transferred/claimed; rollback not needed.")
            else:
                if not self.summary.get("result") or self.summary["result"].get("stopped") is not True:
                    can_rollback = False
                    refusal_reasons.append("normal_stop_not_confirmed")
                if self.remaining_overall() <= 0:
                    can_rollback = False
                    refusal_reasons.append("overall_deadline_exhausted")
                if worker_alive:
                    can_rollback = False
                    refusal_reasons.append("worker_still_running")
                if emulator_alive:
                    can_rollback = False
                    refusal_reasons.append("emulator_still_running")
                if dev_status != "clean":
                    can_rollback = False
                    refusal_reasons.append(f"adb_devices_not_clean_{dev_status}")
                if self.summary["cleanup_errors"]:
                    can_rollback = False
                    refusal_reasons.append(f"cleanup_errors_occurred_{len(self.summary['cleanup_errors'])}")

                try:
                    after_ok, after_reason = verify_candidate_after_state(self.prod_root, self.install_plan)
                    if not after_ok:
                        can_rollback = False
                        refusal_reasons.append(f"production_after_state_invalid_{after_reason}")
                except Exception as e:
                    can_rollback = False
                    refusal_reasons.append(f"hash_verification_error_{type(e).__name__}")

                if can_rollback:
                    self.log("All rollback preconditions strictly met. Executing transfer.py rollback...")
                    try:
                        rollback_cmd = [
                            str(self.python_exe), "-B", str(self.transfer), "rollback",
                            "--project-root", str(self.prod_root)
                        ]
                        rollback_timeout = min(8.0, self.remaining_overall())
                        if rollback_timeout <= 0:
                            raise TimeoutError("overall_deadline_exhausted_before_rollback")
                        rb_data = self.cli_runner(rollback_cmd, capture_json=True, timeout=rollback_timeout)
                        self.summary["rollback"] = rb_data
                        self.summary["rollback_status"] = "rolled_back"
                        self.log(f"Rollback succeeded: {rb_data}")

                        manifest_data = load_json_safe(self.task_dir / "baseline-manifest.json")
                        if manifest_data:
                            ok_cnt, missing, drift = 0, [], []
                            for rel, exp in manifest_data["files"].items():
                                p = self.prod_root / rel
                                if not p.is_file():
                                    missing.append(rel)
                                else:
                                    h = hashlib.sha256(p.read_bytes()).hexdigest()
                                    if h == exp:
                                        ok_cnt += 1
                                    else:
                                        drift.append(rel)
                            self.summary["post_rollback_manifest"] = {
                                "ok": ok_cnt,
                                "missing": missing,
                                "drift": drift
                            }
                            self.log(f"Post-rollback manifest: ok={ok_cnt}, missing={len(missing)}, drift={len(drift)}")
                    except Exception as e:
                        err_msg = f"rollback_execution_error: {type(e).__name__}: {e}"
                        self.log(err_msg)
                        self.summary["cleanup_errors"].append(err_msg)
                        self.summary["rollback_status"] = "refused"
                        self.summary["rollback"] = {"error": err_msg}
                else:
                    msg = f"REFUSE ROLLBACK: {refusal_reasons}. Preserving state and evidence."
                    self.log(f"CRITICAL: {msg}")
                    self.summary["rollback_status"] = "refused"
                    self.summary["rollback"] = {"refused": True, "reasons": refusal_reasons}

            if not self.summary["cleanup_errors"] and not worker_alive and not emulator_alive and dev_status == "clean":
                self.summary["cleanup_status"] = "clean"
            else:
                self.summary["cleanup_status"] = "dirty_or_unconfirmed"


            self.compute_final_verdict()
            self.summary["ended_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.summary["elapsed_s"] = round(time.monotonic() - self.start_time, 2)
            self._save_summary()
            self.log(f"=== Task 101 Gemini Session Supervisor Finished (status={self.summary['status']}) ===")

        return 0 if self.summary.get("status") == "success" else 1

    def _save_summary(self) -> None:
        try:
            self.summary_file.parent.mkdir(parents=True, exist_ok=True)
            self.summary_file.write_text(json.dumps(self.summary, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass


# Backward compatibility aliases
Task99Supervisor = Task101Supervisor
Task97Supervisor = Task101Supervisor
Task96Supervisor = Task101Supervisor
Task95Supervisor = Task101Supervisor


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="session_supervisor.py",
        description="Task 101 Gemini Session Supervisor with strict entry point defense.",
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="subcommand", required=True)
    run_parser = subparsers.add_parser("run", help="Run supervisor session lifecycle", allow_abbrev=False)
    run_parser.add_argument("--low-ram", action="store_true", default=False,
                            help="Explicitly enable the experimental low-RAM launcher mode.")
    run_parser.add_argument(
        "--enable-startup-logcat-diagnostic",
        action="store_true",
        default=False,
        help="Enable bounded startup logcat diagnostic collection on launch failure.",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    if getattr(args, "subcommand", None) == "run":
        supervisor = Task101Supervisor(
            enable_startup_logcat_diagnostic=bool(args.enable_startup_logcat_diagnostic),
            low_ram=args.low_ram,
        )
        return supervisor.run()
    return 2


if __name__ == "__main__":
    sys.exit(main())
