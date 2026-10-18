#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fdroid_search.py —— 固定搜索场景（F-Droid Basic），只通过冻结的 phone CLI 操作。

契约：docs/phone-cli-contract-v0.1.md（v0.1）。本脚本**不是**手机控制器：
它不直接运行 ADB、不启停实例、不读写设备，只按契约拼参数调用 phone CLI，
并根据返回的 JSON 决定下一步。

```
python fdroid_search.py --phone-cli <phone.py> --python <解释器> --adb <adb.exe> \
    --state-file <项目实例state> --session-dir <antigravity/runtime/唯一会话> \
    --query termux --report-dir <自己的证据目录> [--profile api35-google-x86_64]
```

流程：status → launch → observe/after_observation → 按**实际节点结构**定位
Search 入口或已有 EditText → click → set-text → evaluate-search。

硬性约束（与 dispatch/20 一致）：

* 只用**当前观察**里的 node_id，绝不写死坐标，也不自己拼 tap/swipe 计划。
* 期望 AVD 由 `--profile`（缺省 baseline）从 `integration/instance_profiles.py`
  的固定定义推导；显式 `--expect-avd` 与所选 profile 冲突时在任何调用前拒绝。
* 非 baseline profile 的每次 phone CLI 调用都显式带 `--profile`，并核对返回的
  `profile_id` 绑定（缺字段同样失败）。
* 返回 `ok=true` 不等于目标成功：必须核对 `content_verified`、`input_matches_query`、
  候选数、空态；`network_verified` 恒记 `unknown`。
* 搜索入口不唯一、或多个输入框、或结构上找不到 → **明确失败**，不猜、不点第一个。
* `stale_observation` 只允许**一次**重新 observe + 重新按结构定位；再次 stale 直接失败。
* 每次调用有超时，全流程 90 秒上限、最多 12 次 CLI 调用，失败逐步保留，不循环轮询。
* 截图默认不调用（仅显式 `--screenshot-on-failure` 时在失败后调用一次）。
* 只写 `departments/deepseek/scenarios/**`（报告目录越界即拒绝）。

stdout 恒一条 JSON（`--help` 除外），外层沿用契约外形：
`{"ok":...,"action":"fdroid_search","data":{...},"error":null,"artifacts":[...]}`。
退出码：0 目标已验证；1 跑完但未验证（空态/查询未确认）；2 参数或报告目录不合法；
3 phone CLI/协议/超时/权限等失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

MODULE_VERSION = "1.0.2-r4profile"
ACTION_NAME = "fdroid_search"
SCENARIO_DIR = Path(__file__).resolve().parent
DEEPSEEK_DIR = SCENARIO_DIR.parent
if str(SCENARIO_DIR) not in sys.path:
    sys.path.insert(0, str(SCENARIO_DIR))

import round4_profiles as r4   # noqa: E402  固定 profile 接线（共用模块）

FDROID_PACKAGE = "org.fdroid.basic"          # 契约只允许这一个包
# 期望 AVD 不再写死：由 --profile（缺省 baseline）从受版本控制的固定 profile
# 定义（integration/instance_profiles.py）推导，避免出现第二份 AVD 常量。
DEFAULT_EXPECT_AVD = None
SERIAL_RE = re.compile(r"^emulator-(\d+)$")  # 合法-emulator 序列号（端口须为偶数）
DEFAULT_MAX_CALLS = 12
DEFAULT_TOTAL_TIMEOUT_S = 90.0
DEFAULT_CALL_TIMEOUT_S = 20.0
MIN_TOTAL_SLICE_S = 0.05        # 剩余预算低于此值就不再发起新调用（总时限是硬上限）
QUERY_RE = re.compile(r"^[A-Za-z0-9]{1,200}$")   # 契约 set-text 只支持该字符集
SEARCH_LABELS = ("search",)                       # 归一后整串相等才算搜索入口

# phone CLI 的错误码（契约列举）；本脚本原样透传，另加自己的协议层错误码
PHONE_ERROR_CODES = (
    "invalid_argument", "invalid_output_path", "ownership_mismatch", "device_unavailable",
    "driver_timeout", "stale_observation", "unknown_node_id", "unsupported_target",
    "verification_failed",
)

OUTCOME_VERIFIED = "verified"
OUTCOME_EMPTY = "empty_state"
OUTCOME_NOT_VERIFIED = "not_verified"
OUTCOME_INVALID = "invalid_response"   # 字段缺失/类型不对/自相矛盾，不得当成功
OUTCOME_ERROR = "error"

EXIT_VERIFIED = 0
EXIT_NOT_VERIFIED = 1
EXIT_USAGE = 2
EXIT_FAILURE = 3


class ScenarioError(Exception):
    """场景层错误：错误码 + 可读信息 + 可选的 detail。"""

    def __init__(self, code, message, exit_code=EXIT_FAILURE, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code
        self.detail = detail

    def as_json(self):
        err = {"code": self.code, "message": self.message}
        if self.detail:
            err["detail"] = self.detail
        return err


# --------------------------------------------------------------------------
# 观察与节点定位（纯结构判断，不猜）
# --------------------------------------------------------------------------

def _norm(value):
    return value.strip().casefold() if isinstance(value, str) else ""


def as_observation(data):
    """data 是否是契约里的“观察”形状（observe 或 after_observation）。"""
    if not isinstance(data, dict):
        return None
    if not isinstance(data.get("observation_id"), str) or not data["observation_id"]:
        return None
    if not isinstance(data.get("nodes"), list):
        return None
    return data


def input_fields(obs):
    """class 以 EditText 结尾的节点（结构证据，不看文本内容）。"""
    return [
        n for n in obs.get("nodes", [])
        if isinstance(n, dict) and isinstance(n.get("class"), str)
        and n["class"].endswith("EditText")
    ]


def search_entries(obs):
    """归一后 text 或 content-desc 整串等于 'search' 的节点（含不可点击的标签：
    真正可点的祖先由 phone CLI 沿真实父链解析，本脚本不越权判断）。"""
    out = []
    for n in obs.get("nodes", []):
        if not isinstance(n, dict):
            continue
        for key in ("text", "content_desc"):
            if _norm(n.get(key)) in SEARCH_LABELS:
                out.append(n)
                break
    return out


def require_input_field(obs):
    fields = input_fields(obs)
    if len(fields) == 1:
        return fields[0]
    if not fields:
        raise ScenarioError(
            "input_field_not_found",
            "no EditText input field in the current observation",
            EXIT_FAILURE,
            observation_id=obs.get("observation_id"),
        )
    raise ScenarioError(
        "input_field_ambiguous",
        "more than one EditText input field is present; the operated field cannot be determined",
        EXIT_FAILURE,
        observation_id=obs.get("observation_id"),
        field_node_ids=[n.get("node_id") for n in fields],
    )


def require_search_entry(obs):
    entries = search_entries(obs)
    if len(entries) == 1:
        return entries[0]
    if not entries:
        raise ScenarioError(
            "search_entry_not_found",
            "no 'Search' entry (text/content-desc) and no EditText in the current observation",
            EXIT_FAILURE,
            observation_id=obs.get("observation_id"),
        )
    raise ScenarioError(
        "search_entry_ambiguous",
        "more than one 'Search' entry is present; refusing to guess which one opens search",
        EXIT_FAILURE,
        observation_id=obs.get("observation_id"),
        entry_node_ids=[n.get("node_id") for n in entries],
    )


def _node_arg(node):
    nid = node.get("node_id")
    if not isinstance(nid, str) or not nid:
        raise ScenarioError("invalid_phone_response", "node has no node_id", EXIT_FAILURE)
    return nid


def _obs_args(obs, node):
    oid = obs.get("observation_id")
    if not isinstance(oid, str) or not oid:
        raise ScenarioError("invalid_phone_response", "observation has no observation_id", EXIT_FAILURE)
    return ["--observation-id", oid, "--node-id", _node_arg(node)]


# --------------------------------------------------------------------------
# phone CLI 会话
# --------------------------------------------------------------------------

class PhoneSession:
    """按契约调用 phone CLI，记录每一步（含失败），守住调用数与时间上限。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.steps = []
        self.started = time.monotonic()
        self.recovery_used = False
        self.device = None            # status 之后填上，用于观察/评估的设备绑定校验
        self.expect_avd = cfg.expect_avd
        # 本次运行选定的固定 profile；非 baseline 时每次调用都显式带上
        self.profile_id = getattr(cfg, "profile", None) or r4.DEFAULT_PROFILE_ID

    # -- 记账 ---------------------------------------------------------
    def _elapsed_ms(self):
        return int((time.monotonic() - self.started) * 1000)

    def remaining_s(self):
        return self.cfg.total_timeout_s - (time.monotonic() - self.started)

    def take_recovery(self):
        """stale 重定位机会全局只有一次。"""
        if self.recovery_used:
            return False
        self.recovery_used = True
        return True

    def record(self, entry):
        entry["index"] = len(self.steps) + 1
        entry["elapsed_ms"] = self._elapsed_ms()
        self.steps.append(entry)
        return entry

    # -- 调用 ---------------------------------------------------------
    def call(self, action, args=()):
        if len(self.steps) >= self.cfg.max_calls:
            raise ScenarioError(
                "call_budget_exhausted",
                "phone CLI call budget exhausted (%d calls)" % self.cfg.max_calls,
                EXIT_FAILURE,
                max_calls=self.cfg.max_calls,
                steps=len(self.steps),
            )
        remaining = self.remaining_s()
        if remaining <= MIN_TOTAL_SLICE_S:
            raise ScenarioError(
                "total_timeout",
                "scenario total time budget exhausted (%.1fs)" % self.cfg.total_timeout_s,
                EXIT_FAILURE,
                total_timeout_s=self.cfg.total_timeout_s,
                action=action,
                remaining_s=round(remaining, 3),
            )
        timeout = min(self.cfg.call_timeout_s, remaining)
        argv = [
            self.cfg.python, str(self.cfg.phone_cli),
            "--adb", str(self.cfg.adb),
            "--state-file", str(self.cfg.state_file),
            "--output-dir", str(self.cfg.session_dir),
        ] + r4.phone_args(self.profile_id) + [
            action,
        ] + [str(a) for a in args]

        entry = {"action": action, "args": [str(a) for a in args], "argv": argv,
                 "profile_id": self.profile_id, "timeout_s": round(timeout, 3)}
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            entry.update({
                "timed_out": True, "exit_code": None, "ok": False,
                "error_code": "driver_timeout",
                "elapsed_call_ms": int((time.monotonic() - t0) * 1000),
                "raw_stdout": (exc.stdout or "")[:4000] if isinstance(exc.stdout, str) else "",
            })
            self.record(entry)
            raise ScenarioError(
                "driver_timeout",
                "phone CLI call '%s' exceeded %.1fs" % (action, timeout),
                EXIT_FAILURE,
                action=action,
                timeout_s=round(timeout, 3),
            )
        except OSError as exc:
            entry.update({
                "exit_code": None, "ok": False, "error_code": "phone_cli_not_launchable",
                "elapsed_call_ms": int((time.monotonic() - t0) * 1000),
            })
            self.record(entry)
            raise ScenarioError(
                "phone_cli_not_launchable",
                "could not launch phone CLI: %s" % exc,
                EXIT_FAILURE,
                python=self.cfg.python,
                phone_cli=str(self.cfg.phone_cli),
            )

        entry["elapsed_call_ms"] = int((time.monotonic() - t0) * 1000)
        entry["exit_code"] = proc.returncode
        entry["raw_stdout"] = (proc.stdout or "")[:4000]
        entry["stderr_head"] = (proc.stderr or "")[:1000]

        payload = self._parse(proc.stdout, action, entry)
        entry["ok"] = payload.get("ok") is True
        entry["data_keys"] = sorted(payload["data"].keys()) if isinstance(payload.get("data"), dict) else []

        if payload.get("ok") is True:
            if proc.returncode != 0:
                entry["error_code"] = "invalid_phone_response"
                self.record(entry)
                raise ScenarioError(
                    "invalid_phone_response",
                    "phone CLI reported ok=true with a non-zero exit code (%s)" % proc.returncode,
                    EXIT_FAILURE,
                    action=action,
                )
            self.record(entry)
            return payload
        else:
            err = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = err.get("code") if isinstance(err.get("code"), str) else "phone_cli_failed"
            entry["error_code"] = code
            entry["error_message"] = err.get("message")
            if proc.returncode == 0:
                entry["error_code"] = "invalid_phone_response"
                self.record(entry)
                raise ScenarioError(
                    "invalid_phone_response",
                    "phone CLI reported ok=false but exited 0",
                    EXIT_FAILURE,
                    action=action,
                )
            self.record(entry)
            raise ScenarioError(
                code,
                "phone CLI %s failed: %s" % (action, err.get("message") or code),
                EXIT_FAILURE,
                action=action,
                phone_error=err or None,
            )

    def _parse(self, stdout, action, entry):
        text = (stdout or "").strip()
        if not text:
            entry["error_code"] = "empty_stdout"
            self.record(entry)
            raise ScenarioError("invalid_json_from_phone", "phone CLI printed nothing on stdout", EXIT_FAILURE, action=action)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            entry["error_code"] = "invalid_json_from_phone"
            self.record(entry)
            raise ScenarioError(
                "invalid_json_from_phone",
                "stdout was not a single JSON object: %s" % exc,
                EXIT_FAILURE,
                action=action,
                stdout_head=text[:400],
            )
        if not isinstance(payload, dict) or "ok" not in payload:
            entry["error_code"] = "invalid_phone_response"
            self.record(entry)
            raise ScenarioError(
                "invalid_phone_response",
                "stdout JSON is not a phone CLI envelope",
                EXIT_FAILURE,
                action=action,
            )
        if "action" in payload and payload["action"] != action:
            entry["error_code"] = "invalid_phone_response"
            self.record(entry)
            raise ScenarioError(
                "invalid_phone_response",
                "envelope action %r does not match requested action %r" % (payload["action"], action),
                EXIT_FAILURE,
                action=action,
            )
        return payload


def observe(session):
    payload = session.call("observe")
    obs = as_observation(payload.get("data"))
    if obs is None:
        raise ScenarioError(
            "invalid_phone_response",
            "observe data is missing observation_id/nodes",
            EXIT_FAILURE,
            data_keys=sorted((payload.get("data") or {}).keys()) if isinstance(payload.get("data"), dict) else [],
        )
    if session.device is not None:
        check_device_binding(obs, session.device, session.expect_avd, "observation", session.profile_id)
    return obs


def advance(session, obs, action, locate, extra=()):
    """定位 → 调用 → 刷新观察。遇 stale_observation 时只允许一次：重新 observe + 重新定位。"""
    while True:
        node = locate(obs)                                   # 每次都用当前观察重新定位
        try:
            payload = session.call(action, _obs_args(obs, node) + [str(e) for e in extra])
        except ScenarioError as exc:
            if exc.code == "stale_observation" and session.take_recovery():
                obs = observe(session)                       # 唯一一次重新观察
                continue                                     # 下一轮按结构重新定位，不复用旧 node_id
            raise
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        nxt = as_observation(data.get("after_observation"))
        if nxt is None:
            nxt = observe(session)                           # 契约要求有 after_observation；缺了就补一次 observe
            session.steps[-1]["note"] = "after_observation missing; explicit observe used"
        elif session.device is not None:
            check_device_binding(nxt, session.device, session.expect_avd, "observation", session.profile_id)
        return payload, nxt, node


# --------------------------------------------------------------------------
# 场景主体
# --------------------------------------------------------------------------

def _device_from_status(data, expect_avd):
    """status 必须给出 ready、合法 emulator 序列号（偶数端口）与预期 AVD。

    契约只接受本项目 AVD 与合法偶数端口 emulator 序列号；状态文件不是信任凭证，
    所以这里对返回值本身也做校验，不把"CLI 说 ok"当作设备正确。
    """
    data = data if isinstance(data, dict) else {}
    serial = data.get("device_serial")
    avd = data.get("avd")
    if data.get("ready") is not True:
        raise ScenarioError(
            "device_unavailable",
            "phone CLI status is not ready",
            EXIT_FAILURE,
            status=data,
        )
    if not isinstance(serial, str) or not serial:
        raise ScenarioError(
            "ownership_mismatch",
            "status did not report a device_serial",
            EXIT_FAILURE,
            status=data,
        )
    m = SERIAL_RE.match(serial)
    if not m or int(m.group(1)) % 2 != 0:
        raise ScenarioError(
            "ownership_mismatch",
            "device_serial is not a legal emulator serial (even port expected): %r" % serial,
            EXIT_FAILURE,
            device_serial=serial,
        )
    if not isinstance(avd, str) or not avd:
        raise ScenarioError(
            "ownership_mismatch",
            "status did not report an AVD name",
            EXIT_FAILURE,
            status=data,
        )
    if avd != expect_avd:
        raise ScenarioError(
            "ownership_mismatch",
            "status reports a different AVD than expected: %r != %r" % (avd, expect_avd),
            EXIT_FAILURE,
            reported_avd=avd,
            expected_avd=expect_avd,
        )
    return {"device_serial": serial, "avd": avd}


def require_profile_binding(profile_id, payload_data, kind):
    """响应必须属于所选固定 profile；非 baseline 时缺 profile_id 同样失败。

    与设备/AVD 校验并列：AVD 相符还不够，响应还得声明同一个 profile，
    否则一个 baseline 形状的响应可能被当成新配置的结果。
    """
    problems = r4.profile_binding_problems(profile_id, payload_data)
    if problems:
        raise ScenarioError(
            "ownership_mismatch",
            "%s profile binding failed: %s" % (kind, "; ".join(problems)),
            EXIT_FAILURE,
            kind=kind,
            expected_profile=profile_id,
            reported_profile=payload_data.get("profile_id") if isinstance(payload_data, dict) else None,
            problems=problems,
        )


def check_device_binding(payload, device, expect_avd, kind, profile_id=None):
    """观察 / evaluate-search 必须明确属于同一设备、同一 AVD 与同一 profile。

    **缺字段同样失败**（不允许"缺 serial 就跳过对比"）。
    """
    data = payload if isinstance(payload, dict) else {}
    if profile_id is not None:
        require_profile_binding(profile_id, data, kind)
    serial = data.get("device_serial")
    if not isinstance(serial, str) or not serial:
        raise ScenarioError(
            "ownership_mismatch",
            "%s did not report device_serial; refusing to skip the device comparison" % kind,
            EXIT_FAILURE,
            kind=kind,
            expected_serial=device.get("device_serial"),
        )
    if serial != device.get("device_serial"):
        raise ScenarioError(
            "ownership_mismatch",
            "%s belongs to a different device (%r != %r)" % (kind, serial, device.get("device_serial")),
            EXIT_FAILURE,
            kind=kind,
            reported_serial=serial,
            expected_serial=device.get("device_serial"),
        )
    avd = data.get("avd")
    if avd is None:
        if kind == "observation":
            raise ScenarioError(
                "ownership_mismatch",
                "%s did not report avd; refusing to skip the AVD comparison" % kind,
                EXIT_FAILURE,
                kind=kind,
                expected_avd=expect_avd,
            )
        return
    if avd != expect_avd:
        raise ScenarioError(
            "ownership_mismatch",
            "%s reports a different AVD than expected: %r != %r" % (kind, avd, expect_avd),
            EXIT_FAILURE,
            kind=kind,
            reported_avd=avd,
            expected_avd=expect_avd,
        )


def _check_readback(payload, query):
    """契约要求精确读回：**不做 strip/casefold**，多一个空格也算失败。"""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    readback = data.get("readback_text")
    typed = data.get("input_text")
    verified = data.get("verified")
    problems = []
    if verified is not True or type(verified) is not bool:
        problems.append("verified is not exactly True (got %r)" % (verified,))
    if type(typed) is not str or typed != query:
        problems.append("input_text is not exactly the query (got %r)" % (typed,))
    if type(readback) is not str or readback != query:
        problems.append("readback_text is not exactly the query (got %r)" % (readback,))
    if problems:
        raise ScenarioError(
            "verification_failed",
            "set-text readback does not exactly match the query; refusing to treat it as success",
            EXIT_FAILURE,
            expected=query,
            input_text=typed,
            readback_text=readback,
            verified=verified,
            problems=problems,
        )
    return {"input_text": typed, "readback_text": readback, "verified": True}


def _evidence_matches_query(evidence, query):
    """成功路径必须核对 input_evidence，不能只信 input_matches_query 这个布尔。"""
    problems = []
    if not isinstance(evidence, dict):
        return ["input_evidence is missing"]
    field_count = evidence.get("field_count")
    if type(field_count) is not int or field_count != 1:
        problems.append("input_evidence.field_count is not exactly 1 (got %r)" % (field_count,))
    if evidence.get("ambiguous") is not False:
        problems.append("input_evidence.ambiguous is not False (got %r)" % (evidence.get("ambiguous"),))
    if evidence.get("exact_match") is not True:
        problems.append("input_evidence.exact_match is not True (got %r)" % (evidence.get("exact_match"),))
    raw = evidence.get("raw_text")
    if type(raw) is not str or raw != query:
        problems.append("input_evidence.raw_text does not exactly equal the query (got %r)" % (raw,))
    node_id = evidence.get("node_id")
    matches = evidence.get("matches_query_node_ids")
    if not isinstance(node_id, str) or not node_id:
        problems.append("input_evidence.node_id is missing")
    elif not isinstance(matches, list) or node_id not in matches:
        problems.append("input_evidence.node_id is not in matches_query_node_ids")
    return problems


def _verdict(ev_data, query):
    """严格判定 evaluate-search 的返回值：(outcome, 摘要, 警告)。

    * 类型/缺失/自相矛盾 → `invalid_response`（调用方转成 `invalid_phone_response`）
    * 只有全部成功信号明确成立才 `verified`；空态是 `empty_state`；其余 `not_verified`
    * 任何输入都不抛异常：校验函数必须 fail closed，不能 fail open
    """
    warnings = []
    try:
        data = ev_data if isinstance(ev_data, dict) else {}
        violations = []

        # 1) 类型与存在性（结构性检查，无论是否声称成功）
        for name in ("content_verified", "input_matches_query", "empty_state"):
            if type(data.get(name)) is not bool:
                violations.append("%s must be a JSON boolean (got %r)" % (name, data.get(name)))
        if type(data.get("result_count")) is not int or type(data.get("result_count")) is bool:
            violations.append("result_count must be an int, not bool (got %r)" % (data.get("result_count"),))
        candidates = data.get("real_result_candidates")
        if not isinstance(candidates, list):
            violations.append("real_result_candidates must be a list (got %r)" % (type(candidates).__name__,))
        page_kind = data.get("page_kind")
        if type(page_kind) is not str or not page_kind:
            violations.append("page_kind must be a non-empty string (got %r)" % (page_kind,))

        count = data.get("result_count")
        if isinstance(candidates, list) and type(count) is int and type(count) is not bool:
            if len(candidates) != count:
                violations.append(
                    "result_count (%d) does not match the candidate list length (%d)"
                    % (count, len(candidates))
                )

        # 2) 成功路径：所有成功信号必须明确成立且有 input_evidence 支撑
        if not violations and data.get("content_verified") is True:
            if page_kind != "search_results":
                violations.append("page_kind is %r while content_verified claims success" % (page_kind,))
            if data.get("input_matches_query") is not True:
                violations.append("content_verified is true while input_matches_query is not true")
            if data.get("empty_state") is not False:
                violations.append("content_verified is true while empty_state is not false")
            if not isinstance(candidates, list) or not candidates:
                violations.append("content_verified is true with an empty candidate list")
            violations.extend(_evidence_matches_query(data.get("input_evidence"), query))

        if violations:
            return OUTCOME_INVALID, {
                "page_kind": page_kind,
                "content_verified": data.get("content_verified"),
                "input_matches_query": data.get("input_matches_query"),
                "result_count": count,
                "empty_state": data.get("empty_state"),
                "network_verified": "unknown",
                "network_verified_reported": data.get("network_verified"),
                "observed_at": data.get("observed_at"),
                "query": query,
                "violations": violations,
            }, warnings

        reported_net = data.get("network_verified")
        if reported_net != "unknown":
            warnings.append({
                "code": "phone_cli_network_state_not_unknown",
                "message": "the phone CLI reported a network state; this scenario keeps network_verified=unknown",
                "reported": reported_net,
            })
        if not isinstance(data.get("observed_at"), str) or not data["observed_at"]:
            warnings.append({
                "code": "observed_at_missing",
                "message": "evaluate-search did not report the observation capture time",
            })
        for code in (w.get("code") for w in (data.get("warnings") or []) if isinstance(w, dict)):
            if code in ("query_not_confirmed", "multiple_search_fields", "conflicting_evidence"):
                warnings.append({"code": "adapter:" + code, "message": "adapter reported %s" % code})

        summary = {
            "page_kind": page_kind,
            "content_verified": data.get("content_verified"),
            "input_matches_query": data.get("input_matches_query"),
            "result_count": count,
            "candidate_count": len(candidates),
            "empty_state": data.get("empty_state"),
            "network_verified": "unknown",
            "network_verified_reported": reported_net,
            "observed_at": data.get("observed_at"),
            "query": query,
            "violations": [],
        }
        if data.get("content_verified") is True and candidates:
            return OUTCOME_VERIFIED, summary, warnings
        if data.get("empty_state") is True:
            if candidates:
                warnings.append({
                    "code": "conflicting_evidence",
                    "message": "empty_state is true while rows exist; not counted as verified",
                })
                return OUTCOME_NOT_VERIFIED, summary, warnings
            return OUTCOME_EMPTY, summary, warnings
        return OUTCOME_NOT_VERIFIED, summary, warnings
    except Exception as exc:      # pragma: no cover - 兜底 fail closed
        return OUTCOME_INVALID, {
            "query": query,
            "violations": ["verdict raised %s: %s" % (type(exc).__name__, exc)],
            "network_verified": "unknown",
        }, warnings


def run_scenario(cfg):
    session = PhoneSession(cfg)
    data = {
        "scenario": ACTION_NAME,
        "module_version": MODULE_VERSION,
        "query": cfg.query,
        "outcome": OUTCOME_ERROR,
        "scenario_completed": False,
        "target_verified": False,
        "device": None,
        "profile": dict(getattr(cfg, "profile_info", None)
                        or {"profile_id": session.profile_id, "avd": cfg.expect_avd,
                            "source": "integration/instance_profiles.py (fixed definition)"}),
        "observation": None,
        "located": {},
        "verification": None,
        "network_verified": "unknown",
        "warnings": [],
        "limits": {
            "max_calls": cfg.max_calls,
            "total_timeout_s": cfg.total_timeout_s,
            "call_timeout_s": cfg.call_timeout_s,
            "stale_recovery_allowed": 1,
            "stale_recovery_used": 0,
            "screenshot_on_failure": bool(cfg.screenshot_on_failure),
            "screenshots_taken": 0,
        },
        "calls": {},
        "report_path": None,
        "no_coordinates_sent": True,
    }
    artifacts = []
    try:
        status = session.call("status")
        status_data = status.get("data") if isinstance(status.get("data"), dict) else {}
        data["device"] = _device_from_status(status_data, cfg.expect_avd)
        require_profile_binding(session.profile_id, status_data, "status")
        session.device = data["device"]        # 之后每个观察/evaluate 都要对齐这台设备
        data["limits"]["device_serial"] = data["device"]["device_serial"]
        data["limits"]["avd"] = data["device"]["avd"]
        data["limits"]["expected_avd"] = cfg.expect_avd
        data["limits"]["profile_id"] = session.profile_id

        launch = session.call("launch", ["--package", FDROID_PACKAGE])
        launch_data = launch.get("data") if isinstance(launch.get("data"), dict) else {}
        if launch_data.get("package") != FDROID_PACKAGE:
            raise ScenarioError(
                "invalid_phone_response",
                "launch must report the whitelisted package %r (got %r)"
                % (FDROID_PACKAGE, launch_data.get("package")),
                EXIT_FAILURE,
                expected_package=FDROID_PACKAGE,
                reported_package=launch_data.get("package"),
            )
        obs = as_observation(launch_data.get("after_observation"))
        if obs is None:
            obs = observe(session)
        check_device_binding(obs, data["device"], cfg.expect_avd, "observation", session.profile_id)
        data["observation"] = {
            "observation_id": obs.get("observation_id"),
            "observed_at": obs.get("observed_at"),
            "coverage": obs.get("coverage"),
            "xml_path": obs.get("xml_path"),
            "profile_id": obs.get("profile_id"),
        }

        # 1) 定位：已有 EditText 就直接用；否则按结构找唯一 Search 入口并点开
        field = None
        fields_now = input_fields(obs)
        if len(fields_now) == 1:
            field = fields_now[0]
        elif len(fields_now) > 1:
            require_input_field(obs)              # 抛 input_field_ambiguous
        else:
            _, obs, entry_node = advance(session, obs, "click", require_search_entry)
            data["located"]["search_entry_node_id"] = entry_node.get("node_id")
            field = require_input_field(obs)      # 点开之后必须出现唯一输入框
        data["located"]["field_node_id_before_focus"] = field.get("node_id")

        # 2) 聚焦 + 输入 + 精确读回
        _, obs, focus_node = advance(session, obs, "click", require_input_field)
        data["located"]["focus_click_node_id"] = focus_node.get("node_id")
        typed_payload, obs, typed_node = advance(
            session, obs, "set-text", require_input_field, extra=("--value", cfg.query))
        data["located"]["set_text_node_id"] = typed_node.get("node_id")
        data["input"] = _check_readback(typed_payload, cfg.query)

        # 3) 评估当前观察（不声称重新联网）
        oid = obs.get("observation_id")
        ev_payload = session.call("evaluate-search", ["--observation-id", oid, "--query", cfg.query])
        ev_data = ev_payload.get("data") if isinstance(ev_payload.get("data"), dict) else {}
        check_device_binding(ev_data, data["device"], cfg.expect_avd, "evaluate-search", session.profile_id)
        outcome, summary, warns = _verdict(ev_data, cfg.query)
        if outcome == OUTCOME_INVALID:
            raise ScenarioError(
                "invalid_phone_response",
                "evaluate-search returned missing / mistyped / self-contradictory fields; "
                "refusing to read it as a successful search",
                EXIT_FAILURE,
                violations=summary.get("violations"),
            )
        data["outcome"] = outcome
        data["verification"] = summary
        data["warnings"].extend(warns)
        data["observation"]["evaluation_observation_id"] = oid
        data["scenario_completed"] = True
        data["target_verified"] = outcome == OUTCOME_VERIFIED
    except ScenarioError as exc:
        data["error"] = exc.as_json()
        data["outcome"] = OUTCOME_ERROR
        if exc.code == "stale_observation":
            data["warnings"].append({
                "code": "stale_not_recoverable",
                "message": "stale_observation again after the single allowed recovery; stopping",
            })
    finally:
        data["limits"]["stale_recovery_used"] = 1 if session.recovery_used else 0
        data["limits"]["screenshots_taken"] = sum(1 for s in session.steps if s["action"] == "screenshot")
        _maybe_screenshot(session, cfg, data)
        data["limits"]["screenshots_taken"] = sum(1 for s in session.steps if s["action"] == "screenshot")
        data["calls"] = {
            "count": len(session.steps),
            "max_calls": cfg.max_calls,
            "elapsed_ms": session._elapsed_ms(),
            "sequence": ["%s%s" % (s["action"], "/timeout" if s.get("timed_out") else "") for s in session.steps],
        }
        data["steps"] = session.steps
        # 事实性自检：本次所有调用里没有出现任何裸坐标数字
        data["no_coordinates_sent"] = all(
            not t.lstrip("-").isdigit() for s in session.steps for t in s.get("argv", [])
        )
        try:
            report_path = write_report(cfg, data)
            data["report_path"] = str(report_path)
            artifacts.append(str(report_path))
        except OSError as exc:                      # 报告写不进去不影响已有结论，但要如实报
            data["warnings"].append({"code": "report_not_written", "message": str(exc)})
        data["error"] = data.get("error")

    error = data.get("error")
    ok = data.get("target_verified") is True
    payload = {"ok": ok, "action": ACTION_NAME, "data": data, "error": error, "artifacts": artifacts}
    if ok:
        payload["exit_code"] = EXIT_VERIFIED
    elif error:
        payload["exit_code"] = EXIT_FAILURE
    else:
        payload["exit_code"] = EXIT_NOT_VERIFIED
    return payload


def _maybe_screenshot(session, cfg, data):
    """默认不截图；仅显式开启且在失败后调用一次，并给出诊断原因。"""
    if not cfg.screenshot_on_failure or data.get("target_verified"):
        return
    reason = "fdroid_search failed: %s" % ((data.get("error") or {}).get("code")
                                           or data.get("outcome") or "unknown")
    try:
        session.call("screenshot", ["--reason", reason])
    except ScenarioError as exc:
        data["warnings"].append({"code": "screenshot_failed", "message": exc.message})


# --------------------------------------------------------------------------
# 报告与参数
# --------------------------------------------------------------------------

def resolve_report_dir(path_str):
    """报告只能落在本场景目录内（departments/deepseek/scenarios/**）。"""
    base = Path(os.path.realpath(str(SCENARIO_DIR)))
    target = Path(os.path.realpath(os.path.abspath(str(path_str))))
    try:
        target.relative_to(base)
    except ValueError:
        raise ScenarioError(
            "report_dir_outside_scenario_dir",
            "report-dir must stay inside the scenario directory",
            EXIT_USAGE,
            target=str(target),
            allowed_dir=str(base),
        )
    return target


def write_report(cfg, data):
    target_dir = resolve_report_dir(cfg.report_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = target_dir / ("fdroid-search-%s-%s.json" % (stamp, os.getpid()))
    path = base
    n = 1
    while path.exists():
        n += 1
        path = base.with_name(base.stem + "-%d.json" % n)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return path


def _resolve_executable(value, kind):
    if not value:
        raise ScenarioError("invalid_argument", "%s must not be empty" % kind, EXIT_USAGE)
    candidate = Path(value)
    if candidate.exists():
        return str(candidate)
    found = shutil.which(value)
    if found:
        return found
    raise ScenarioError(
        "invalid_argument",
        "%s not found: %s" % (kind, value),
        EXIT_USAGE,
        kind=kind,
        value=value,
    )


def _require_file(value, kind):
    if not value:
        raise ScenarioError("invalid_argument", "%s must not be empty" % kind, EXIT_USAGE)
    p = Path(value)
    if not p.is_file():
        raise ScenarioError("invalid_argument", "%s is not a file: %s" % (kind, value), EXIT_USAGE, kind=kind)
    return str(p)


def build_parser():
    p = argparse.ArgumentParser(
        prog="fdroid_search.py",
        description="Run one fixed F-Droid Basic search scenario through the frozen phone CLI contract.",
    )
    p.add_argument("--version", action="version", version="%(prog)s " + MODULE_VERSION)
    p.add_argument("--phone-cli", required=True, dest="phone_cli", help="path to phone.py (frozen contract)")
    p.add_argument("--python", required=True, dest="python", help="interpreter used to launch phone.py")
    p.add_argument("--adb", required=True, help="adb executable path (passed through, never run by this script)")
    p.add_argument("--state-file", required=True, dest="state_file", help="launcher-produced instance state file")
    p.add_argument("--session-dir", required=True, dest="session_dir", help="phone CLI --output-dir (antigravity runtime session)")
    p.add_argument("--query", required=True, help="search term, [A-Za-z0-9]{1,200}")
    p.add_argument("--profile", default=None,
                   help="fixed instance profile from integration/instance_profiles.py "
                        "(default: %s); the expected AVD is derived from it" % r4.DEFAULT_PROFILE_ID)
    p.add_argument("--expect-avd", default=DEFAULT_EXPECT_AVD, dest="expect_avd",
                   help="AVD that status must report (default: derived from --profile)")
    p.add_argument("--report-dir", default=str(SCENARIO_DIR / "evidence" / "offline"),
                   help="where the scenario report is written (must stay inside scenarios/)")
    p.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, dest="max_calls")
    p.add_argument("--total-timeout", type=float, default=DEFAULT_TOTAL_TIMEOUT_S, dest="total_timeout_s")
    p.add_argument("--per-call-timeout", type=float, default=DEFAULT_CALL_TIMEOUT_S, dest="call_timeout_s")
    p.add_argument("--screenshot-on-failure", action="store_true", dest="screenshot_on_failure")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # pragma: no cover
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if not QUERY_RE.match(args.query or ""):
            raise ScenarioError(
                "invalid_argument",
                "query must match [A-Za-z0-9]{1,200} (contract set-text charset)",
                EXIT_USAGE,
                query=args.query,
            )
        if args.max_calls < 1:
            raise ScenarioError("invalid_argument", "--max-calls must be >= 1", EXIT_USAGE)
        if args.total_timeout_s <= 0 or args.call_timeout_s <= 0:
            raise ScenarioError("invalid_argument", "timeouts must be > 0", EXIT_USAGE)
        try:
            selection = r4.select(args.profile, args.expect_avd)
        except r4.ProfileSelectionError as exc:
            raise ScenarioError(exc.code, exc.message, EXIT_USAGE, **exc.detail)
        cfg = argparse.Namespace(
            phone_cli=_require_file(args.phone_cli, "--phone-cli"),
            python=_resolve_executable(args.python, "--python"),
            adb=_require_file(args.adb, "--adb"),
            state_file=_require_file(args.state_file, "--state-file"),
            session_dir=args.session_dir,
            query=args.query,
            profile=selection.profile_id,
            profile_info=selection.as_dict(),
            expect_avd=selection.avd,
            report_dir=args.report_dir,
            max_calls=args.max_calls,
            total_timeout_s=args.total_timeout_s,
            call_timeout_s=args.call_timeout_s,
            screenshot_on_failure=args.screenshot_on_failure,
        )
        resolve_report_dir(cfg.report_dir)          # 早失败，不白跑设备
        payload = run_scenario(cfg)
    except ScenarioError as exc:
        sys.stderr.write("%s: %s\n" % (exc.code, exc.message))
        payload = {
            "ok": False, "action": ACTION_NAME, "data": None,
            "error": exc.as_json(), "artifacts": [], "exit_code": exc.exit_code,
        }
    except Exception as exc:                        # pragma: no cover - 兜底，不伪装成功
        import traceback
        traceback.print_exc(file=sys.stderr)
        payload = {
            "ok": False, "action": ACTION_NAME, "data": None,
            "error": {"code": "internal_error", "message": str(exc)},
            "artifacts": [], "exit_code": EXIT_FAILURE,
        }
    exit_code = payload.pop("exit_code", EXIT_FAILURE)
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
