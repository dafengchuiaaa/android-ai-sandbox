"""Host resource measurement sampler (Observer) for Windows Android sandbox.

Strictly read-only, zero-dependency (Python standard library + ctypes Win32 APIs).
Provides:
1. Whole-machine physical memory (Total, Available, Used, and Delta relative to Stage A baseline).
2. Whole-machine CPU utilization.
3. Target instance processes (Host, Worker, Emulator, QEMU, child processes):
   - Strict 64-bit Win32 prototypes with INVALID_HANDLE_VALUE checking.
   - Persistent identity binding (PID + creation_raw) across process termination.
   - Host exit does NOT cause child processes (Worker/emulator) to be forgotten or claimed released.
   - Permanent PID reuse protection via tombstoning and separate (PID, creation_raw) CPU counters.
4. Observer process overhead (recorded separately, never added to product totals).
5. Stage-aware baseline: delta is strictly computed from Stage A baseline median;
   if Stage A is missing or insufficient (<3 samples), delta is marked None and measurement is marked partial.
"""

from collections import defaultdict
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import statistics
import sys
import time

# --- Win32 Structures and 64-bit Prototypes ---
kernel32 = ctypes.WinDLL("kernel32.dll")
psapi = ctypes.WinDLL("psapi.dll")

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", wintypes.DWORD),
        ("dwMemoryLoad", wintypes.DWORD),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class PROCESS_MEMORY_COUNTERS_EX(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * 260),
    ]


# Setup exact 64-bit function prototypes
kernel32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(MEMORYSTATUSEX)]
kernel32.GlobalMemoryStatusEx.restype = wintypes.BOOL

kernel32.GetSystemTimes.argtypes = [
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
kernel32.GetSystemTimes.restype = wintypes.BOOL

kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32FirstW.restype = wintypes.BOOL

kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
kernel32.Process32NextW.restype = wintypes.BOOL

kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.GetProcessTimes.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
    ctypes.POINTER(wintypes.FILETIME),
]
kernel32.GetProcessTimes.restype = wintypes.BOOL

psapi.GetProcessMemoryInfo.argtypes = [
    wintypes.HANDLE,
    ctypes.POINTER(PROCESS_MEMORY_COUNTERS_EX),
    wintypes.DWORD,
]
psapi.GetProcessMemoryInfo.restype = wintypes.BOOL


def filetime_to_int(ft):
    return (ft.dwHighDateTime << 32) + ft.dwLowDateTime


def filetime_to_iso(ft):
    ft_int = filetime_to_int(ft)
    if ft_int == 0:
        return None
    sec = (ft_int - 116444736000000000) / 10_000_000.0
    try:
        dt = datetime.fromtimestamp(sec, tz=timezone.utc)
        return dt.isoformat()
    except Exception:
        return None


def get_global_memory():
    mem = MEMORYSTATUSEX()
    mem.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    if not kernel32.GlobalMemoryStatusEx(ctypes.byref(mem)):
        return None
    tot = mem.ullTotalPhys / (1024.0 * 1024.0)
    avail = mem.ullAvailPhys / (1024.0 * 1024.0)
    used = tot - avail
    return {
        "total_phys_mib": round(tot, 2),
        "avail_phys_mib": round(avail, 2),
        "used_phys_mib": round(used, 2),
    }


def get_system_cpu_times():
    idle = wintypes.FILETIME()
    kernel = wintypes.FILETIME()
    user = wintypes.FILETIME()
    if not kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    return {
        "idle": filetime_to_int(idle),
        "kernel": filetime_to_int(kernel),
        "user": filetime_to_int(user),
    }


def query_process_metrics(pid):
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h or h == INVALID_HANDLE_VALUE:
        return None
    try:
        pmc = PROCESS_MEMORY_COUNTERS_EX()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS_EX)
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return None
        ws_mib = pmc.WorkingSetSize / (1024.0 * 1024.0)
        priv_mib = pmc.PrivateUsage / (1024.0 * 1024.0)

        creation = wintypes.FILETIME()
        exit_t = wintypes.FILETIME()
        kernel_t = wintypes.FILETIME()
        user_t = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            h,
            ctypes.byref(creation),
            ctypes.byref(exit_t),
            ctypes.byref(kernel_t),
            ctypes.byref(user_t),
        ):
            return None

        creation_raw = filetime_to_int(creation)
        kernel_sec = filetime_to_int(kernel_t) / 10_000_000.0
        user_sec = filetime_to_int(user_t) / 10_000_000.0
        total_cpu_sec = kernel_sec + user_sec

        return {
            "pid": pid,
            "creation_raw": creation_raw,
            "creation_utc": filetime_to_iso(creation),
            "working_set_mib": round(ws_mib, 2),
            "private_bytes_mib": round(priv_mib, 2),
            "total_cpu_s": total_cpu_sec,
        }
    finally:
        kernel32.CloseHandle(h)


def snapshot_process_tree():
    """Enumerates Windows process tree with strict 64-bit HANDLE and error checking.

    Returns dict mapping pid -> {ppid, name} on success.
    Returns None if snapshot failed (never returns {} to fake zero usage).
    """
    hSnap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if hSnap == INVALID_HANDLE_VALUE or hSnap == 0 or not hSnap:
        return None
    pe = PROCESSENTRY32W()
    pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    procs = {}
    try:
        if kernel32.Process32FirstW(hSnap, ctypes.byref(pe)):
            while True:
                procs[pe.th32ProcessID] = {
                    "ppid": pe.th32ParentProcessID,
                    "name": pe.szExeFile,
                }
                if not kernel32.Process32NextW(hSnap, ctypes.byref(pe)):
                    break
        else:
            return None
    finally:
        kernel32.CloseHandle(hSnap)
    return procs


class InstanceIdentity:
    def __init__(self, pid, role, creation_raw, creation_utc, name):
        self.pid = pid
        self.role = role
        self.creation_raw = creation_raw
        self.creation_utc = creation_utc
        self.name = name
        self.active = True


def resolve_authoritative_pids(out_dir):
    """Resolves authoritative instance PIDs from session and execution metadata."""
    out = Path(out_dir).resolve()
    res = {"host_pid": None, "worker_pid": None, "emulator_pid": None}

    # 1. Host PID
    proc_file = out / "execution-process.json"
    if proc_file.exists():
        try:
            data = json.loads(proc_file.read_text(encoding="utf-8"))
            pid = data.get("host_pid")
            if isinstance(pid, int) and pid > 0:
                res["host_pid"] = pid
        except Exception:
            pass
    if not res["host_pid"]:
        pid_file = out / "host.pid"
        if pid_file.exists():
            try:
                txt = pid_file.read_text(encoding="utf-8").strip()
                if txt.isdigit():
                    res["host_pid"] = int(txt)
            except Exception:
                pass

    # 2. Worker PID from session/worker.json or session/worker-claimed.json
    worker_file = out / "session/worker.json"
    if worker_file.exists():
        try:
            data = json.loads(worker_file.read_text(encoding="utf-8"))
            pid = data.get("pid")
            if isinstance(pid, int) and pid > 0:
                res["worker_pid"] = pid
        except Exception:
            pass
    if not res["worker_pid"]:
        claimed_file = out / "session/worker-claimed.json"
        if claimed_file.exists():
            try:
                data = json.loads(claimed_file.read_text(encoding="utf-8"))
                pid = data.get("pid")
                if isinstance(pid, int) and pid > 0:
                    res["worker_pid"] = pid
            except Exception:
                pass
    if not res["worker_pid"]:
        w_pid_file = out / "worker.pid"
        if w_pid_file.exists():
            try:
                txt = w_pid_file.read_text(encoding="utf-8").strip()
                if txt.isdigit():
                    res["worker_pid"] = int(txt)
            except Exception:
                pass

    # 3. Emulator PID from current-session.json -> run_dir -> state-start.json
    curr_session = out / "session/current-session.json"
    if curr_session.exists():
        try:
            cs_data = json.loads(curr_session.read_text(encoding="utf-8"))
            rd = cs_data.get("run_dir")
            if rd:
                st_file = Path(rd) / "state-start.json"
                if st_file.exists():
                    st_data = json.loads(st_file.read_text(encoding="utf-8"))
                    pid = st_data.get("pid")
                    if isinstance(pid, int) and pid > 0:
                        res["emulator_pid"] = pid
            if not res["emulator_pid"]:
                sf = cs_data.get("state_file")
                if sf and Path(sf).exists():
                    sf_data = json.loads(Path(sf).read_text(encoding="utf-8"))
                    pid = sf_data.get("pid")
                    if isinstance(pid, int) and pid > 0:
                        res["emulator_pid"] = pid
        except Exception:
            pass

    # Glob fallback for state-start.json or state-final.json under session
    if not res["emulator_pid"]:
        for pattern in ("session/**/state-start.json", "session/**/state-final.json", "session/**/state.json"):
            matches = sorted(out.glob(pattern))
            if matches:
                try:
                    st_data = json.loads(matches[0].read_text(encoding="utf-8"))
                    pid = st_data.get("pid")
                    if isinstance(pid, int) and pid > 0:
                        res["emulator_pid"] = pid
                        break
                except Exception:
                    pass

    return res


class Sampler:
    def __init__(self, out_dir, interval=1.0, host_pid=None):
        self.out_dir = Path(out_dir).resolve()
        self.interval = float(interval)
        self.host_pid = int(host_pid) if host_pid else None
        self.observer_pid = os.getpid()
        self.logical_cpus = os.cpu_count() or 1

        self.samples_jsonl = self.out_dir / "samples.jsonl"
        self.samples_csv = self.out_dir / "samples.csv"
        self.stage_file = self.out_dir / "current_stage.json"
        self.stop_signal = self.out_dir / "stop.signal"
        self.env_file = self.out_dir / "environment.json"

        self.prev_sys_cpu = None
        self.prev_proc_cpu = {}  # Key: (pid, creation_raw) -> total_cpu_s
        self.prev_mono = None
        self.sample_seq = 0

        # Persistent identity tracking and permanent anti-PID-reuse tombstoning
        self.bound_instances = {}  # pid -> InstanceIdentity
        self.tombstoned_identities = set()  # set of (pid, creation_raw)

    def get_current_stage(self):
        if self.stage_file.exists():
            try:
                data = json.loads(self.stage_file.read_text(encoding="utf-8"))
                return data.get("stage", "unknown")
            except Exception:
                pass
        return "unknown"

    def record_environment(self, avd_config_path=None):
        env = {
            "recorded_utc": datetime.now(timezone.utc).isoformat(),
            "observer_pid": self.observer_pid,
            "logical_cpus": self.logical_cpus,
            "python_executable": sys.executable,
            "python_version": sys.version,
            "platform": sys.platform,
        }
        mem = get_global_memory()
        if mem:
            env["total_physical_memory_mib"] = mem["total_phys_mib"]

        if avd_config_path and Path(avd_config_path).exists():
            try:
                txt = Path(avd_config_path).read_text(encoding="utf-8", errors="ignore")
                avd_props = {}
                for line in txt.splitlines():
                    if "=" in line:
                        k, v = line.split("=", 1)
                        k, v = k.strip(), v.strip()
                        if k in ("hw.ramSize", "hw.cpu.ncore", "hw.cpu.arch", "vm.heapSize"):
                            avd_props[k] = v
                env["avd_guest_config"] = {
                    "path": str(avd_config_path),
                    "properties": avd_props,
                    "note": "Read-only guest RAM setting; strictly separate from Windows host physical memory.",
                }
            except Exception as e:
                env["avd_guest_config_error"] = str(e)

        self.env_file.write_text(json.dumps(env, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def resolve_external_hints(self):
        """Reads artifact-based instance identity hints from execution files."""
        hints = {}
        pids = resolve_authoritative_pids(self.out_dir)

        # 1. Host PID
        if not self.host_pid or self.host_pid <= 0:
            if pids["host_pid"]:
                self.host_pid = pids["host_pid"]
        if self.host_pid and self.host_pid > 0:
            hints[self.host_pid] = "host"

        # 2. Worker PID
        if pids["worker_pid"]:
            hints[pids["worker_pid"]] = "worker"

        # 3. Emulator PID
        if pids["emulator_pid"]:
            hints[pids["emulator_pid"]] = "emulator"

        return hints

    def update_instance_identities(self, all_procs):
        """Discovers, binds, and maintains persistent instance process identities.

        Ensures:
        - Host exit does NOT cause Worker or Emulator to be dropped.
        - Unrelated system processes or qemu instances are never adopted.
        - Once an instance process exits, (pid, creation_raw) is tombstoned.
        - Short-lived CLI commands spawned by Host are labeled 'host_cli', NOT 'worker'.
        """
        if all_procs is None:
            return

        hints = self.resolve_external_hints()

        # 1. Bind explicit hints (Host, Worker, Emulator)
        for h_pid, role in hints.items():
            if h_pid in all_procs and h_pid not in self.bound_instances:
                p_metrics = query_process_metrics(h_pid)
                if p_metrics:
                    key = (h_pid, p_metrics["creation_raw"])
                    if key not in self.tombstoned_identities:
                        self.bound_instances[h_pid] = InstanceIdentity(
                            h_pid,
                            role,
                            p_metrics["creation_raw"],
                            p_metrics["creation_utc"],
                            all_procs[h_pid]["name"],
                        )

        # 2. Hierarchy discovery: find children of any CURRENTLY ACTIVE bound instances
        # Build children index from all_procs
        children_by_ppid = defaultdict(list)
        for p, info in all_procs.items():
            children_by_ppid[info["ppid"]].append(p)

        # Breadth-first walk starting from active bound instances
        active_pids = [pid for pid, inst in self.bound_instances.items() if inst.active]
        visited = set(active_pids)
        queue = list(active_pids)

        while queue:
            curr_pid = queue.pop(0)
            curr_role = self.bound_instances[curr_pid].role
            for child_pid in children_by_ppid.get(curr_pid, []):
                if child_pid in visited:
                    continue
                visited.add(child_pid)
                child_name = all_procs.get(child_pid, {}).get("name", "").lower()

                # Determine role based on parent and binary name
                if "emulator" in child_name:
                    c_role = "emulator"
                elif "qemu" in child_name:
                    c_role = "qemu"
                elif "adb" in child_name:
                    c_role = "adb"
                elif "crashpad" in child_name:
                    c_role = "crashpad"
                elif "conhost" in child_name:
                    c_role = "conhost"
                elif "python" in child_name:
                    if curr_role == "host":
                        c_role = "host_cli"
                    elif curr_role == "worker":
                        c_role = "worker_child_python"
                    else:
                        c_role = f"child_python_{curr_role}"
                else:
                    c_role = f"child_{child_name}"

                # Check if already bound
                if child_pid not in self.bound_instances:
                    p_metrics = query_process_metrics(child_pid)
                    if p_metrics:
                        key = (child_pid, p_metrics["creation_raw"])
                        if key not in self.tombstoned_identities:
                            self.bound_instances[child_pid] = InstanceIdentity(
                                child_pid,
                                c_role,
                                p_metrics["creation_raw"],
                                p_metrics["creation_utc"],
                                all_procs[child_pid]["name"],
                            )
                queue.append(child_pid)

    def tick(self):
        self.sample_seq += 1
        now_mono = time.monotonic()
        now_utc = datetime.now(timezone.utc).isoformat()
        stage = self.get_current_stage()

        delta_mono = (now_mono - self.prev_mono) if self.prev_mono else self.interval
        self.prev_mono = now_mono

        # 1. System Memory
        sys_mem = get_global_memory()
        if not sys_mem:
            return None

        # 2. System CPU
        sys_cpu_times = get_system_cpu_times()
        sys_cpu_pct = 0.0
        if sys_cpu_times and self.prev_sys_cpu:
            di = sys_cpu_times["idle"] - self.prev_sys_cpu["idle"]
            dk = sys_cpu_times["kernel"] - self.prev_sys_cpu["kernel"]
            du = sys_cpu_times["user"] - self.prev_sys_cpu["user"]
            tot = dk + du
            busy = tot - di
            sys_cpu_pct = round((busy / tot * 100.0), 2) if tot > 0 else 0.0
        self.prev_sys_cpu = sys_cpu_times

        # 3. Process Tree & Target Instance Tracking
        all_procs = snapshot_process_tree()

        if all_procs is None:
            # Snapshot failed: record explicit defect, DO NOT fake 0 processes
            instance_summary = {
                "status": "snapshot_failed",
                "process_count": None,
                "total_working_set_mib": None,
                "total_private_bytes_mib": None,
                "total_cpu_pct_core": None,
                "total_cpu_pct_system": None,
                "processes": [],
            }
        else:
            self.update_instance_identities(all_procs)

            proc_records = []
            tot_inst_ws = 0.0
            tot_inst_priv = 0.0
            tot_inst_cpu_core = 0.0

            # Verify and query each bound instance
            for pid, inst in list(self.bound_instances.items()):
                if not inst.active:
                    continue

                if pid not in all_procs:
                    # Process exited! Mark inactive and tombstone
                    inst.active = False
                    self.tombstoned_identities.add((pid, inst.creation_raw))
                    continue

                info = query_process_metrics(pid)
                if not info or info["creation_raw"] != inst.creation_raw:
                    # Process terminated or PID was reused!
                    inst.active = False
                    self.tombstoned_identities.add((pid, inst.creation_raw))
                    continue

                # Process is alive and identity confirmed
                cpu_key = (pid, inst.creation_raw)
                prev_cpu_s = self.prev_proc_cpu.get(cpu_key)
                total_cpu_s = info["total_cpu_s"]
                self.prev_proc_cpu[cpu_key] = total_cpu_s

                cpu_core_pct = 0.0
                if prev_cpu_s is not None and delta_mono > 0:
                    delta_cpu = max(0.0, total_cpu_s - prev_cpu_s)
                    cpu_core_pct = round((delta_cpu / delta_mono) * 100.0, 2)
                cpu_sys_pct = round(cpu_core_pct / self.logical_cpus, 2)

                info["role"] = inst.role
                info["exe_name"] = inst.name
                info["ppid"] = all_procs.get(pid, {}).get("ppid")
                info["cpu_pct_core"] = cpu_core_pct
                info["cpu_pct_system"] = cpu_sys_pct
                proc_records.append(info)

                tot_inst_ws += info["working_set_mib"]
                tot_inst_priv += info["private_bytes_mib"]
                tot_inst_cpu_core += cpu_core_pct

            instance_summary = {
                "status": "ok",
                "process_count": len(proc_records),
                "total_working_set_mib": round(tot_inst_ws, 2),
                "total_private_bytes_mib": round(tot_inst_priv, 2),
                "total_cpu_pct_core": round(tot_inst_cpu_core, 2),
                "total_cpu_pct_system": round(tot_inst_cpu_core / self.logical_cpus, 2),
                "processes": proc_records,
            }

        # 4. Observer Process Overhead (Separately Recorded)
        obs_info = query_process_metrics(self.observer_pid)
        obs_cpu_core = 0.0
        if obs_info:
            prev_obs = getattr(self, "prev_obs_cpu", None)
            if prev_obs is not None and delta_mono > 0:
                d_obs_cpu = max(0.0, obs_info["total_cpu_s"] - prev_obs)
                obs_cpu_core = round((d_obs_cpu / delta_mono) * 100.0, 2)
            self.prev_obs_cpu = obs_info["total_cpu_s"]
            obs_record = {
                "pid": self.observer_pid,
                "working_set_mib": obs_info["working_set_mib"],
                "private_bytes_mib": obs_info["private_bytes_mib"],
                "cpu_pct_core": obs_cpu_core,
                "cpu_pct_system": round(obs_cpu_core / self.logical_cpus, 2),
            }
        else:
            obs_record = None

        sample = {
            "seq": self.sample_seq,
            "monotonic_s": round(now_mono, 3),
            "utc_iso": now_utc,
            "stage": stage,
            "global_memory": {
                "total_phys_mib": sys_mem["total_phys_mib"],
                "avail_phys_mib": sys_mem["avail_phys_mib"],
                "used_phys_mib": sys_mem["used_phys_mib"],
                "delta_used_phys_mib": None,  # Computed objectively during analysis from Stage A baseline
            },
            "global_cpu": {
                "system_cpu_pct": sys_cpu_pct,
                "logical_cpus": self.logical_cpus,
            },
            "instance": instance_summary,
            "observer": obs_record,
        }

        # Append to JSONL
        with open(self.samples_jsonl, "a", encoding="utf-8") as f:
            f.write(json.dumps(sample, ensure_ascii=False) + "\n")

        # Append to CSV
        self.write_csv_row(sample)
        return sample

    def write_csv_row(self, s):
        file_exists = self.samples_csv.exists()
        with open(self.samples_csv, "a", encoding="utf-8") as f:
            if not file_exists:
                header = [
                    "seq",
                    "monotonic_s",
                    "utc_iso",
                    "stage",
                    "total_phys_mib",
                    "avail_phys_mib",
                    "used_phys_mib",
                    "system_cpu_pct",
                    "instance_proc_count",
                    "instance_ws_mib",
                    "instance_priv_mib",
                    "instance_cpu_core_pct",
                    "instance_cpu_sys_pct",
                    "obs_ws_mib",
                    "obs_priv_mib",
                    "obs_cpu_core_pct",
                ]
                f.write(",".join(header) + "\n")

            obs = s["observer"] or {}
            inst = s["instance"]
            gm = s["global_memory"]
            p_cnt = str(inst["process_count"]) if inst["process_count"] is not None else "ERR"
            p_ws = str(inst["total_working_set_mib"]) if inst["total_working_set_mib"] is not None else "ERR"
            p_priv = str(inst["total_private_bytes_mib"]) if inst["total_private_bytes_mib"] is not None else "ERR"
            p_cpu_c = str(inst["total_cpu_pct_core"]) if inst["total_cpu_pct_core"] is not None else "ERR"
            p_cpu_s = str(inst["total_cpu_pct_system"]) if inst["total_cpu_pct_system"] is not None else "ERR"

            row = [
                str(s["seq"]),
                str(s["monotonic_s"]),
                s["utc_iso"],
                s["stage"],
                str(gm["total_phys_mib"]),
                str(gm["avail_phys_mib"]),
                str(gm["used_phys_mib"]),
                str(s["global_cpu"]["system_cpu_pct"]),
                p_cnt,
                p_ws,
                p_priv,
                p_cpu_c,
                p_cpu_s,
                str(obs.get("working_set_mib", 0.0)),
                str(obs.get("private_bytes_mib", 0.0)),
                str(obs.get("cpu_pct_core", 0.0)),
            ]
            f.write(",".join(row) + "\n")

    def run_loop(self):
        print(f"[{datetime.now(timezone.utc).isoformat()}] Sampler started (PID={self.observer_pid}). Interval={self.interval}s")
        sys.stdout.flush()
        next_tick = time.monotonic()
        try:
            while not self.stop_signal.exists():
                self.tick()
                next_tick += self.interval
                sleep_s = max(0.0, next_tick - time.monotonic())
                time.sleep(sleep_s)
        except KeyboardInterrupt:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Sampler interrupted by keyboard.")
        finally:
            print(f"[{datetime.now(timezone.utc).isoformat()}] Sampler loop exited. Samples recorded: {self.sample_seq}")
            sys.stdout.flush()
            if self.stop_signal.exists():
                try:
                    self.stop_signal.unlink()
                except Exception:
                    pass


def set_stage(out_dir, stage_name):
    p = Path(out_dir).resolve() / "current_stage.json"
    obj = {
        "stage": stage_name,
        "updated_utc": datetime.now(timezone.utc).isoformat(),
        "updated_mono": round(time.monotonic(), 3),
    }
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, indent=2), encoding="utf-8")
    tmp.replace(p)
    print(f"STAGE_SET: {stage_name}")
    sys.stdout.flush()


def stop_sampler(out_dir):
    p = Path(out_dir).resolve() / "stop.signal"
    p.write_text("stop\n", encoding="utf-8")
    print(f"STOP_SIGNAL_WRITTEN: {p}")
    sys.stdout.flush()


def analyze_samples(out_dir, out_filename="resource-summary.json"):
    out = Path(out_dir).resolve()
    jsonl_path = out / "samples.jsonl"
    if not jsonl_path.exists():
        print(f"No samples.jsonl found at {jsonl_path}")
        return None

    samples = []
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))

    if not samples:
        print("samples.jsonl is empty.")
        return None

    # Group by stage
    by_stage = defaultdict(list)
    for s in samples:
        by_stage[s["stage"]].append(s)

    # Establish baseline strictly from Stage A
    baseline_samples = by_stage.get("A_baseline", [])
    if len(baseline_samples) >= 3:
        baseline_used_phys = statistics.median([s["global_memory"]["used_phys_mib"] for s in baseline_samples])
        baseline_valid = True
        baseline_error = None
    else:
        baseline_used_phys = None
        baseline_valid = False
        baseline_error = f"Stage A baseline samples insufficient ({len(baseline_samples)} < 3); host delta cannot be computed."

    # 1. Automatic defect detection: Time gaps (> 3.0s between consecutive samples)
    gaps = []
    for i in range(len(samples) - 1):
        s_curr = samples[i]
        s_next = samples[i + 1]
        d_mono = s_next["monotonic_s"] - s_curr["monotonic_s"]
        if d_mono > 3.0:
            gaps.append({
                "gap_index": len(gaps) + 1,
                "sample_index_before": i + 1,
                "sample_index_after": i + 2,
                "prev_seq": s_curr.get("seq"),
                "next_seq": s_next.get("seq"),
                "prev_utc": s_curr.get("utc_iso"),
                "next_utc": s_next.get("utc_iso"),
                "gap_duration_s": round(d_mono, 3),
                "stage_before": s_curr.get("stage"),
                "stage_after": s_next.get("stage"),
            })

    # 2. Automatic defect detection: Observer process lifecycle & Sequence resets
    obs_segments = []
    seq_resets = []
    curr_obs = None
    curr_range = [None, None]
    curr_start_utc = None
    curr_end_utc = None

    for idx, s in enumerate(samples):
        obs_info = s.get("observer") or {}
        obs_pid = obs_info.get("pid")
        if obs_pid != curr_obs:
            if curr_obs is not None:
                obs_segments.append({
                    "observer_pid": curr_obs,
                    "sample_range": [curr_range[0], curr_range[1]],
                    "sample_count": curr_range[1] - curr_range[0] + 1,
                    "start_utc": curr_start_utc,
                    "end_utc": curr_end_utc,
                })
            curr_obs = obs_pid
            curr_range = [idx + 1, idx + 1]
            curr_start_utc = s.get("utc_iso")
            curr_end_utc = s.get("utc_iso")
        else:
            curr_range[1] = idx + 1
            curr_end_utc = s.get("utc_iso")

        if idx > 0:
            prev_seq = samples[idx - 1].get("seq", 0)
            this_seq = s.get("seq", 0)
            if this_seq <= prev_seq:
                seq_resets.append({
                    "reset_index": len(seq_resets) + 1,
                    "sample_index": idx + 1,
                    "prev_seq": prev_seq,
                    "next_seq": this_seq,
                    "prev_utc": samples[idx - 1].get("utc_iso"),
                    "next_utc": s.get("utc_iso"),
                })

    if curr_obs is not None:
        obs_segments.append({
            "observer_pid": curr_obs,
            "sample_range": [curr_range[0], curr_range[1]],
            "sample_count": curr_range[1] - curr_range[0] + 1,
            "start_utc": curr_start_utc,
            "end_utc": curr_end_utc,
        })

    # 3. Automatic defect detection: Authoritative target PID coverage
    authoritative_pids = resolve_authoritative_pids(out)
    pid_sample_counts = {role: 0 for role in ("host", "worker", "emulator")}
    pid_stage_counts = {role: defaultdict(int) for role in ("host", "worker", "emulator")}

    target_pids_map = {}
    if authoritative_pids.get("host_pid"):
        target_pids_map[authoritative_pids["host_pid"]] = "host"
    if authoritative_pids.get("worker_pid"):
        target_pids_map[authoritative_pids["worker_pid"]] = "worker"
    if authoritative_pids.get("emulator_pid"):
        target_pids_map[authoritative_pids["emulator_pid"]] = "emulator"

    for s in samples:
        stage = s.get("stage", "unknown")
        seen_roles_in_sample = set()
        for p in s.get("instance", {}).get("processes", []):
            p_pid = p.get("pid")
            if p_pid in target_pids_map:
                r = target_pids_map[p_pid]
                if r not in seen_roles_in_sample:
                    seen_roles_in_sample.add(r)
                    pid_sample_counts[r] += 1
                    pid_stage_counts[r][stage] += 1

    critical_process_coverage = {}
    missing_critical_pids = []
    total_sample_count = len(samples)

    for role, pid_key in [("host", "host_pid"), ("worker", "worker_pid"), ("emulator", "emulator_pid")]:
        t_pid = authoritative_pids.get(pid_key)
        cnt = pid_sample_counts[role]
        pct = round((cnt / total_sample_count * 100.0), 2) if total_sample_count > 0 else 0.0
        critical_process_coverage[role] = {
            "pid": t_pid,
            "covered_samples": cnt,
            "coverage_pct": pct,
            "stage_distribution": dict(pid_stage_counts[role]),
        }
        if t_pid is not None:
            if cnt == 0:
                missing_critical_pids.append({"role": role, "pid": t_pid, "reason": "zero_samples_recorded"})
            elif "B_launch" in by_stage and role in ("worker", "emulator") and pid_stage_counts[role].get("B_launch", 0) == 0:
                missing_critical_pids.append({"role": role, "pid": t_pid, "reason": "missing_during_launch_stage"})

    # 4. Defect synthesis and Verdict determination
    defects_detected = []
    if gaps:
        defects_detected.append(f"unobserved_time_gaps_count_{len(gaps)}")
    if len(obs_segments) > 1:
        defects_detected.append(f"observer_restarted_segments_{len(obs_segments)}")
    if seq_resets:
        defects_detected.append(f"sequence_resets_count_{len(seq_resets)}")
    if missing_critical_pids:
        missing_roles = sorted(set(m["role"] for m in missing_critical_pids))
        defects_detected.append(f"missing_critical_processes_{'_'.join(missing_roles)}")
    if not baseline_valid:
        defects_detected.append("insufficient_stage_a_baseline")

    if defects_detected:
        measurement_status = "partial"
        review_verdict = "partial_measurement_process_coverage_and_sampling_gaps"
    else:
        measurement_status = "complete"
        review_verdict = "complete"

    def stats(values):
        valid = [v for v in values if v is not None]
        if not valid:
            return {"count": len(values), "min": None, "median": None, "max": None}
        return {
            "count": len(valid),
            "min": round(min(valid), 2),
            "median": round(statistics.median(valid), 2),
            "max": round(max(valid), 2),
        }

    summary = {
        "analyzed_utc": datetime.now(timezone.utc).isoformat(),
        "total_samples": len(samples),
        "total_duration_s": round(samples[-1]["monotonic_s"] - samples[0]["monotonic_s"], 2),
        "measurement_status": measurement_status,
        "review_verdict": review_verdict,
        "defects_detected": defects_detected,
        "baseline_valid": baseline_valid,
        "baseline_used_phys_mib": round(baseline_used_phys, 2) if baseline_used_phys is not None else None,
        "baseline_error": baseline_error,
        "gaps_detected": gaps,
        "observer_lifecycle": {
            "segments": obs_segments,
            "sequence_resets": seq_resets,
        },
        "critical_process_coverage": {
            "authoritative_pids": authoritative_pids,
            "coverage_by_role": critical_process_coverage,
            "missing_critical_pids": missing_critical_pids,
        },
        "methodological_notes": {
            "observed_host_memory_delta": "Whole-machine physical memory delta represents observed host memory change relative to Stage A baseline median. It reflects total host-level consumption (including OS, drivers, runtime pools, and VM manager), not solely sandbox processes. Cooldown deltas reflect observed post-exit state and do not imply specific OS cache/page trim mechanisms or absolute leak freedom.",
            "private_bytes_semantics": "Private Bytes represents commit charge / pagefile allocation, not dedicated physical RAM.",
            "working_set_semantics": "Working Set represents resident physical memory actively mapped in RAM.",
            "turnaround_admission_and_caps": "The 90-second turnaround limit is a per-action set-text call cap; the 115-second threshold is the operational balance admission gate for entering new action steps.",
            "coverage_criteria": "Continuous sampling across all active stages is required for complete measurement status. Isolated or late process detection does not constitute complete launch coverage; unobserved gaps or missing critical processes retain partial status.",
        },
        "stages": {},
    }

    for stage_name, stage_list in by_stage.items():
        dur = round(stage_list[-1]["monotonic_s"] - stage_list[0]["monotonic_s"], 2) if len(stage_list) > 1 else 0.0
        used_vals = [s["global_memory"]["used_phys_mib"] for s in stage_list]
        delta_vals = (
            [round(u - baseline_used_phys, 2) for u in used_vals]
            if baseline_valid
            else [None for _ in used_vals]
        )
        sys_cpu_vals = [s["global_cpu"]["system_cpu_pct"] for s in stage_list]
        inst_ws_vals = [s["instance"]["total_working_set_mib"] for s in stage_list]
        inst_priv_vals = [s["instance"]["total_private_bytes_mib"] for s in stage_list]
        inst_cpu_vals = [s["instance"]["total_cpu_pct_core"] for s in stage_list]

        # Process role maximums
        role_max_priv = defaultdict(float)
        role_max_ws = defaultdict(float)
        for s in stage_list:
            for p in s["instance"].get("processes", []):
                role = p.get("role", "other")
                role_max_priv[role] = max(role_max_priv[role], p.get("private_bytes_mib", 0.0))
                role_max_ws[role] = max(role_max_ws[role], p.get("working_set_mib", 0.0))

        summary["stages"][stage_name] = {
            "sample_count": len(stage_list),
            "duration_s": dur,
            "used_physical_mib": stats(used_vals),
            "observed_host_delta_mib": stats(delta_vals),
            "system_cpu_pct": stats(sys_cpu_vals),
            "instance_private_bytes_mib": stats(inst_priv_vals),
            "instance_working_set_mib": stats(inst_ws_vals),
            "instance_cpu_pct_core": stats(inst_cpu_vals),
            "process_role_peak_private_bytes_mib": dict(role_max_priv),
            "process_role_peak_working_set_mib": dict(role_max_ws),
        }

    # Resource release analysis (comparing Stage E to Stage A)
    cooldown_samples = by_stage.get("E_stopped_cooldown", [])
    if cooldown_samples:
        final_used = statistics.median([s["global_memory"]["used_phys_mib"] for s in cooldown_samples])
        summary["resource_release_assessment"] = {
            "stage_e_median_used_phys_mib": round(final_used, 2),
            "delta_relative_to_stage_a_mib": (
                round(final_used - baseline_used_phys, 2) if baseline_valid else None
            ),
            "instance_processes_in_stage_e": sum(
                (s["instance"]["process_count"] or 0) for s in cooldown_samples
            ),
            "note": "Evaluates whether host physical memory returns to near baseline and instance processes are completely zero.",
        }

    summary_file = out / out_filename
    summary_file.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Summary generated at: {summary_file}")
    return summary


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  python sampler.py run <out_dir> [--interval 1.0] [--host-pid <pid>] [--avd-config <path>]")
        print("  python sampler.py mark <out_dir> <stage_name>")
        print("  python sampler.py stop <out_dir>")
        print("  python sampler.py analyze <out_dir> [--out <output_json>]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "run":
        out_d = sys.argv[2]
        interval = 1.0
        h_pid = None
        avd_cfg = None
        idx = 3
        while idx < len(sys.argv):
            if sys.argv[idx] == "--interval" and idx + 1 < len(sys.argv):
                interval = float(sys.argv[idx + 1])
                idx += 2
            elif sys.argv[idx] == "--host-pid" and idx + 1 < len(sys.argv):
                h_pid = int(sys.argv[idx + 1])
                idx += 2
            elif sys.argv[idx] == "--avd-config" and idx + 1 < len(sys.argv):
                avd_cfg = sys.argv[idx + 1]
                idx += 2
            else:
                idx += 1

        sampler = Sampler(out_d, interval=interval, host_pid=h_pid)
        sampler.record_environment(avd_config_path=avd_cfg)
        sampler.run_loop()

    elif cmd == "mark":
        set_stage(sys.argv[2], sys.argv[3])

    elif cmd == "stop":
        stop_sampler(sys.argv[2])

    elif cmd == "analyze":
        out_d = sys.argv[2]
        out_name = "resource-summary.json"
        if len(sys.argv) > 3 and sys.argv[3] == "--out" and len(sys.argv) > 4:
            out_name = sys.argv[4]
        analyze_samples(out_d, out_filename=out_name)

    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
