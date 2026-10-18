#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
content_adapter.py —— DS-01 离线内容适配器（F-Droid Basic 单 App 适配）

把 Android UI Automator 的 hierarchy XML 转换成结构化观察（observe）、
按节点生成动作计划（resolve）、并对 F-Droid 搜索结果页做专用验证
（evaluate-search）。

约束（与 dispatch/12 一致）：
  * 纯离线：只读 XML 文本，不碰 ADB / 模拟器 / 账号 / 网络，不新增依赖。
  * 只生成计划，不执行任何设备指令。
  * XML 里的文本一律当作数据，绝不执行其中内容。
  * stdout 恒为单行 JSON（--help 除外）；失败非零退出，诊断走 stderr。
  * observe 只能往本模块目录（departments/deepseek/**）写 .json，
    且拒绝覆盖不是本工具产出的文件；输入 XML 只读不改。

退出码：0 成功；1 输入内容/领域错误；2 用法、文件 IO 或输出路径策略错误。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
import xml.etree.ElementTree as ET

MODULE_VERSION = "1.1.0"
GENERATOR = "content_adapter.py"
BASE_DIR = Path(__file__).resolve().parent

FDROID_PACKAGE = "org.fdroid.basic"
SUPPORTED_ACTIONS = ("click", "long_click", "scroll")
SCROLL_DIRECTIONS = ("up", "down", "left", "right")
COVERAGE = "partial"

# 底部导航标签（F-Droid Basic 的 Discover / Search / My apps），不得当结果
NAV_LABELS = frozenset({"discover", "search", "my apps"})
# 工具栏/设置/对话框动作标签：text 与 content-desc 都要查，且必须是**整串精确相等**
# （大小写与首尾空白归一后）。子串匹配会误伤普通 App 介绍，例如
# "Apply dynamic colors from your system settings" 或应用名 "Settings Sync" 都不是动作按钮。
CHROME_ACTION_LABELS = frozenset({
    "back", "navigate up", "close", "cancel", "dismiss", "ok", "done", "apply", "save",
    "retry", "skip", "next", "previous", "allow", "deny", "confirm", "clear",
    "clear search", "search", "settings", "repositories", "more options", "share", "copy",
})
# 空态/无结果文案
EMPTY_STATE_RE = re.compile(
    r"no apps found|fewer search terms|more repositories|nothing found|no results", re.I
)

BOUNDS_RE = re.compile(
    r"^\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]\s*\[\s*(-?\d+)\s*,\s*(-?\d+)\s*\]$"
)

# XML 属性名 -> JSON 字段名
BOOL_ATTRS = (
    ("enabled", "enabled"),
    ("clickable", "clickable"),
    ("checked", "checked"),
    ("checkable", "checkable"),
    ("focusable", "focusable"),
    ("focused", "focused"),
    ("long-clickable", "long_clickable"),
    ("password", "password"),
    ("selected", "selected"),
    ("scrollable", "scrollable"),
)

MAX_NODES = 20000
MAX_DEPTH = 400
SNAP_AXIS_MIN = 4  # 滑动的可用边长下限（像素）


class AdapterError(Exception):
    """带错误码的领域/用法错误。"""

    def __init__(self, code, message, exit_code=1, **detail):
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
# 字段解析规则（统一、可解释，缺字段一律 null，不编造）
# --------------------------------------------------------------------------

def norm_str(raw):
    """属性缺失或去空白后为空 -> None。"""
    if raw is None:
        return None
    s = raw.strip()
    return s if s else None


def norm_bool(raw):
    """'true'/'false' -> True/False；缺失 -> None；其它值 -> None（记警告）。"""
    if raw is None:
        return None
    v = raw.strip().lower()
    if v == "true":
        return True
    if v == "false":
        return False
    return None


def _is_dump_envelope(item):
    """dumpsys/uiautomator 的根 <hierarchy> 只是包裹元素，不是 Android 视图节点：
    它没有 class/package/enabled 等属性，不能当成"disabled 中途节点"阻断父链。"""
    return item.parent_idx is None and item.attr("class") is None and item.attr("package") is None


def _enabled_state(value):
    """给错误详情用的可读三态：true / false / unknown。"""
    if value is True:
        return "true"
    if value is False:
        return "false"
    return "unknown"


def parse_bounds(raw):
    """[x1,y1][x2,y2] -> [x1,y1,x2,y2]；非法/零面积/反向/缺失 -> None。"""
    if raw is None:
        return None
    m = BOUNDS_RE.match(raw.strip())
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    if x2 <= x1 or y2 <= y1:
        return None
    return [x1, y1, x2, y2]


def center(bounds):
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


class Item:
    """一个 XML 元素对应的树节点。"""

    __slots__ = ("idx", "parent_idx", "path", "el", "obs_key")

    def __init__(self, idx, parent_idx, path, el, obs_key):
        self.idx = idx
        self.parent_idx = parent_idx
        self.path = path
        self.el = el
        self.obs_key = obs_key

    @property
    def node_id(self):
        return "%s:%s" % (self.obs_key, self.path)

    def attr(self, name):
        return self.el.get(name)


class Observation:
    """对一份 hierarchy XML 快照的确定性解读。"""

    def __init__(self, xml_bytes, source_name=None):
        self.xml_bytes = xml_bytes
        self.source_name = source_name
        self.sha256 = hashlib.sha256(xml_bytes).hexdigest()
        # observation_id 由 XML 内容 hash 决定；node_id 前缀取前 12 位十六进制
        self.observation_id = self.sha256
        self.obs_key = self.sha256[:12]
        self.items = []
        self.root = ET.fromstring(xml_bytes)
        if not isinstance(self.root.tag, str):
            raise AdapterError("xml_unexpected_structure", "XML root is not an element", 1)
        self._build(self.root, None, "R", 0)

    # -- 树 ----------------------------------------------------------------
    def _build(self, el, parent_idx, path, depth):
        if depth > MAX_DEPTH:
            raise AdapterError("xml_too_deep", "XML nesting exceeds %d levels" % MAX_DEPTH, 1)
        if len(self.items) >= MAX_NODES:
            raise AdapterError("xml_too_large", "XML has more than %d nodes" % MAX_NODES, 1)
        idx = len(self.items)
        self.items.append(Item(idx, parent_idx, path, el, self.obs_key))
        for i, child in enumerate(list(el)):
            self._build(child, idx, "%s.%d" % (path, i), depth + 1)

    def node_id(self, item):
        return item.node_id

    def parent(self, item):
        if item.parent_idx is None:
            return None
        return self.items[item.parent_idx]

    def ancestor_chain(self, item):
        """item 自身 -> 根 的节点列表。"""
        chain = []
        cur = item
        while cur is not None:
            chain.append(cur)
            cur = self.parent(cur)
        return chain

    def descendants(self, item):
        """DFS 前序保证后代连续，遇到非后代即可停止。"""
        prefix = item.path + "."
        out = []
        for it in self.items[item.idx + 1:]:
            if it.path.startswith(prefix):
                out.append(it)
            else:
                break
        return out

    def subtree(self, item):
        return [item] + self.descendants(item)

    def is_within(self, item, container):
        return item.path.startswith(container.path + ".")

    def subtree_texts(self, item):
        out = []
        for it in self.subtree(item):
            t = norm_str(it.attr("text"))
            if t:
                out.append(t)
        return out

    # -- 基础投影 ----------------------------------------------------------
    def node_dict(self, it):
        a = it.el.attrib
        raw_bounds = a.get("bounds")
        bounds = parse_bounds(raw_bounds)
        d = {
            "node_id": it.node_id,
            "parent_id": None if it.parent_idx is None else self.items[it.parent_idx].node_id,
            "path": it.path,
            "package": norm_str(a.get("package")),
            "class": norm_str(a.get("class")),
            "resource_id": norm_str(a.get("resource-id")),
            "text": norm_str(a.get("text")),
            "content_desc": norm_str(a.get("content-desc")),
            "bounds": bounds,
            "bounds_raw": raw_bounds,
            "bounds_valid": bounds is not None,
        }
        for attr_name, key in BOOL_ATTRS:
            d[key] = norm_bool(a.get(attr_name))
        d["actions"] = derive_actions(d)
        return d

    def item_by_node_id(self, node_id):
        """严格校验 node_id：前缀必须等于本观察的 obs_key。"""
        if not isinstance(node_id, str) or ":" not in node_id:
            raise AdapterError(
                "invalid_node_id",
                "node-id must look like '<observation-prefix>:<tree-path>'",
                1,
                requested_node_id=node_id,
            )
        prefix, _, path = node_id.partition(":")
        if prefix != self.obs_key:
            raise AdapterError(
                "stale_node_id",
                "node-id belongs to a different observation; re-run observe on this XML",
                1,
                requested_node_id=node_id,
                requested_prefix=prefix,
                observation_prefix=self.obs_key,
                observation_id=self.observation_id,
            )
        for it in self.items:
            if it.path == path:
                return it
        raise AdapterError(
            "unknown_node_id",
            "node-id prefix matches this observation but the tree path does not exist",
            1,
            requested_node_id=node_id,
            observation_id=self.observation_id,
        )

    # -- 应用集合 ----------------------------------------------------------
    def packages(self):
        seen = []
        for it in self.items:
            pkg = norm_str(it.attr("package"))
            if pkg and pkg not in seen:
                seen.append(pkg)
        return seen

    def apps(self):
        return self.packages()

    def search_fields(self):
        return [it for it in self.items if (it.attr("class") or "").endswith("EditText")]

    # -- 警告 --------------------------------------------------------------
    def warnings(self):
        w = []

        def add(code, message, node_ids=None, **extra):
            entry = {"code": code, "message": message}
            if node_ids:
                entry["node_ids"] = list(node_ids)
            entry.update(extra)
            w.append(entry)

        root_pkg = norm_str(self.root.get("package"))
        if root_pkg is None:
            add(
                "root_package_missing",
                "OS dump root <hierarchy> has no package attribute; apps is derived from actual nodes",
                [self.items[0].node_id],
            )
        pkgs = self.packages()
        if not pkgs:
            add("no_package_information", "no node in this XML carries a package attribute")

        invalid = [
            it.node_id for it in self.items
            if it.attr("bounds") is not None and parse_bounds(it.attr("bounds")) is None
        ]
        if invalid:
            add(
                "invalid_bounds",
                "node has a bounds attribute that is not a positive-area rectangle; no tap coordinates derived "
                "(nodes without a bounds attribute at all are not listed here)",
                invalid,
            )

        bad_bool = []
        unknown_enabled = []
        for it in self.items:
            for attr_name, _key in BOOL_ATTRS:
                raw = it.attr(attr_name)
                if raw is not None and norm_bool(raw) is None:
                    bad_bool.append(it.node_id)
                    break
            if norm_bool(it.attr("clickable")) is True and norm_bool(it.attr("enabled")) is None:
                unknown_enabled.append(it.node_id)
        if bad_bool:
            add("unexpected_boolean_value", "boolean attribute is neither 'true' nor 'false'; treated as unknown (null)", bad_bool)
        if unknown_enabled:
            add("unknown_enabled_state", "node is clickable but enabled is unreadable; no action claimed", unknown_enabled)

        add("coverage_partial", "UI tree cannot prove all data on screen was captured; coverage stays partial")
        add(
            "observed_at_unavailable",
            "the XML carries no device observation timestamp; observe does not fabricate observed_at",
        )
        if len(pkgs) > 1:
            add("multiple_packages", "XML contains more than one package", packages=pkgs)
        if self.root.tag != "hierarchy":
            add("unexpected_root_tag", "root element tag is not <hierarchy>", root_tag=self.root.tag)
        return w

    # -- 序列化 ------------------------------------------------------------
    def source_block(self):
        return {
            "file": self.source_name,
            "sha256": self.sha256,
            "bytes": len(self.xml_bytes),
        }

    def to_json(self):
        return {
            "ok": True,
            "generator": GENERATOR,
            "module_version": MODULE_VERSION,
            "observation_id": self.observation_id,
            "source": "ui",
            "source_xml": self.source_block(),
            "apps": self.apps(),
            "coverage": COVERAGE,
            "observed_at": None,
            "nodes": [self.node_dict(it) for it in self.items],
            "warnings": self.warnings(),
            "error": None,
        }


def derive_actions(node):
    """只有拿到明确属性依据才声明动作；enabled 必须是 True。"""
    acts = []
    if not node.get("bounds_valid"):
        return acts
    if node.get("clickable") is True and node.get("enabled") is True:
        acts.append("click")
    if node.get("long_clickable") is True and node.get("enabled") is True:
        acts.append("long_click")
    if node.get("scrollable") is True and node.get("enabled") is True:
        acts.append("scroll")
    return acts


# --------------------------------------------------------------------------
# 输入 / 输出
# --------------------------------------------------------------------------

def load_observation(xml_path):
    p = Path(xml_path)
    if not p.exists():
        raise AdapterError("input_not_found", "XML file does not exist", 2, xml_path=str(p))
    if not p.is_file():
        raise AdapterError("input_not_a_file", "XML path is not a regular file", 2, xml_path=str(p))
    data = p.read_bytes()
    if not data.strip():
        raise AdapterError("xml_empty", "XML file is empty", 1, xml_path=str(p))
    try:
        return Observation(data, source_name=p.name)
    except ET.ParseError as exc:
        raise AdapterError("xml_parse_error", "XML could not be parsed: %s" % exc, 1, xml_path=str(p))
    except RecursionError:
        raise AdapterError("xml_too_deep", "XML nesting is too deep to parse safely", 1, xml_path=str(p))


def resolve_output_path(path_str):
    """只允许写本模块目录下的 .json，且拒绝覆盖非本工具产出的文件。"""
    base = Path(os.path.realpath(str(BASE_DIR)))
    target = Path(os.path.realpath(os.path.abspath(str(path_str))))
    try:
        target.relative_to(base)
    except ValueError:
        raise AdapterError(
            "output_path_outside_department",
            "observe may only write inside the module directory",
            2,
            target=str(target),
            allowed_dir=str(base),
        )
    if target.suffix.lower() != ".json":
        raise AdapterError(
            "output_extension_not_allowed",
            "observe only writes .json files",
            2,
            target=str(target),
        )
    if target.exists():
        if target.is_dir():
            raise AdapterError("output_is_directory", "output path is an existing directory", 2, target=str(target))
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            raise AdapterError(
                "refusing_to_overwrite_unknown_file",
                "existing file is not a JSON document produced by this tool",
                2,
                target=str(target),
            )
        gen = existing.get("generator") if isinstance(existing, dict) else None
        # 精确标识 + 基本结构校验（旧版 v1.0.0 的合法输出同样满足，见 USAGE）
        # 注意：这个标识只防误覆盖，不是来源认证——任何人都能写出同样的字段。
        structure_ok = (
            isinstance(existing, dict)
            and gen == GENERATOR
            and isinstance(existing.get("module_version"), str)
            and isinstance(existing.get("observation_id"), str)
            and isinstance(existing.get("nodes"), list)
            and existing.get("source") == "ui"
        )
        if not structure_ok:
            raise AdapterError(
                "refusing_to_overwrite_unknown_file",
                "existing JSON is not a recognized this-tool observation output "
                "(exact generator id + module_version + observation_id + nodes + source=ui required)",
                2,
                target=str(target),
                found_generator=gen if isinstance(gen, (str, type(None))) else repr(gen),
            )
    return target


def write_json_file(target, payload):
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    target.write_text(text, encoding="utf-8", newline="\n")


def emit(payload, exit_code=0):
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return exit_code


# --------------------------------------------------------------------------
# 命令 1：observe
# --------------------------------------------------------------------------

def cmd_observe(args):
    obs = load_observation(args.xml)
    target = resolve_output_path(args.output)
    payload = obs.to_json()
    write_json_file(target, payload)
    return emit(payload, 0)


# --------------------------------------------------------------------------
# 命令 2：resolve
# --------------------------------------------------------------------------

ACTION_EVIDENCE = {
    "click": ("clickable", "tap"),
    "long_click": ("long_clickable", "long_press"),
    "scroll": ("scrollable", "swipe"),
}


def _node_brief(obs, it):
    d = obs.node_dict(it)
    return {
        "node_id": d["node_id"],
        "class": d["class"],
        "text": d["text"],
        "content_desc": d["content_desc"],
        "package": d["package"],
        "bounds": d["bounds"],
        "enabled": d["enabled"],
        "clickable": d["clickable"],
        "long_clickable": d["long_clickable"],
        "scrollable": d["scrollable"],
        "actions": d["actions"],
    }


def cmd_resolve(args):
    obs = load_observation(args.xml)
    # 未知或非规范写法（"Click"）一律拒绝，不自动猜、不默默降级
    action = args.action if args.action is not None else "click"
    if action not in SUPPORTED_ACTIONS:
        raise AdapterError(
            "unsupported_action",
            "this adapter emits tap / long_press / swipe plans only; action must be one of supported_actions",
            1,
            requested_action=args.action,
            supported_actions=list(SUPPORTED_ACTIONS),
        )

    requested = obs.item_by_node_id(args.node_id)  # 会拒绝过期/未知 ID
    attr_name, plan_action = ACTION_EVIDENCE[action]

    # enabled 契约（v1.1.0 收紧）：请求节点、途中节点、动作目标都必须是 enabled=true。
    # enabled 为 false 或读不出（unknown）都不据此宣称可执行，也不跨越它们去点更高祖先。
    chain = obs.ancestor_chain(requested)  # requested -> root
    requested_enabled = norm_bool(requested.attr("enabled"))
    if requested_enabled is not True:
        raise AdapterError(
            "requested_node_disabled",
            "requested node is not enabled (disabled or unknown); refusing to plan an action",
            1,
            requested_node_id=args.node_id,
            requested_action=action,
            enabled_state=_enabled_state(requested_enabled),
        )

    target = None
    blocked = None
    if norm_bool(requested.attr(attr_name)) is True:
        target = requested                    # 请求节点自身即可执行该动作
    else:
        for it in chain[1:]:                  # 从请求节点的父节点开始向上
            if _is_dump_envelope(it):
                continue                       # 包裹元素不参与 enabled 契约
            if norm_bool(it.attr(attr_name)) is True:
                target = it                    # 最近的具备该动作属性的祖先（可能仍是禁用目标）
                break
            if norm_bool(it.attr("enabled")) is not True:
                blocked = it                   # 不能跨过这个 disabled/unknown 中间节点
                break

    if target is None and blocked is not None:
        raise AdapterError(
            "blocked_by_disabled_node",
            "an ancestor between the requested node and any action target is not enabled; "
            "refusing to climb past it",
            1,
            requested_node_id=args.node_id,
            requested_action=action,
            blocked_node_id=blocked.node_id,
            enabled_state=_enabled_state(norm_bool(blocked.attr("enabled"))),
            ancestor_chain=[it.node_id for it in chain],
            required_attribute=attr_name,
        )

    if target is None:
        raise AdapterError(
            "no_supported_target",
            "neither the node nor any ancestor on the real parent chain supports this action",
            1,
            requested_node_id=args.node_id,
            requested_action=action,
            ancestor_chain=[it.node_id for it in chain],
            required_attribute=attr_name,
        )

    tdict = obs.node_dict(target)
    if tdict["enabled"] is not True:
        raise AdapterError(
            "target_disabled",
            "nearest action-capable node on the real parent chain is not enabled; "
            "refusing to plan and refusing to climb past it",
            1,
            requested_node_id=args.node_id,
            target_node_id=tdict["node_id"],
            enabled=tdict["enabled"],
            enabled_state=_enabled_state(tdict["enabled"]),
        )
    if not tdict["bounds_valid"]:
        raise AdapterError(
            "invalid_bounds",
            "resolved target has no usable bounds; cannot derive plan coordinates",
            1,
            requested_node_id=args.node_id,
            target_node_id=tdict["node_id"],
            bounds_raw=tdict["bounds_raw"],
        )

    bounds = tdict["bounds"]
    x1, y1, x2, y2 = bounds
    cx, cy = center(bounds)

    if action == "scroll":
        direction = args.direction
        if direction not in SCROLL_DIRECTIONS:
            raise AdapterError(
                "missing_or_unsupported_direction",
                "scroll requires --direction up|down|left|right; direction is never guessed",
                1,
                requested_node_id=args.node_id,
                target_node_id=tdict["node_id"],
                requested_direction=direction,
                supported_directions=list(SCROLL_DIRECTIONS),
            )
        if (x2 - x1) < SNAP_AXIS_MIN or (y2 - y1) < SNAP_AXIS_MIN:
            raise AdapterError(
                "target_too_small",
                "target bounds are too small to derive an in-bounds swipe",
                1,
                target_node_id=tdict["node_id"],
                bounds=bounds,
            )
        w, h = x2 - x1, y2 - y1
        if direction == "up":
            plan = {"action": plan_action, "x1": cx, "y1": y1 + (h * 3) // 4, "x2": cx, "y2": y1 + h // 4}
        elif direction == "down":
            plan = {"action": plan_action, "x1": cx, "y1": y1 + h // 4, "x2": cx, "y2": y1 + (h * 3) // 4}
        elif direction == "left":
            plan = {"action": plan_action, "x1": x1 + (w * 3) // 4, "y1": cy, "x2": x1 + w // 4, "y2": cy}
        else:
            plan = {"action": plan_action, "x1": x1 + w // 4, "y1": cy, "x2": x1 + (w * 3) // 4, "y2": cy}
    else:
        plan = {"action": plan_action, "x": cx, "y": cy}

    rdict = obs.node_dict(requested)
    resolved_via = "self" if target is requested else "ancestor"
    # 只在目标祖先链上、靠目标最近的那一段做依据说明
    try:
        up_chain = chain[: chain.index(target) + 1]
    except ValueError:  # pragma: no cover - target 必来自 chain
        up_chain = [requested, target]

    payload = {
        "ok": True,
        "generator": GENERATOR,
        "module_version": MODULE_VERSION,
        "observation_id": obs.observation_id,
        "source_xml": obs.source_block(),
        "requested_node_id": rdict["node_id"],
        "requested_node": _node_brief(obs, requested),
        "action": action,
        "direction": args.direction if action == "scroll" else None,
        "target_node_id": tdict["node_id"],
        "target_node": dict(_node_brief(obs, target), subtree_texts=obs.subtree_texts(target)),
        "resolved_via": resolved_via,
        "hop_count": len(up_chain) - 1,
        "ancestor_chain": [it.node_id for it in up_chain],
        "bounds_used": bounds,
        "plan": plan,
        "plan_only": True,
        "error": None,
        "caveats": [
            "plan is derived from this XML snapshot and is not executed by this tool",
            "the node-id check proves the id matches this XML, not that the device UI is unchanged",
            "the executor must confirm the observation is still the latest before acting",
        ],
    }
    return emit(payload, 0)


# --------------------------------------------------------------------------
# 命令 3：evaluate-search
# --------------------------------------------------------------------------

def _subtree_labels(item, obs):
    """子树（含自身）里所有非空 text 与 content-desc，按文档序。"""
    out = []
    for sub in obs.subtree(item):
        for raw in (sub.attr("text"), sub.attr("content-desc")):
            s = norm_str(raw)
            if s:
                out.append(s)
    return out


def classify_node(obs, it, search_field_items, candidate_ids):
    """只用于给出"为什么这个文本不算结果"的依据。"""
    text = norm_str(it.attr("text"))
    desc = norm_str(it.attr("content-desc"))
    label = text or desc
    if not label:
        return None
    if it.node_id in search_field_items or any(
        obs.is_within(it, sf) for sf in search_field_items.values()
    ):
        return "search_input_echo"
    low = text.lower() if text else ""
    if text and low in NAV_LABELS:
        return "bottom_nav_label"
    # text 与 content-desc 都查，且要求整串精确相等（避免误伤含同名词的 App 介绍）
    if low in CHROME_ACTION_LABELS or (desc and desc.lower() in CHROME_ACTION_LABELS):
        return "toolbar_or_action_label"
    if text and EMPTY_STATE_RE.search(text):
        return "empty_state_text"
    row_id = candidate_ids.get(it.node_id)
    if row_id:
        return "row_content_of:%s" % row_id
    if norm_bool(it.attr("clickable")) is not True:
        return "not_a_clickable_row"
    return "clickable_without_result_structure"


def cmd_evaluate_search(args):
    obs = load_observation(args.xml)
    query = args.query
    if query is None or not query.strip():
        raise AdapterError("empty_query", "--query must not be empty", 2)
    q = query.strip().casefold()

    items = obs.items
    packages = obs.packages()
    app = packages[0] if len(packages) == 1 else None
    # 不认识的 App 不冒充页面理解：只报状态，不产出结果候选
    app_supported = bool(packages) and FDROID_PACKAGE in packages

    # 1) 搜索输入框（EditText = 真实属性依据）
    #    多个搜索框时无法可靠断定操作目标：不自动把树序第一个当成功，input_matches_query 保守为 false。
    fields = obs.search_fields()
    field_items = {it.node_id: it for it in fields}
    input_evidence = None
    input_exact = False
    input_substring = False
    field_count = len(fields)
    ambiguous = field_count > 1
    if fields:
        first = fields[0]
        raw = norm_str(first.attr("text")) or ""
        exact = raw.strip().casefold() == q
        sub = q in raw.strip().casefold()
        # 仅当只有一个搜索框时才把回显当作可采信的匹配依据
        input_exact, input_substring = (exact and not ambiguous), sub
        input_evidence = {
            "field_count": field_count,
            "ambiguous": ambiguous,
            "node_id": first.node_id,
            "class": norm_str(first.attr("class")),
            "raw_text": norm_str(first.attr("text")),
            "exact_match": exact,
            "substring_match": sub,
            "matches_query_node_ids": [
                it.node_id for it in fields
                if (norm_str(it.attr("text")) or "").strip().casefold() == q
            ],
            "all_search_field_node_ids": [it.node_id for it in fields],
        }
        if ambiguous:
            input_evidence["reason_if_ambiguous"] = (
                "more than one EditText search field is present; the operated field cannot be "
                "identified from the XML, so no match is claimed"
            )
    search_context = bool(fields)

    # 2) 空态
    empty_nodes = [
        it for it in items
        if EMPTY_STATE_RE.search(norm_str(it.attr("text")) or "")
    ]
    empty_present = bool(empty_nodes)
    empty_evidence = None
    if empty_nodes:
        e = empty_nodes[0]
        empty_evidence = {
            "node_id": e.node_id,
            "text": norm_str(e.attr("text")),
            "pattern": EMPTY_STATE_RE.pattern,
        }

    # 3) 真实结果候选：必须是可点击的列表行结构
    candidates = []
    candidate_ids = {}
    field_ids = set(field_items)
    empty_ids = {it.node_id for it in empty_nodes}
    if search_context and app_supported:
        provisional = []
        for it in items:
            if it.node_id in field_ids:
                continue
            if any(obs.is_within(it, sf) for sf in fields) or any(obs.is_within(sf, it) for sf in fields if sf is not it):
                continue
            if norm_bool(it.attr("clickable")) is not True:
                continue
            if norm_bool(it.attr("enabled")) is not True:
                continue
            if it.node_id in empty_ids or any(obs.is_within(it, e) for e in empty_nodes):
                continue
            texts = obs.subtree_texts(it)
            if not texts:
                continue
            labels = _subtree_labels(it, obs)   # text 与 content-desc 都算，含可点击父行的子节点
            # 行内出现底部导航标签 -> 不是结果行
            if any(l.lower() in NAV_LABELS for l in labels):
                continue
            # 行内出现整串动作标签（Cancel / Settings / OK ...）-> 工具栏/对话框按钮，
            # 在候选生成阶段就排除，不能只写进 excluded 却仍计数
            if any(l.lower() in CHROME_ACTION_LABELS for l in labels):
                continue
            if any(EMPTY_STATE_RE.search(t) for t in texts):
                continue
            provisional.append(it)
        # 若一个候选是另一个候选的后代，保留最外层行，避免同一行重复计数
        kept = []
        for it in provisional:
            if any(obs.is_within(it, other) for other in provisional if other is not it):
                continue
            kept.append(it)
        for it in kept:
            node = obs.node_dict(it)
            texts = obs.subtree_texts(it)
            label_ids = [
                sub.node_id for sub in obs.subtree(it) if norm_str(sub.attr("text"))
            ]
            candidates.append(
                {
                    "row_node_id": it.node_id,
                    "label": texts[0],
                    "label_node_id": label_ids[0] if label_ids else None,
                    "summary": texts[1] if len(texts) > 1 else None,
                    "summary_node_id": label_ids[1] if len(label_ids) > 1 else None,
                    "label_texts": texts,
                    "row_class": node["class"],
                    "bounds": node["bounds"],
                    "clickable": node["clickable"],
                    "enabled": node["enabled"],
                    "actions": node["actions"],
                }
            )
            for sub in obs.subtree(it):
                candidate_ids.setdefault(sub.node_id, it.node_id)

    # 4) 为何其它文本不算结果
    excluded = []
    if search_context and app_supported:
        for it in items:
            node = obs.node_dict(it)
            reason = classify_node(obs, it, field_items, candidate_ids)
            if reason:
                excluded.append(
                    {
                        "node_id": it.node_id,
                        "text": node["text"],
                        "content_desc": node["content_desc"],
                        "class": node["class"],
                        "reason": reason,
                    }
                )

    # 5) 判定
    #    content_present  = 这份页面上确实有结果候选（事实）
    #    content_verified = 只有"支持的明确搜索页 + 单一搜索框精确回显查询 + 无空态 + 有候选"才为 true
    warnings = []
    if not app_supported:
        page_kind = "unsupported_app"
        warnings.append(
            {
                "code": "not_fdroid_basic",
                "message": "this adapter only understands the F-Droid Basic search screen",
                "packages": packages,
            }
        )
    elif not search_context:
        page_kind = "not_a_search_screen"
        warnings.append(
            {
                "code": "no_search_input_field",
                "message": "no EditText search field found; page is not treated as an F-Droid search screen",
            }
        )
    elif candidates and empty_present:
        page_kind = "search_conflicting"
        warnings.append(
            {
                "code": "conflicting_evidence",
                "message": "both an empty-state text and list rows were found; not counted as verified",
            }
        )
    elif candidates:
        page_kind = "search_results"
    elif empty_present:
        page_kind = "search_empty_state"
    else:
        page_kind = "search_page_unrecognized"
        warnings.append(
            {
                "code": "no_result_structure",
                "message": "search screen without rows or empty-state text; nothing is claimed as content",
            }
        )

    content_present = bool(candidates)
    content_verified = bool(
        page_kind == "search_results"      # 已含：F-Droid 包名 + 有搜索框 + 有候选 + 无空态
        and candidates
        and not empty_present
        and input_exact                   # 单一搜索框且精确回显查询
    )
    if content_present and not content_verified:
        if ambiguous:
            warnings.append(
                {
                    "code": "multiple_search_fields",
                    "message": "several search fields are present, so the operated field cannot be "
                               "identified; content_present stays true but content_verified is false "
                               "on purpose",
                    "search_field_node_ids": [it.node_id for it in fields],
                }
            )
        else:
            warnings.append(
                {
                    "code": "query_not_confirmed",
                    "message": "rows are present on the page but the search input does not exactly "
                               "echo the query; content_present=true, content_verified=false",
                }
            )
    if empty_present and not candidates:
        warnings.append(
            {
                "code": "no_results_is_a_real_state",
                "message": "an empty result set is a legitimate search state, but it does not verify that target content was read",
            }
        )

    payload = {
        "ok": True,
        "generator": GENERATOR,
        "module_version": MODULE_VERSION,
        "observation_id": obs.observation_id,
        "source_xml": obs.source_block(),
        "app": app,
        "packages": packages,
        "query": query,
        "page_kind": page_kind,
        "coverage": COVERAGE,
        "input_matches_query": input_exact,
        "input_evidence": input_evidence,
        "real_result_candidates": candidates,
        "result_count": len(candidates),
        "empty_state": empty_present,
        "empty_state_evidence": empty_evidence,
        "content_present": content_present,
        "content_verified": content_verified,
        "content_verified_note": (
            "content_verified means: this supported F-Droid search screen shows result rows and its "
            "single search field exactly echoes the query. It does not prove the rows are fresh, nor "
            "that the network is reachable; row text has no field-level link to the query."
        ),
        "network_verified": "unknown",
        "network_note": "XML alone cannot prove the current network state; cached rows would look identical",
        "excluded": excluded,
        "warnings": warnings,
        "error": None,
    }
    return emit(payload, 0)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="content_adapter.py",
        description="Offline content adapter: UI Automator XML -> observation / action plan / search verdict.",
    )
    parser.add_argument("--version", action="version", version="%(prog)s " + MODULE_VERSION)
    sub = parser.add_subparsers(dest="command", metavar="command", required=True)

    p_obs = sub.add_parser("observe", help="parse an XML snapshot into a structured observation")
    p_obs.add_argument("--xml", required=True, help="path to a UI Automator hierarchy XML")
    p_obs.add_argument("--output", required=True, help="JSON path inside this module directory")
    p_obs.set_defaults(handler=cmd_observe)

    p_res = sub.add_parser("resolve", help="build an action plan from a node id")
    p_res.add_argument("--xml", required=True, help="path to a UI Automator hierarchy XML")
    p_res.add_argument("--node-id", required=True, dest="node_id", help="node id from observe")
    p_res.add_argument("--action", default="click", help="click | long_click | scroll (default: click)")
    p_res.add_argument("--direction", default=None, help="required for scroll: up | down | left | right")
    p_res.set_defaults(handler=cmd_resolve)

    p_ev = sub.add_parser("evaluate-search", help="F-Droid Basic search-result verification")
    p_ev.add_argument("--xml", required=True, help="path to a UI Automator hierarchy XML")
    p_ev.add_argument("--query", required=True, help="search term that was typed")
    p_ev.set_defaults(handler=cmd_evaluate_search)
    return parser


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except Exception:  # pragma: no cover - 老环境兜底
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except AdapterError as exc:
        sys.stderr.write("%s: %s\n" % (exc.code, exc.message))
        payload = {
            "ok": False,
            "generator": GENERATOR,
            "module_version": MODULE_VERSION,
            "command": getattr(args, "command", None),
            "error": exc.as_json(),
        }
        if getattr(args, "command", None) == "resolve":
            payload.update(
                {
                    "observation_id": None,
                    "requested_node_id": getattr(args, "node_id", None),
                    "target_node_id": None,
                    "plan": None,
                }
            )
        return emit(payload, exc.exit_code)
    except BrokenPipeError:  # pragma: no cover
        return 2


if __name__ == "__main__":
    sys.exit(main())
