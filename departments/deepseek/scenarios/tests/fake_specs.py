#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线测试工具：把**真实**快照通过已验 DS-01 适配器的内存 API 变成 phone CLI 形状的
假响应（观察 / evaluate-search），再写成假 phone CLI 的脚本。

说明：
* 只用 `content_adapter.py` 的**内存 API**（`Observation` / `main`），不写它的目录、
  不改它的文件、不绕过它的路径防护。
* 节点 id、observation_id 都是真实 XML 的确定性结果，不是手编的；
  这样假 CLI 回放的是"真数据形状的假响应"，测试仍然明确标注为**离线合成**。
* import 顺序安全：测试运行时把 `departments/deepseek` 加入 sys.path。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from pathlib import Path

SCENARIO_DIR = Path(__file__).resolve().parent.parent          # scenarios/
DEEPSEEK_DIR = SCENARIO_DIR.parent                             # departments/deepseek
if str(DEEPSEEK_DIR) not in sys.path:
    sys.path.insert(0, str(DEEPSEEK_DIR))

import content_adapter as ca  # noqa: E402

ORIG = DEEPSEEK_DIR / "fixtures" / "originals"
SYN = DEEPSEEK_DIR / "fixtures" / "synthetic"

DEVICE_SERIAL = "emulator-5554"
AVD = "ai-sandbox-baseline"
OBSERVED_AT = "2026-10-15T08:00:00Z"


def observation_payload(xml_path, observed_at=OBSERVED_AT, serial=DEVICE_SERIAL):
    """把真实 XML 变成契约里 observe / after_observation 的形状。"""
    xml_path = Path(xml_path)
    obs = ca.Observation(xml_path.read_bytes(), source_name=xml_path.name)
    d = obs.to_json()
    return {
        "observation_id": d["observation_id"],
        "nodes": d["nodes"],
        "device_serial": serial,
        "avd": AVD,
        "observed_at": observed_at,
        "coverage": "partial",
        "xml_path": "<offline-fixture>/%s" % xml_path.name,
        "apps": d["apps"],
        "warnings": d["warnings"],
    }


def evaluate_payload(xml_path, query, serial=DEVICE_SERIAL):
    """跑一次真实适配器的 evaluate-search（内存调用，不落盘），补上设备字段。"""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ca.main(["evaluate-search", "--xml", str(xml_path), "--query", query])
    d = json.loads(buf.getvalue())
    d["device_serial"] = serial
    d["observed_at"] = OBSERVED_AT
    return d


def node_id_with_text(payload, text):
    for n in payload["nodes"]:
        if n.get("text") == text:
            return n["node_id"]
    raise AssertionError("no node with text %r" % text)


def node_id_of_class_suffix(payload, suffix):
    hits = [n["node_id"] for n in payload["nodes"] if (n.get("class") or "").endswith(suffix)]
    if len(hits) != 1:
        raise AssertionError("expected exactly one %s node, got %d" % (suffix, len(hits)))
    return hits[0]


def status_ok(ready=True, serial=DEVICE_SERIAL, avd=AVD):
    return {"action": "status", "data": {"ready": ready, "device_serial": serial, "avd": avd}}


def launch_ok(observation):
    return {"action": "launch", "data": {"package": "org.fdroid.basic", "after_observation": observation}}


def click_ok(observation, target_node_id="stub"):
    return {
        "action": "click",
        "data": {
            "target_node_id": target_node_id,
            "plan": {"action": "tap", "x": 1, "y": 1},
            "after_observation": observation,
        },
    }


def set_text_ok(observation, value, readback=None, verified=True):
    return {
        "action": "set-text",
        "data": {
            "input_text": value,
            "readback_text": value if readback is None else readback,
            "verified": verified,
            "after_observation": observation,
        },
    }


def evaluate_ok(payload):
    return {"action": "evaluate-search", "data": payload}


def phone_error(action, code, message="scripted failure"):
    return {"action": action, "ok": False, "error": {"code": code, "message": message}, "exit": 3}


def write_spec(path, calls):
    path = Path(path)
    path.write_text(json.dumps({"cursor": 0, "calls": calls}, ensure_ascii=False, indent=1),
                    encoding="utf-8", newline="\n")
    return path


def read_log(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_spec(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def spawn_env(spec_path, log_path):
    env = dict(os.environ)
    env["FAKE_PHONE_SCRIPT"] = str(spec_path)
    env["FAKE_PHONE_LOG"] = str(log_path)
    return env
