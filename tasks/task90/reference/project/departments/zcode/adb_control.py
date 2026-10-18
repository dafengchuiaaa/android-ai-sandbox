#!/usr/bin/env python3
"""adb_control.py -- minimal ADB control CLI for the sandboxed Android emulator.

Scope (dispatch 03-ZCode): status / screenshot / hierarchy / tap / text against
one explicitly named emulator serial. No auto device selection, no physical
phones, no HTTP service, no host input injection.

stdout contract: exactly one JSON line, nothing else:

    {"ok": true, "action": "screenshot", "serial": "emulator-5554",
     "artifact": "C:\\abs\\path\\screenshot-....png", "error": null}

On failure ok=false, "error" is a short stable code, diagnostics go to stderr,
and the process exits non-zero.

Error codes:
    usage                 invalid or missing CLI arguments
    serial_missing        --serial not provided or empty
    serial_invalid        not an emulator serial with an even port
    adb_not_found         --adb path does not exist / is not executable
    adb_timeout           an ADB call exceeded its timeout
    adb_error             an ADB call returned a non-zero exit code
    device_not_connected  serial absent from `adb devices`
    device_offline        serial present but state is not "device"
    not_booted            sys.boot_completed != 1 (status)
    invalid_coords        tap coordinates not non-negative ints inside screen
    unsupported_text      text outside single ASCII [A-Za-z0-9]+ word
    output_dir_missing    --output-dir not provided or empty
    output_dir_rejected   --output-dir escapes the approved artifact roots
                           (departments/zcode or departments/workbuddy/integration),
                           including same-name prefix sibling directories and
                           symlink/junction escapes
    output_failed         local artifact directory/file could not be produced

Output boundary (WB-02 patch, review item 4): --output-dir is resolved with
realpath (symlinks/junctions resolved) and must sit inside an approved root
with a path-separator boundary. Validation runs BEFORE any device command,
for every action including status/tap/text.

Injection note: local subprocess calls use argument arrays with shell=False.
Remote arguments are whitelisted (serial matches ^emulator-<even port>$, text
is [A-Za-z0-9]+, coordinates are integers, remote paths are built from
uuid hex). Because `adb shell` still goes through the device shell, we
constrain every remote argument instead of claiming shell=False alone is
sufficient.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime

SERIAL_RE = re.compile(r"^emulator-(\d{1,5})$")
COORD_RE = re.compile(r"^\d+$")
TEXT_RE = re.compile(r"^[A-Za-z0-9]+$")
SIZE_RE = re.compile(r"(\d+)x(\d+)")

MAX_TEXT_LEN = 200
SHELL_TIMEOUT = 30.0
PULL_TIMEOUT = 60.0
REMOTE_PREFIX = "/sdcard/Download/ai-sandbox"

# Approved local artifact roots (WB-02 patch). This file lives at
# <project>/departments/zcode/adb_control.py, so the project root is two levels up.
PROJECT_ROOT = os.path.realpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
APPROVED_OUTPUT_ROOTS = (
    os.path.realpath(os.path.join(PROJECT_ROOT, "departments", "zcode")),
    os.path.realpath(os.path.join(PROJECT_ROOT, "departments", "workbuddy", "integration")),
    os.path.realpath(os.path.join(PROJECT_ROOT, "departments", "antigravity", "runtime")),
)


class AdbTimeout(Exception):
    """An ADB call exceeded its allowed wall-clock timeout."""


class AdbNotFound(Exception):
    """The adb executable could not be launched."""


def log(message):
    """Diagnostics go to stderr; stdout carries only the result JSON."""
    sys.stderr.write("[adb_control] %s\n" % message)


class SubprocessRunner:
    """Default runner: argument-array subprocess, shell=False."""

    def run(self, cmd, timeout):
        try:
            proc = subprocess.run(cmd, timeout=timeout, capture_output=True)
        except subprocess.TimeoutExpired as exc:
            raise AdbTimeout("timed out after %.0fs: %s" % (timeout, cmd[0])) from exc
        except FileNotFoundError as exc:
            raise AdbNotFound("cannot execute: %s" % cmd[0]) from exc
        return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def _decode(data):
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return str(data)


class AdbControl:
    """Binds every ADB invocation to the configured serial via `-s`."""

    def __init__(self, adb_path, serial, runner=None, timeout=SHELL_TIMEOUT):
        self.adb_path = adb_path
        self.serial = serial
        self.timeout = timeout
        self.runner = runner if runner is not None else SubprocessRunner()

    def run(self, args, timeout=None):
        cmd = [self.adb_path, "-s", self.serial] + list(args)
        return self.runner.run(cmd, timeout if timeout is not None else self.timeout)

    def device_state(self):
        """Return ("connected"|"offline"|None, state_string)."""
        rc, out, err = self.run(["devices"])
        if rc != 0:
            raise AdbError("adb devices failed: %s" % (err.strip() or out.strip()))
        state = None
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == self.serial:
                state = parts[1]
                break
        if state is None:
            return None, ""
        return ("connected" if state == "device" else "offline"), state

    def boot_completed(self):
        rc, out, err = self.run(["shell", "getprop", "sys.boot_completed"])
        if rc != 0:
            raise AdbError("getprop sys.boot_completed failed: %s" % (err.strip() or out.strip()))
        return out.strip()

    def screen_size(self):
        rc, out, err = self.run(["shell", "wm", "size"])
        if rc != 0:
            raise AdbError("wm size failed: %s" % (err.strip() or out.strip()))
        physical = override = None
        for line in out.splitlines():
            if line.startswith("Physical size"):
                m = SIZE_RE.search(line)
                if m:
                    physical = (int(m.group(1)), int(m.group(2)))
            elif line.startswith("Override size"):
                m = SIZE_RE.search(line)
                if m:
                    override = (int(m.group(1)), int(m.group(2)))
        size = override or physical
        if size is None:
            raise AdbError("cannot parse wm size output: %r" % out.strip())
        return size

    def screencap(self, remote_path):
        rc, out, err = self.run(["shell", "screencap", "-p", remote_path])
        if rc != 0:
            raise AdbError("screencap failed: %s" % (err.strip() or out.strip()))

    def ui_dump(self, remote_path):
        rc, out, err = self.run(["shell", "uiautomator", "dump", remote_path])
        if rc != 0:
            raise AdbError("uiautomator dump failed: %s" % (err.strip() or out.strip()))

    def input_tap(self, x, y):
        rc, out, err = self.run(["shell", "input", "tap", str(x), str(y)])
        if rc != 0:
            raise AdbError("input tap failed: %s" % (err.strip() or out.strip()))

    def input_text(self, value):
        rc, out, err = self.run(["shell", "input", "text", value])
        if rc != 0:
            raise AdbError("input text failed: %s" % (err.strip() or out.strip()))

    def pull(self, remote_path, local_path):
        rc, out, err = self.run(["pull", remote_path, local_path], timeout=PULL_TIMEOUT)
        if rc != 0:
            raise AdbError("pull failed: %s" % (err.strip() or out.strip()))

    def remove_remote(self, remote_path):
        """Remove exactly the temp file this tool created; never fatal."""
        try:
            rc, out, err = self.run(["shell", "rm", remote_path])
            if rc != 0:
                log("cleanup of %s failed: %s" % (remote_path, (err.strip() or out.strip())))
        except (AdbTimeout, AdbNotFound) as exc:
            log("cleanup of %s interrupted: %s" % (remote_path, exc))


class AdbError(Exception):
    """An ADB call returned a non-zero exit code."""


def check_serial(serial):
    """Return an error code string, or None when the serial is acceptable."""
    if not serial:
        return "serial_missing"
    m = SERIAL_RE.match(serial)
    if not m:
        return "serial_invalid"
    port = int(m.group(1))
    if port == 0 or port > 65535 or port % 2 != 0:
        return "serial_invalid"
    return None


def check_adb_path(adb_path):
    if os.path.dirname(adb_path):
        return os.path.isfile(adb_path)
    return shutil.which(adb_path) is not None


def check_output_dir(output_dir):
    """Return an error code string, or None when the output dir is approved.

    The path is fully resolved (symlinks and junctions are followed to their
    real target) and must sit inside an approved root with a separator
    boundary, so same-name prefix siblings (e.g. departments/zcode-evil) and
    link escapes are both rejected. Runs before any device command.
    """
    if not output_dir or not output_dir.strip():
        return "output_dir_missing"
    try:
        resolved = os.path.realpath(output_dir)
    except OSError:
        return "output_dir_rejected"
    # On Windows a symlink created without target_is_directory is a "file"
    # reparse point; realpath may leave it unresolved even when it points at a
    # directory. Resolve the final component explicitly so the escape is caught.
    if os.path.islink(output_dir):
        try:
            target = os.readlink(output_dir)
        except OSError:
            return "output_dir_rejected"
        if not os.path.isabs(target):
            target = os.path.join(os.path.dirname(os.path.abspath(output_dir)), target)
        try:
            resolved = os.path.realpath(target)
        except OSError:
            return "output_dir_rejected"
    for root in APPROVED_OUTPUT_ROOTS:
        if resolved == root or resolved.startswith(root + os.sep):
            return None
    return "output_dir_rejected"


def make_local_name(kind, ext):
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return "%s-%s-%s.%s" % (kind, stamp, uuid.uuid4().hex[:8], ext)


def emit(action, serial, ok, artifact=None, error=None):
    payload = {
        "ok": ok,
        "action": action,
        "serial": serial,
        "artifact": artifact,
        "error": error,
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if ok else 1


def ensure_output_dir(output_dir):
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise OutputError("cannot create output dir %s: %s" % (output_dir, exc))
    if not os.path.isdir(output_dir):
        raise OutputError("output dir is not a directory: %s" % output_dir)


class OutputError(Exception):
    """Local artifact directory/file could not be produced."""


def pull_to_output(adb, remote_path, output_dir, kind, ext):
    ensure_output_dir(output_dir)
    local_path = os.path.abspath(os.path.join(output_dir, make_local_name(kind, ext)))
    adb.pull(remote_path, local_path)
    if not os.path.isfile(local_path) or os.path.getsize(local_path) == 0:
        raise OutputError("pulled file missing or empty: %s" % local_path)
    return local_path


def do_status(adb, args):
    state_kind, state = adb.device_state()
    if state_kind is None:
        return emit("status", adb.serial, False, error="device_not_connected")
    if state_kind == "offline":
        return emit("status", adb.serial, False, error="device_offline")
    boot = adb.boot_completed()
    if boot != "1":
        log("boot_completed=%r" % boot)
        return emit("status", adb.serial, False, error="not_booted")
    return emit("status", adb.serial, True, artifact=None)


def do_screenshot(adb, args):
    remote = "%s-screenshot-%s.png" % (REMOTE_PREFIX, uuid.uuid4().hex)
    try:
        adb.screencap(remote)
        try:
            artifact = pull_to_output(adb, remote, args.output_dir, "screenshot", "png")
        finally:
            adb.remove_remote(remote)
        return emit("screenshot", adb.serial, True, artifact=artifact)
    except (AdbError, OutputError):
        adb.remove_remote(remote)
        raise


def do_hierarchy(adb, args):
    remote = "%s-hierarchy-%s.xml" % (REMOTE_PREFIX, uuid.uuid4().hex)
    try:
        adb.ui_dump(remote)
        try:
            artifact = pull_to_output(adb, remote, args.output_dir, "hierarchy", "xml")
        finally:
            adb.remove_remote(remote)
        return emit("hierarchy", adb.serial, True, artifact=artifact)
    except (AdbError, OutputError):
        adb.remove_remote(remote)
        raise


def do_tap(adb, args):
    x_raw, y_raw = args.x, args.y
    if not (COORD_RE.match(x_raw or "") and COORD_RE.match(y_raw or "")):
        return emit("tap", adb.serial, False, error="invalid_coords")
    x, y = int(x_raw), int(y_raw)
    width, height = adb.screen_size()
    if x >= width or y >= height:
        log("tap (%d,%d) outside screen %dx%d" % (x, y, width, height))
        return emit("tap", adb.serial, False, error="invalid_coords")
    adb.input_tap(x, y)
    return emit("tap", adb.serial, True, artifact=None)


def do_text(adb, args):
    value = args.value or ""
    if not TEXT_RE.match(value) or len(value) > MAX_TEXT_LEN:
        log("unsupported text %r: only single ASCII [A-Za-z0-9]+ words are supported "
            "(no spaces, punctuation, Chinese or other Unicode)" % value[:80])
        return emit("text", adb.serial, False, error="unsupported_text")
    adb.input_text(value)
    return emit("text", adb.serial, True, artifact=None)


ACTIONS = {
    "status": do_status,
    "screenshot": do_screenshot,
    "hierarchy": do_hierarchy,
    "tap": do_tap,
    "text": do_text,
}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="adb_control.py",
        description="Minimal ADB control CLI for one sandboxed Android emulator.",
    )
    parser.add_argument("--adb", required=True, help="path to adb.exe")
    parser.add_argument("--serial", required=True,
                        help="emulator serial with even port, e.g. emulator-5554")
    parser.add_argument("--output-dir", required=True,
                        help="directory for PNG/XML artifacts")
    sub = parser.add_subparsers(dest="action", required=True,
                                metavar="{status,screenshot,hierarchy,tap,text}")
    sub.add_parser("status", help="read ADB device state and boot_completed")
    sub.add_parser("screenshot", help="screencap to device Download, pull PNG to output-dir")
    sub.add_parser("hierarchy", help="uiautomator dump, pull XML to output-dir")
    p_tap = sub.add_parser("tap", help="input tap at integer coordinates")
    p_tap.add_argument("--x", required=True, help="x pixel (0-based)")
    p_tap.add_argument("--y", required=True, help="y pixel (0-based)")
    p_text = sub.add_parser("text", help="input a single ASCII [A-Za-z0-9]+ word")
    p_text.add_argument("--value", required=True, help="e.g. termux")
    return parser


def main(argv=None, runner=None):
    parser = build_parser()
    args = None
    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as exc:
        log("usage error: %s" % exc)
        return emit(None, None, False, error="usage")
    except SystemExit as exc:
        if exc.code in (0, None):  # --help
            raise
        log("usage error: missing or invalid arguments")
        return emit(None, None, False, error="usage")

    serial_err = check_serial(args.serial)
    if serial_err is not None:
        return emit(args.action, args.serial, False, error=serial_err)
    if not check_adb_path(args.adb):
        return emit(args.action, args.serial, False, error="adb_not_found")
    # Output-boundary check BEFORE any device command, for every action.
    output_err = check_output_dir(args.output_dir)
    if output_err is not None:
        log("output dir %r rejected (approved roots: %s)" % (args.output_dir, APPROVED_OUTPUT_ROOTS))
        return emit(args.action, args.serial, False, error=output_err)

    adb = AdbControl(args.adb, args.serial, runner=runner)
    try:
        return ACTIONS[args.action](adb, args)
    except AdbTimeout as exc:
        log("%s" % exc)
        return emit(args.action, args.serial, False, error="adb_timeout")
    except AdbNotFound as exc:
        log("%s" % exc)
        return emit(args.action, args.serial, False, error="adb_not_found")
    except AdbError as exc:
        log("%s" % exc)
        return emit(args.action, args.serial, False, error="adb_error")
    except OutputError as exc:
        log("%s" % exc)
        return emit(args.action, args.serial, False, error="output_failed")


if __name__ == "__main__":
    sys.exit(main())
