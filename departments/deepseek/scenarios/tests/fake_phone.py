#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
假 phone CLI —— 离线测试专用（**不是**手机控制器，不接触设备/ADB）。

按 docs/phone-cli-contract-v0.1.md 的外形回答：公共参数在子命令前，
stdout 恒一条 JSON，失败时 ok=false + 非零退出；也可以故意返回坏 JSON、
拖时间或报错，用来测场景脚本的每个分支。

驱动方式（环境变量）：

* `FAKE_PHONE_SCRIPT`：脚本文件，`{"cursor": 0, "calls": [ {…}, … ]}`。
  每次调用消费 `calls[cursor]` 并把 cursor 写回，保证可断言“调了几次、调的什么”。
* `FAKE_PHONE_LOG`：每次调用追加一行 JSON（含完整 argv），供测试断言调用序列。

每条 call 的字段：`action`（可选，用于自检）、`ok`、`data`、`error`、`raw`（直接打印的
原始文本，用于坏 JSON 场景）、`exit`（退出码）、`delay`（秒，用于超时场景）。
"""

import json
import os
import sys
import time

COMMON_OPTS = ("--adb", "--state-file", "--output-dir")
ACTIONS = ("status", "launch", "observe", "click", "set-text", "evaluate-search", "screenshot")


def parse_argv(argv):
    """返回 (common, action, args, shape_ok)。公共参数必须全部出现在子命令之前。"""
    common = {}
    i = 0
    while i < len(argv):
        token = argv[i]
        if token in COMMON_OPTS:
            if i + 1 >= len(argv):
                return common, None, [], False
            common[token] = argv[i + 1]
            i += 2
            continue
        if token in ACTIONS:
            return common, token, argv[i + 1:], all(o in common for o in COMMON_OPTS)
        return common, None, argv[i:], False
    return common, None, [], False


def main():
    argv = sys.argv[1:]
    common, action, args, shape_ok = parse_argv(argv)
    spec_path = os.environ.get("FAKE_PHONE_SCRIPT")
    log_path = os.environ.get("FAKE_PHONE_LOG")
    if not spec_path:
        sys.stderr.write("FAKE_PHONE_SCRIPT not set\n")
        return 90

    spec = json.loads(open(spec_path, encoding="utf-8").read())
    calls = spec.get("calls") or []
    cursor = int(spec.get("cursor") or 0)

    if log_path:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "index": cursor + 1, "action": action, "args": args, "argv": argv,
                "common": common, "shape_ok": shape_ok,
            }, ensure_ascii=False) + "\n")

    if cursor >= len(calls):
        sys.stderr.write("fake: no scripted call left (cursor=%d)\n" % cursor)
        print(json.dumps({"ok": False, "action": action, "data": None,
                          "error": {"code": "invalid_argument",
                                    "message": "fake phone CLI ran out of scripted calls"},
                          "artifacts": []}))
        return 3

    entry = calls[cursor]
    spec["cursor"] = cursor + 1
    with open(spec_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(spec, ensure_ascii=False, indent=1))

    delay = float(entry.get("delay") or 0)
    if delay:
        time.sleep(delay)

    if entry.get("expect_action") and entry["expect_action"] != action:
        sys.stderr.write("fake: expected action %r, got %r\n" % (entry["expect_action"], action))
        print(json.dumps({"ok": False, "action": action, "data": None,
                          "error": {"code": "invalid_argument",
                                    "message": "fake: unexpected action"},
                          "artifacts": []}))
        return 3

    if "raw" in entry:
        sys.stdout.write(entry["raw"])
        sys.stdout.flush()
        return int(entry.get("exit") or 0)

    ok = entry.get("ok") is not False
    payload = {
        "ok": ok,
        "action": entry.get("action", action),
        "data": entry.get("data") if ok else None,
        "error": entry.get("error") if not ok else None,
        "artifacts": entry.get("artifacts") or [],
    }
    print(json.dumps(payload, ensure_ascii=False))
    if "exit" in entry:
        return int(entry["exit"])
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
