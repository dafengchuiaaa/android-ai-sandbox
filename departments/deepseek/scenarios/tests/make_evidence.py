#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
离线证据生成器：把几类典型情形跑一遍（假 phone CLI 驱动），把**输入 spec、假调用日志、
场景 stdout 与落盘报告**分case 存到 `evidence/offline/<case>/`。

不是设备工具、不是 harness：全程不接触 ADB/实例，只跑本目录的场景脚本 + 假 CLI。
运行：python tests/make_evidence.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIO_DIR = HERE.parent
sys.path.insert(0, str(HERE))
import fake_specs as fs  # noqa: E402

PY = sys.executable
SCENE = SCENARIO_DIR / "fdroid_search.py"
FAKE = HERE / "fake_phone.py"
OUT = SCENARIO_DIR / "evidence" / "offline"
WORK = SCENARIO_DIR / "evidence" / "_tmp_evidence"

RESULTS = fs.ORIG / "hierarchy-search-results.xml"
EMPTY = fs.ORIG / "hierarchy-20261015T024231-c934f0c7.xml"
MIN_RESULTS = fs.SYN / "synthetic-min-results.xml"
MULTI_FIELDS = fs.SYN / "synthetic-multi-search-fields.xml"


def cases():
    results_obs = fs.observation_payload(RESULTS)
    empty_obs = fs.observation_payload(EMPTY)
    mini_obs = fs.observation_payload(MIN_RESULTS)
    multi_obs = fs.observation_payload(MULTI_FIELDS)
    return [
        ("01-verified-5-rows", {}, "termux", [
            fs.status_ok(), fs.launch_ok(results_obs), fs.click_ok(results_obs),
            fs.set_text_ok(results_obs, "termux"),
            fs.evaluate_ok(fs.evaluate_payload(RESULTS, "termux")),
        ]),
        ("02-empty-state-not-success", {}, "termux", [
            fs.status_ok(), fs.launch_ok(empty_obs), fs.click_ok(empty_obs),
            fs.set_text_ok(empty_obs, "termux"),
            fs.evaluate_ok(fs.evaluate_payload(EMPTY, "termux")),
        ]),
        ("03-concat-readback-refused", {}, "termux", [
            fs.status_ok(), fs.launch_ok(results_obs), fs.click_ok(results_obs),
            fs.set_text_ok(results_obs, "termux", readback="termuxtermux", verified=False),
            fs.evaluate_ok(fs.evaluate_payload(RESULTS, "termux")),
        ]),
        ("04-stale-then-recovered", {}, "termux", [
            fs.status_ok(), fs.launch_ok(results_obs),
            fs.phone_error("click", "stale_observation", "observation changed"),
            {"action": "observe", "data": mini_obs},
            fs.click_ok(mini_obs), fs.set_text_ok(mini_obs, "termux"),
            fs.evaluate_ok(fs.evaluate_payload(MIN_RESULTS, "termux")),
        ]),
        ("05-repeated-stale-stops", {}, "termux", [
            fs.status_ok(), fs.launch_ok(results_obs),
            fs.phone_error("click", "stale_observation"),
            {"action": "observe", "data": mini_obs},
            fs.phone_error("click", "stale_observation"),
            fs.click_ok(mini_obs),
        ]),
        ("06-invalid-json-preserved", {}, "termux", [
            {"action": "status", "raw": "adb daemon starting...\nnot json at all\n", "exit": 0},
        ]),
        ("07-device-unavailable", {}, "termux", [
            fs.phone_error("status", "device_unavailable", "no emulator matches the state file"),
        ]),
        ("08-ambiguous-input-fields", {}, "termux", [
            fs.status_ok(), fs.launch_ok(multi_obs), fs.click_ok(multi_obs),
        ]),
        ("09-screenshot-on-failure", {"screenshot_on_failure": True}, "termux", [
            fs.status_ok(), fs.launch_ok(empty_obs), fs.click_ok(empty_obs),
            fs.set_text_ok(empty_obs, "termux"),
            fs.evaluate_ok(fs.evaluate_payload(EMPTY, "termux")),
            {"action": "screenshot", "data": {"reason": "fdroid_search failed: empty_state",
                                              "artifact": "fake-shot.png"}},
        ]),
        # dispatch/25 反例回归：矛盾字段（Codex 探针第 1 类）
        ("10-contradictory-count-invalid", {}, "termux", [
            fs.status_ok(), fs.launch_ok(results_obs), fs.click_ok(results_obs),
            fs.set_text_ok(results_obs, "termux"),
            fs.evaluate_ok(dict(fs.evaluate_payload(RESULTS, "termux"),
                                real_result_candidates=[], result_count=1)),
        ]),
        # dispatch/25 反例回归：布尔 result_count（Codex 探针第 3 类）
        ("11-boolean-count-invalid", {}, "termux", [
            fs.status_ok(), fs.launch_ok(results_obs), fs.click_ok(results_obs),
            fs.set_text_ok(results_obs, "termux"),
            fs.evaluate_ok(dict(fs.evaluate_payload(RESULTS, "termux"), result_count=True)),
        ]),
        # dispatch/25 绑定回归：观察缺 device_serial 不得跳过设备对比
        ("12-binding-missing-serial", {}, "termux", [
            fs.status_ok(),
            {"action": "launch",
             "data": {"package": "org.fdroid.basic",
                      "after_observation": {k: v for k, v in results_obs.items()
                                             if k != "device_serial"}}},
            fs.click_ok(results_obs),
        ]),
        # dispatch/25 读回回归：多一个空格不算精确读回
        ("13-readback-extra-space", {}, "termux", [
            fs.status_ok(), fs.launch_ok(results_obs), fs.click_ok(results_obs),
            fs.set_text_ok(results_obs, "termux", readback="termux "),
            fs.evaluate_ok(fs.evaluate_payload(RESULTS, "termux")),
        ]),
    ]


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK / "bin").mkdir(parents=True, exist_ok=True)
    (WORK / "bin" / "adb.exe").write_text("stub-not-a-real-adb", encoding="utf-8")
    (WORK / "bin" / "instance-abc.json").write_text('{"avd":"ai-sandbox-baseline"}', encoding="utf-8")

    summary = []
    for name, opts, query, calls in cases():
        case_dir = OUT / name
        shutil.rmtree(case_dir, ignore_errors=True)
        case_dir.mkdir(parents=True, exist_ok=True)
        spec = fs.write_spec(case_dir / "fake-spec.json", calls)
        log = case_dir / "fake-calls.jsonl"
        log.write_text("", encoding="utf-8")
        argv = [
            PY, str(SCENE),
            "--phone-cli", str(FAKE), "--python", PY,
            "--adb", str(WORK / "bin" / "adb.exe"),
            "--state-file", str(WORK / "bin" / "instance-abc.json"),
            "--session-dir", str(WORK / "session"),
            "--query", query,
            "--report-dir", str(case_dir),
        ]
        if opts.get("screenshot_on_failure"):
            argv.append("--screenshot-on-failure")
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              env=fs.spawn_env(spec, log))
        (case_dir / "scenario-stdout.json").write_text(proc.stdout, encoding="utf-8", newline="\n")
        payload = json.loads(proc.stdout)
        data = payload.get("data") or {}
        report = data.get("report_path")
        summary.append({
            "case": name,
            "exit_code": proc.returncode,
            "ok": payload.get("ok"),
            "outcome": data.get("outcome"),
            "error_code": (payload.get("error") or {}).get("code"),
            "calls": (data.get("calls") or {}).get("sequence"),
            "report_file": Path(report).name if report else None,
        })
        print("%-28s rc=%s ok=%-5s outcome=%-13s error=%s" % (
            name, proc.returncode, payload.get("ok"), data.get("outcome"),
            (payload.get("error") or {}).get("code")))

    (OUT / "SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")
    shutil.rmtree(WORK, ignore_errors=True)
    print("evidence written to", OUT)


if __name__ == "__main__":
    main()
