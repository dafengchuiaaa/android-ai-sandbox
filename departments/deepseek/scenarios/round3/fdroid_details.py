#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fdroid_details.py —— 第三轮：搜索 → 唯一定位指定应用 → 打开详情 → 独立读回详情。

只通过**项目根目录 sandbox.py**（冻结的 CLI 转发入口）操作，不直接碰 ADB、
不启停实例、不安装 APK、不截图、不改设置、不用真实账号。

流程（≤16 次 CLI 调用、≤120 秒、单调用有超时、stale 最多恢复一次）：

    status → launch → locate 输入框 → click → set-text → evaluate-search
           → 从**候选里唯一精确匹配** label → click 该结果 → observe/after_observation
           → 独立提取详情（标题 / 版本 / 摘要 / 包名 / 操作控件）

```
python fdroid_details.py --sandbox <sandbox.py> --python <解释器> --adb <adb.exe> \
    --state-file <项目实例state> --session-dir <antigravity runtime 会话> \
    --query aurora --label "Aurora Droid" --report-dir <round3 证据目录> \
    [--profile api35-google-x86_64]
python fdroid_details.py --parse-observation <保存的观察JSON> --label Termux
```

要点：

* **不默认选第一个结果**、不把 `Termux:API` 当 `Termux`：只认 label **完全相等**的候选，
  0 个 → `target_not_found`，>1 个 → `target_ambiguous`。
* **profile 与搜索场景同一来源**：期望 AVD 由 `--profile` 从受版本控制的固定定义推导；
  复用 `fdroid_search` 的调用链时同样带上 profile 并核对 `profile_id` 绑定，
  不会被丢掉。显式 `--expect-avd` 与所选 profile 冲突时在任何调用前拒绝。
* **详情识别独立于搜索验证**：一次 click 返回 0 不等于打开了详情。
  只有"精确标题 + 独立结构证据（不是搜索列表）"才算 `details_verified`；
  没显示的字段一律 null，绝不编造。
* **识别规则待实机确认**（本项目当前没有任何详情页真实样本）：报告里
  `details.recognition.status = "provisional_pending_real_sample"`，
  规则写在 `details.recognition.rules`；严格失败优于假成功。
* 只读复用已验收的 `scenarios/fdroid_search.py`（v1.0.1）里的会话/定位/判定 helpers，
  不复制分叉、不修改它；本目录只新增 round3 的文件。
* stdout 恒一条 JSON；目标未验证时退出码非零；输入输出 UTF-8；报告含每次动作的宿主 UTC 起止时间。

退出码：0 详情已验证；1 跑完但没验证到（搜索未确认/目标找不到/同名不唯一/详情证据不足）；
2 参数或报告目录不合法；3 sandbox/协议/设备/超时等失败。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROUND3_DIR = Path(__file__).resolve().parent
SCEN_DIR = ROUND3_DIR.parent
BASE_DIR = SCEN_DIR                       # departments/deepseek/scenarios
DEEPSEEK_DIR = SCEN_DIR.parent            # departments/deepseek
for extra in (str(SCEN_DIR), str(DEEPSEEK_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import fdroid_search as ds      # noqa: E402  已验收 v1.0.1，只读复用
import content_adapter as ca    # noqa: E402  已验收 v1.1.0，只读复用（只为常量）
import round4_profiles as r4    # noqa: E402  固定 profile 接线（与搜索场景共用）

MODULE_VERSION = "1.0.1-r4profile"
ACTION_NAME = "fdroid_details"
ACTION_PARSE = "fdroid_details_parse"
DEFAULT_SANDBOX = str(SCEN_DIR.parent.parent / "sandbox.py")   # 项目根 sandbox.py

DEFAULT_MAX_CALLS = 16
DEFAULT_TOTAL_TIMEOUT_S = 120.0
DEFAULT_CALL_TIMEOUT_S = 20.0
# 期望 AVD 由 --profile（缺省 baseline）推导；不在这里写第二份 AVD 常量。
DEVICE_EXPECT_AVD = ds.DEFAULT_EXPECT_AVD

# 详情页结构证据：控件类标签（整串归一后相等），**未在真机详情样本上确认**
BACK_LABELS = frozenset({"back", "navigate up", "up", "\u2190"})
DETAIL_ACTION_LABELS = frozenset({
    "install", "update", "uninstall", "open", "share", "website", "source code",
    "issues", "donate", "anti-features", "permissions", "versions", "links", "metadata",
    "nearby", "changelog", "report issue",
})
# 详情页上"看得出来"的字段的识别正则（同样待实机确认）
PACKAGE_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+){1,6}$")
VERSION_RE = re.compile(r"^v?[0-9]+\.[0-9][0-9A-Za-z._-]{0,24}$")
MAX_VISIBLE_TEXTS = 40
SUMMARY_MIN_CHARS = 20

STAGES = (
    "status", "launch", "observe", "search_input", "search_focus",
    "search_set_text", "search_evaluate", "locate_target", "open_details",
    "extract_details", "parse_observation",
)

OUTCOME_VERIFIED = "details_verified"
OUTCOME_SEARCH_NOT_VERIFIED = "search_not_verified"
OUTCOME_TARGET_NOT_FOUND = "target_not_found"
OUTCOME_TARGET_AMBIGUOUS = "target_ambiguous"
OUTCOME_DETAILS_NOT_CONFIRMED = "details_not_confirmed"
OUTCOME_ERROR = "error"
OUTCOME_PARSE_ONLY = "parse_only"

# 错误码 → outcome（未列出的都算 error）
OUTCOME_BY_CODE = {
    "search_not_verified": OUTCOME_SEARCH_NOT_VERIFIED,
    "target_not_found": OUTCOME_TARGET_NOT_FOUND,
    "target_ambiguous": OUTCOME_TARGET_AMBIGUOUS,
    "details_not_confirmed": OUTCOME_DETAILS_NOT_CONFIRMED,
}
NOT_VERIFIED_CODES = frozenset(OUTCOME_BY_CODE)


def _utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class TimedSession(ds.PhoneSession):
    """给每一步补上宿主 UTC 起止时间（只加时间戳，不改已验收的调用逻辑）。"""

    def call(self, action, args=()):
        started = _utc_now()
        try:
            return super().call(action, args)
        finally:
            if self.steps:
                last = self.steps[-1]
                if last.get("action") == action and "host_started_utc" not in last:
                    last["host_started_utc"] = started
                    last["host_ended_utc"] = _utc_now()

ScenarioError = ds.ScenarioError
EXIT_VERIFIED = ds.EXIT_VERIFIED
EXIT_NOT_VERIFIED = ds.EXIT_NOT_VERIFIED
EXIT_USAGE = ds.EXIT_USAGE
EXIT_FAILURE = ds.EXIT_FAILURE


# --------------------------------------------------------------------------
# 结果候选定位（结构判定，可在重新观察后重算）
# --------------------------------------------------------------------------

def label_rows(obs, label):
    """观察里"子树含 label 精确文本"的可点击行（保留最外层，去掉嵌套在里面的候选）。

    只看结构（clickable/enabled + 子树文本完全相等），不看坐标，也不依赖搜索结果
    payload，所以 stale 重新观察之后可以重算。
    """
    nodes = obs.get("nodes") if isinstance(obs, dict) else None
    if not isinstance(nodes, list):
        return []
    by_id = {n.get("node_id"): n for n in nodes if isinstance(n, dict)}
    children = {}
    for n in nodes:
        if isinstance(n, dict):
            children.setdefault(n.get("parent_id"), []).append(n)

    def subtree_has_label(node):
        stack, seen = [node], set()
        while stack:
            cur = stack.pop()
            if not isinstance(cur, dict):
                continue
            nid = cur.get("node_id")
            if nid in seen:
                continue
            seen.add(nid)
            if isinstance(cur.get("text"), str) and cur["text"] == label:
                return True
            stack.extend(children.get(nid, []))
        return False

    rows = [n for n in nodes
            if isinstance(n, dict) and n.get("clickable") is True and n.get("enabled") is True
            and not (isinstance(n.get("class"), str) and n["class"].endswith("EditText"))
            and subtree_has_label(n)]

    def nested_in_other(row):
        candidates = {r.get("node_id") for r in rows if r is not row}
        cur, seen = row, set()
        while isinstance(cur, dict):
            pid = cur.get("parent_id")
            if pid is None or pid in seen:
                return False
            seen.add(pid)
            if pid in candidates:
                return True
            cur = by_id.get(pid)
        return False

    return [r for r in rows if not nested_in_other(r)]


def locate_target_row(obs, label):
    """唯一精确匹配 label 的结果行；0 个/多个都明确失败。"""
    rows = label_rows(obs, label)
    if len(rows) == 1:
        return rows[0]
    if not rows:
        raise ScenarioError(
            "target_not_found",
            "no result row whose label is exactly %r (near names are not accepted)" % label,
            EXIT_NOT_VERIFIED,
            stage="locate_target",
            label=label,
            observation_id=obs.get("observation_id"),
        )
    raise ScenarioError(
        "target_ambiguous",
        "more than one result row matches %r exactly; refusing to pick one" % label,
        EXIT_NOT_VERIFIED,
        stage="locate_target",
        label=label,
        row_node_ids=[r.get("node_id") for r in rows],
    )


# --------------------------------------------------------------------------
# 详情提取（纯函数，便于离线核验保存的观察 JSON）
# --------------------------------------------------------------------------

def _norm(value):
    return value.strip().casefold() if isinstance(value, str) else ""


def _text_nodes(obs):
    return [n for n in (obs.get("nodes") or [])
            if isinstance(n, dict) and isinstance(n.get("text"), str) and n["text"].strip()]


def _field(node, source):
    return {
        "text": node.get("text"),
        "node_id": node.get("node_id"),
        "class": node.get("class"),
        "bounds": node.get("bounds"),
        "source": source,
    }


def _inside(by_id, node, ancestor_ids):
    """node 自身或它的任一祖先是否在 ancestor_ids 里。"""
    cur, seen = node, set()
    while isinstance(cur, dict):
        nid = cur.get("node_id")
        if nid in seen:
            return False
        seen.add(nid)
        if nid in ancestor_ids:
            return True
        cur = by_id.get(cur.get("parent_id"))
    return False


def _children_map(nodes):
    children = {}
    for n in nodes:
        children.setdefault(n.get("parent_id"), []).append(n)
    return children


def _subtree(children, node):
    out, stack, seen = [], [node], set()
    while stack:
        cur = stack.pop()
        if not isinstance(cur, dict):
            continue
        nid = cur.get("node_id")
        if nid in seen:
            continue
        seen.add(nid)
        out.append(cur)
        stack.extend(children.get(nid, []))
    return out


def _action_controls(nodes):
    """可见的操作控件：可点击容器（自己或子节点里有整串动作标签，如 Install/Share）。

    真机上按钮文字常挂在 clickable 容器的**子** TextView 上，所以按子树判定。
    """
    children = _children_map(nodes)
    controls = []
    for n in nodes:
        if n.get("clickable") is not True or n.get("enabled") is not True:
            continue
        for sub in _subtree(children, n):
            hit = None
            for key in ("text", "content_desc"):
                value = sub.get(key)
                if isinstance(value, str) and _norm(value) in DETAIL_ACTION_LABELS:
                    hit = (value, sub.get("node_id"), sub.get("class"))
                    break
            if hit:
                controls.append({
                    "label": hit[0],
                    "label_node_id": hit[1],
                    "label_class": hit[2],
                    "control_node_id": n.get("node_id"),
                    "bounds": n.get("bounds"),
                    "clickable": True,
                    "enabled": True,
                })
                break
    return controls


def extract_details(obs, label):
    """从一份观察里独立提取详情：标题 + 可见字段 + 结构证据。

    纯函数：不写文件、不调 CLI，所以也能用于解析 Codex 保存的真实观察 JSON。
    """
    nodes = [n for n in (obs.get("nodes") or []) if isinstance(n, dict)]
    by_id = {n.get("node_id"): n for n in nodes}
    texts = _text_nodes(obs)
    all_texts = [n["text"] for n in texts]

    # 1) 精确标题（完全相等，不做 strip/大小写折叠）
    title_nodes = [n for n in texts if n["text"] == label]
    title = _field(title_nodes[0], "exact_text_match") if title_nodes else None

    # 2) 结构证据：区分详情页与搜索列表
    input_fields = [n for n in nodes
                    if isinstance(n.get("class"), str) and n["class"].endswith("EditText")]
    back_nodes = [n for n in nodes
                  if _norm(n.get("text")) in BACK_LABELS or _norm(n.get("content_desc")) in BACK_LABELS]
    action_controls = _action_controls(nodes)
    action_ids = {c["control_node_id"] for c in action_controls}
    apps_header = [n for n in nodes if n.get("text") == "Apps"]
    signals = []
    if not input_fields:
        signals.append("no_search_field")
    if back_nodes:
        signals.append("has_back_control")
    if action_controls:
        signals.append("has_detail_action_control")
    if not apps_header:
        signals.append("no_results_header")
    missing = [s for s in ("no_search_field", "has_back_control",
                           "has_detail_action_control", "no_results_header")
               if s not in signals]

    # 3) 可见字段（只认"页面上真的显示了"的内容，其余 null）
    package_node = next((n for n in texts if PACKAGE_RE.match(n["text"])), None)
    version_node = next((n for n in texts
                         if VERSION_RE.match(n["text"]) and n is not package_node), None)
    candidates = [
        n for n in texts
        if n is not title
        and (package_node is None or n is not package_node)
        and (version_node is None or n is not version_node)
        and len(n["text"]) >= SUMMARY_MIN_CHARS
        and " " in n["text"].strip()
        and _norm(n["text"]) not in DETAIL_ACTION_LABELS
        and _norm(n["text"]) not in BACK_LABELS
        and _norm(n["text"]) not in ca.CHROME_ACTION_LABELS
        and not _inside(by_id, n, action_ids)
    ]
    summary_node = max(candidates, key=lambda n: len(n["text"]), default=None) if candidates else None

    fields = {
        "package_name": _field(package_node, "package_like_text") if package_node else None,
        "version": _field(version_node, "version_like_text") if version_node else None,
        "summary": _field(summary_node, "longest_plain_text") if summary_node else None,
    }
    unknown = sorted(k for k, v in fields.items() if v is None)

    title_present = title is not None
    positive = sorted({"has_back_control", "has_detail_action_control"} & set(signals))
    # 要求：精确标题 + 不是搜索列表 + 至少两条结构信号 + **至少一条正向信号**
    # （只靠"缺什么"不能算证据，否则一个空白页也能过关）
    verified = bool(title_present and "no_search_field" in signals
                    and len(signals) >= 2 and positive)

    return {
        "observation_id": obs.get("observation_id"),
        "observed_at": obs.get("observed_at"),
        "coverage": obs.get("coverage"),
        "device_serial": obs.get("device_serial"),
        "avd": obs.get("avd"),
        "xml_path": obs.get("xml_path"),
        "target_label": label,
        "title": title,
        "title_node_count": len(title_nodes),
        "fields": fields,
        "fields_reliable": verified,
        "unknown_fields": unknown,
        "action_controls": action_controls,
        "structure_evidence": {
            "signals": signals,
            "missing": missing,
            "input_field_node_ids": [n.get("node_id") for n in input_fields],
            "back_node_ids": [n.get("node_id") for n in back_nodes],
            "action_node_ids": sorted(action_ids),
            "apps_header_node_ids": [n.get("node_id") for n in apps_header],
            "positive_signals": positive,
            "required": ["exact_title", "no_search_field", "signals>=2", ">=1 positive signal"],
            "satisfied": verified,
        },
        "details_verified": verified,
        "visible_texts": all_texts[:MAX_VISIBLE_TEXTS],
        "visible_texts_truncated": len(all_texts) > MAX_VISIBLE_TEXTS,
        "recognition": {
            "status": "provisional_pending_real_sample",
            "reason": ("no real F-Droid Basic detail-page dump exists in this project yet; "
                       "these rules must be re-checked against the first real sample"),
            "rules": {
                "title": "node text exactly equals the requested label",
                "not_search_list": "no EditText node in the observation",
                "signal_set": sorted(["no_search_field", "has_back_control",
                                      "has_detail_action_control", "no_results_header"]),
                "minimum_signals": 2,
                "minimum_positive_signals": 1,
                "positive_signals": ["has_back_control", "has_detail_action_control"],
                "package_name": PACKAGE_RE.pattern,
                "version": VERSION_RE.pattern,
                "summary": "longest plain text >= %d chars, containing a space, not an action label"
                           % SUMMARY_MIN_CHARS,
                "action_labels": sorted(DETAIL_ACTION_LABELS),
            },
        },
    }


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def resolve_report_dir(path_str):
    """报告只能落在 round3 目录内（departments/deepseek/scenarios/round3/**）。"""
    base = Path(os.path.realpath(str(ROUND3_DIR)))
    target = Path(os.path.realpath(os.path.abspath(str(path_str))))
    try:
        target.relative_to(base)
    except ValueError:
        raise ScenarioError(
            "report_dir_outside_round3_dir",
            "report-dir must stay inside the round3 scenario directory",
            EXIT_USAGE,
            target=str(target),
            allowed_dir=str(base),
        )
    return target


def write_report(cfg, data):
    target_dir = resolve_report_dir(cfg.report_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    base = target_dir / ("fdroid-details-%s-%s.json" % (stamp, os.getpid()))
    path = base
    n = 1
    while path.exists():
        n += 1
        path = base.with_name(base.stem + "-%d.json" % n)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8", newline="\n")
    return path


# --------------------------------------------------------------------------
# 场景主体
# --------------------------------------------------------------------------

def run_scenario(cfg):
    session = TimedSession(cfg)
    data = {
        "scenario": ACTION_NAME,
        "module_version": MODULE_VERSION,
        "query": cfg.query,
        "target_label": cfg.label,
        "stage": "status",
        "host_started_utc": _utc_now(),
        "outcome": OUTCOME_ERROR,
        "completed_through": None,
        "device": None,
        "profile": dict(getattr(cfg, "profile_info", None)
                        or {"profile_id": session.profile_id, "avd": cfg.expect_avd,
                            "source": "integration/instance_profiles.py (fixed definition)"}),
        "search": None,
        "target": None,
        "details": None,
        "details_verified": False,
        "warnings": [],
        "limits": {
            "max_calls": cfg.max_calls,
            "total_timeout_s": cfg.total_timeout_s,
            "call_timeout_s": cfg.call_timeout_s,
            "stale_recovery_allowed": 1,
            "stale_recovery_used": 0,
            "screenshots_taken": 0,
            "screenshot_default": "not called",
            "expected_avd": cfg.expect_avd,
            "profile_id": session.profile_id,
        },
        "calls": {},
        "steps": [],
        "report_path": None,
        "sandbox": str(cfg.sandbox),
    }
    artifacts = []
    started = time.time()
    try:
        status = session.call("status")
        status_data = status.get("data") if isinstance(status.get("data"), dict) else {}
        data["device"] = ds._device_from_status(status_data, cfg.expect_avd)
        ds.require_profile_binding(session.profile_id, status_data, "status")
        session.device = data["device"]
        data["completed_through"] = "status"

        data["stage"] = "launch"
        launch = session.call("launch", ["--package", ds.FDROID_PACKAGE])
        launch_data = launch.get("data") if isinstance(launch.get("data"), dict) else {}
        if launch_data.get("package") != ds.FDROID_PACKAGE:
            raise ScenarioError(
                "invalid_phone_response",
                "launch must report the whitelisted package %r (got %r)"
                % (ds.FDROID_PACKAGE, launch_data.get("package")),
                EXIT_FAILURE, stage="launch",
            )
        obs = ds.as_observation(launch_data.get("after_observation"))
        if obs is None:
            data["stage"] = "observe"
            obs = ds.observe(session)
        ds.check_device_binding(obs, data["device"], cfg.expect_avd, "observation", session.profile_id)
        session.device = data["device"]
        data["completed_through"] = "launch"

        # --- 搜索阶段（与已验收场景同一套定位/校验） ---
        data["stage"] = "search_input"
        fields = ds.input_fields(obs)
        located = {}
        if len(fields) == 1:
            data["completed_through"] = "search_input"
        elif len(fields) > 1:
            ds.require_input_field(obs)          # 抛 input_field_ambiguous
        else:
            _, obs, entry = ds.advance(session, obs, "click", ds.require_search_entry)
            located["search_entry_node_id"] = entry.get("node_id")
            ds.require_input_field(obs)
            data["completed_through"] = "search_input"

        data["stage"] = "search_focus"
        _, obs, focus = ds.advance(session, obs, "click", ds.require_input_field)
        located["focus_click_node_id"] = focus.get("node_id")
        data["completed_through"] = "search_focus"

        data["stage"] = "search_set_text"
        typed, obs, typed_node = ds.advance(session, obs, "set-text", ds.require_input_field,
                                            extra=("--value", cfg.query))
        located["set_text_node_id"] = typed_node.get("node_id")
        input_record = ds._check_readback(typed, cfg.query)
        data["completed_through"] = "search_set_text"

        data["stage"] = "search_evaluate"
        oid = obs.get("observation_id")
        ev_payload = session.call("evaluate-search", ["--observation-id", oid, "--query", cfg.query])
        ev_data = ev_payload.get("data") if isinstance(ev_payload.get("data"), dict) else {}
        ds.check_device_binding(ev_data, data["device"], cfg.expect_avd, "evaluate-search", session.profile_id)
        outcome, summary, warns = ds._verdict(ev_data, cfg.query)
        data["search"] = {
            "observation_id": oid,
            "observed_at": obs.get("observed_at"),
            "profile_id": obs.get("profile_id"),
            "input": input_record,
            "located": located,
            "verdict": summary,
            "adapter_outcome": outcome,
            "warnings": warns,
        }
        data["warnings"].extend(warns)
        if outcome == ds.OUTCOME_INVALID:
            raise ScenarioError(
                "invalid_phone_response",
                "evaluate-search returned self-contradictory fields; refusing to continue",
                EXIT_FAILURE, stage="search_evaluate", violations=summary.get("violations"),
            )
        if outcome != ds.OUTCOME_VERIFIED:
            data["outcome"] = OUTCOME_SEARCH_NOT_VERIFIED
            data["completed_through"] = "search_evaluate"
            raise ScenarioError(
                "search_not_verified",
                "search phase did not verify (adapter outcome %r); not opening details" % outcome,
                EXIT_NOT_VERIFIED, stage="search_evaluate", adapter_outcome=outcome,
            )
        data["completed_through"] = "search_evaluate"

        # --- 唯一定位目标行，并用**当前观察**打开详情 ---
        data["stage"] = "locate_target"
        target_row = locate_target_row(obs, cfg.label)
        data["target"] = {
            "label": cfg.label,
            "row_node_id": target_row.get("node_id"),
            "matched_exactly": True,
            "candidate_labels": [c.get("label") for c in (ev_data.get("real_result_candidates") or [])
                                 if isinstance(c, dict)],
        }
        data["completed_through"] = "locate_target"

        data["stage"] = "open_details"
        _, obs_detail, clicked = ds.advance(
            session, obs, "click", lambda o: locate_target_row(o, cfg.label))
        data["target"]["clicked_row_node_id"] = clicked.get("node_id")

        # --- 独立提取详情 ---
        data["stage"] = "extract_details"
        details = extract_details(obs_detail, cfg.label)
        data["details"] = details
        data["details_observation"] = {
            "observation_id": obs_detail.get("observation_id"),
            "profile_id": obs_detail.get("profile_id"),
        }
        data["details_verified"] = bool(details["details_verified"])
        if not data["details_verified"]:
            data["outcome"] = OUTCOME_DETAILS_NOT_CONFIRMED
            data["completed_through"] = "extract_details"
            raise ScenarioError(
                "details_not_confirmed",
                "the page after the click does not show the independent detail structure "
                "(exact title + not-a-search-list evidence)",
                EXIT_NOT_VERIFIED,
                stage="extract_details",
                signals=details["structure_evidence"]["signals"],
                missing=details["structure_evidence"]["missing"],
            )
        data["outcome"] = OUTCOME_VERIFIED
        data["completed_through"] = "extract_details"
    except ScenarioError as exc:
        data["error"] = exc.as_json()
        data["stage"] = exc.detail.get("stage") or data.get("stage")
        if not data.get("outcome") or data["outcome"] == OUTCOME_ERROR:
            data["outcome"] = OUTCOME_BY_CODE.get(exc.code, OUTCOME_ERROR)
        if exc.code == "stale_observation":
            data["warnings"].append({
                "code": "stale_not_recoverable",
                "message": "stale_observation again after the single allowed recovery; stopping",
            })
    finally:
        data["host_ended_utc"] = _utc_now()
        data["limits"]["stale_recovery_used"] = 1 if session.recovery_used else 0
        data["calls"] = {
            "count": len(session.steps),
            "max_calls": cfg.max_calls,
            "sequence": ["%s%s" % (s["action"], "/timeout" if s.get("timed_out") else "")
                         for s in session.steps],
            "elapsed_ms": int((time.time() - started) * 1000),
        }
        data["steps"] = session.steps
        data["no_coordinates_sent"] = all(
            not str(t).lstrip("-").isdigit() for s in session.steps for t in s.get("argv", [])
        )
        try:
            path = write_report(cfg, data)
            data["report_path"] = str(path)
            artifacts.append(str(path))
        except ScenarioError as exc:
            data["warnings"].append({"code": exc.code, "message": exc.message})
        except OSError as exc:
            data["warnings"].append({"code": "report_not_written", "message": str(exc)})

    error = data.get("error")
    ok = data.get("details_verified") is True
    payload = {"ok": ok, "action": ACTION_NAME, "data": data, "error": error, "artifacts": artifacts}
    if ok:
        payload["exit_code"] = EXIT_VERIFIED
    elif error and error.get("code") in NOT_VERIFIED_CODES:
        payload["exit_code"] = EXIT_NOT_VERIFIED
    elif error:
        payload["exit_code"] = EXIT_FAILURE
    else:
        payload["exit_code"] = EXIT_NOT_VERIFIED
    return payload


# --------------------------------------------------------------------------
# 只读解析保存的观察 JSON
# --------------------------------------------------------------------------

def run_parse(cfg):
    path = Path(cfg.parse_observation)
    if not path.is_file():
        raise ScenarioError("invalid_argument", "observation file not found: %s" % path, EXIT_USAGE)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ScenarioError("invalid_argument", "observation JSON is not readable: %s" % exc, EXIT_USAGE)
    if isinstance(raw, dict) and "data" in raw and isinstance(raw["data"], dict):
        raw = raw["data"]                       # 允许直接吃 sandbox/phone CLI 的信封
    obs = ds.as_observation(raw)
    if obs is None:
        raise ScenarioError(
            "invalid_argument",
            "observation JSON must contain observation_id and nodes",
            EXIT_USAGE,
            file=str(path),
        )
    # 保存的观察若声明了 profile，必须与本次 --profile 一致：不把 baseline 观察
    # 当成新配置的证据，反之亦然。
    problems = r4.profile_binding_problems(
        getattr(cfg, "profile", None) or r4.DEFAULT_PROFILE_ID, obs)
    if problems:
        raise ScenarioError(
            "ownership_mismatch",
            "saved observation profile binding failed: %s" % "; ".join(problems),
            EXIT_USAGE,
            file=str(path),
            expected_profile=getattr(cfg, "profile", None) or r4.DEFAULT_PROFILE_ID,
            observation_profile_id=obs.get("profile_id"),
            problems=problems,
        )
    details = extract_details(obs, cfg.label)
    data = {
        "scenario": ACTION_PARSE,
        "module_version": MODULE_VERSION,
        "stage": "parse_observation",
        "source_file": str(path),
        "target_label": cfg.label,
        "profile": dict(getattr(cfg, "profile_info", None)
                        or {"profile_id": getattr(cfg, "profile", None) or r4.DEFAULT_PROFILE_ID,
                            "source": "integration/instance_profiles.py (fixed definition)"}),
        "observation_profile_id": obs.get("profile_id"),
        "details": details,
        "details_verified": bool(details["details_verified"]),
        "outcome": OUTCOME_VERIFIED if details["details_verified"] else OUTCOME_PARSE_ONLY,
        "warnings": [{
            "code": "recognition_rules_provisional",
            "message": details["recognition"]["reason"],
        }],
        "calls": {"count": 0, "sequence": []},
        "steps": [],
        "no_coordinates_sent": True,
    }
    artifacts = []
    if cfg.report_dir:
        try:
            data["report_path"] = str(write_report(cfg, data))
            artifacts.append(data["report_path"])
        except ScenarioError as exc:
            data["warnings"].append({"code": exc.code, "message": exc.message})
    payload = {
        "ok": data["details_verified"],
        "action": ACTION_PARSE,
        "data": data,
        "error": None if data["details_verified"] else {
            "code": "details_not_confirmed",
            "message": "extraction did not find the required detail structure in this observation",
            "detail": {"stage": "parse_observation",
                       "signals": details["structure_evidence"]["signals"]},
        },
        "artifacts": artifacts,
        "exit_code": EXIT_VERIFIED if data["details_verified"] else EXIT_NOT_VERIFIED,
    }
    return payload


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(
        prog="fdroid_details.py",
        description="Search F-Droid Basic, open exactly one named app, and read its details offline-safely.",
    )
    p.add_argument("--version", action="version", version="%(prog)s " + MODULE_VERSION)
    p.add_argument("--parse-observation", dest="parse_observation", default=None,
                   help="read a saved observation JSON instead of driving sandbox.py")
    p.add_argument("--sandbox", default=DEFAULT_SANDBOX, help="project-root sandbox.py (frozen CLI entry)")
    p.add_argument("--python", default=sys.executable, help="interpreter used to run sandbox.py")
    p.add_argument("--adb", default=None, help="adb path, passed through to sandbox.py")
    p.add_argument("--state-file", dest="state_file", default=None, help="launcher-produced instance state")
    p.add_argument("--session-dir", dest="session_dir", default=None, help="sandbox.py --output-dir")
    p.add_argument("--query", default=None, help="search term, [A-Za-z0-9]{1,200}")
    p.add_argument("--label", required=True, help="exact app label to open (e.g. Termux)")
    p.add_argument("--profile", default=None,
                   help="fixed instance profile from integration/instance_profiles.py "
                        "(default: %s); the expected AVD is derived from it" % r4.DEFAULT_PROFILE_ID)
    p.add_argument("--expect-avd", dest="expect_avd", default=DEVICE_EXPECT_AVD,
                   help="AVD that status must report (default: derived from --profile)")
    p.add_argument("--report-dir", dest="report_dir", default=None,
                   help="report directory inside scenarios/round3 (default evidence/offline)")
    p.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS, dest="max_calls")
    p.add_argument("--total-timeout", type=float, default=DEFAULT_TOTAL_TIMEOUT_S, dest="total_timeout_s")
    p.add_argument("--per-call-timeout", type=float, default=DEFAULT_CALL_TIMEOUT_S, dest="call_timeout_s")
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # pragma: no cover
            pass
    args = build_parser().parse_args(argv)
    try:
        if not args.label or not isinstance(args.label, str):
            raise ScenarioError("invalid_argument", "--label must not be empty", EXIT_USAGE)
        try:
            selection = r4.select(args.profile, args.expect_avd)
        except r4.ProfileSelectionError as exc:
            raise ScenarioError(exc.code, exc.message, EXIT_USAGE, **exc.detail)
        args.profile = selection.profile_id
        args.profile_info = selection.as_dict()
        args.expect_avd = selection.avd
        if args.report_dir is None:
            args.report_dir = str(ROUND3_DIR / "evidence" / "offline")
        if args.parse_observation:
            resolve_report_dir(args.report_dir)     # 早失败
            payload = run_parse(args)
        else:
            missing = [name for name in ("adb", "state_file", "session_dir", "query")
                       if not getattr(args, name)]
            if missing:
                raise ScenarioError(
                    "invalid_argument",
                    "missing required device arguments: %s" % ", ".join("--" + m.replace("_", "-")
                                                                        for m in missing),
                    EXIT_USAGE,
                )
            if not ds.QUERY_RE.match(args.query):
                raise ScenarioError(
                    "invalid_argument",
                    "query must match [A-Za-z0-9]{1,200} (sandbox set-text charset)",
                    EXIT_USAGE, query=args.query,
                )
            if args.max_calls < 1 or args.total_timeout_s <= 0 or args.call_timeout_s <= 0:
                raise ScenarioError("invalid_argument", "budgets must be positive", EXIT_USAGE)
            sandbox = ds._require_file(args.sandbox, "--sandbox")
            adb = ds._require_file(args.adb, "--adb")
            state_file = ds._require_file(args.state_file, "--state-file")
            python = ds._resolve_executable(args.python, "--python")
            resolve_report_dir(args.report_dir)
            cfg = argparse.Namespace(
                phone_cli=sandbox, python=python, adb=adb, state_file=state_file,
                session_dir=args.session_dir, query=args.query, expect_avd=args.expect_avd,
                profile=args.profile, profile_info=args.profile_info,
                report_dir=args.report_dir, max_calls=args.max_calls,
                total_timeout_s=args.total_timeout_s, call_timeout_s=args.call_timeout_s,
                screenshot_on_failure=False, sandbox=sandbox,
                target="phone-cli", label=args.label,
                parse_observation=None,
            )
            payload = run_scenario(cfg)
    except ScenarioError as exc:
        sys.stderr.write("%s: %s\n" % (exc.code, exc.message))
        payload = {
            "ok": False, "action": ACTION_NAME, "data": None,
            "error": exc.as_json(), "artifacts": [], "exit_code": exc.exit_code,
        }
    except Exception as exc:  # pragma: no cover
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
