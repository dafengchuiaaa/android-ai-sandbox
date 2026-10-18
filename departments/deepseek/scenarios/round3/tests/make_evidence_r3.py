#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round3 离线证据生成器：跑遍"正常 + 反例 + 解析模式"十类情形（假 sandbox 驱动），
把输入脚本、假调用日志、stdout 与落盘报告分别存到 `evidence/offline/<case>/`。

不是设备工具：不接触 adb/实例，只跑本目录脚本 + 假 CLI。
运行：python tests/make_evidence_r3.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND3_DIR = HERE.parent
sys.path.insert(0, str(HERE))
import fake_specs_r3 as f3  # noqa: E402

PY = sys.executable
SCENARIO = ROUND3_DIR / "fdroid_details.py"
OUT = ROUND3_DIR / "evidence" / "offline"
WORK = ROUND3_DIR / "evidence" / "_tmp_evidence"


def scenario_calls():
    page = f3.observation_payload(f3.RESULTS_TERMUX)
    detail = f3.observation_payload(f3.DETAIL_TERMUX)
    return [
        ("01-unique-exact-detail", "run", {"label": "Termux", "query": "termux"},
         f3.search_flow("termux", f3.RESULTS_TERMUX, details_page=f3.DETAIL_TERMUX)),
        ("02-still-search-list", "run", {"label": "Termux", "query": "termux"},
         f3.search_flow("termux", f3.RESULTS_TERMUX)),
        ("03-near-names-real-page", "run", {"label": "Termux", "query": "termux"},
         f3.search_flow("termux", f3.RESULTS_REAL, details_page=f3.DETAIL_TERMUX)),
        ("04-duplicate-label", "run", {"label": "Termux", "query": "termux"},
         f3.search_flow("termux", f3.RESULTS_DUPLICATE, details_page=f3.DETAIL_TERMUX)),
        ("05-title-only-no-evidence", "run", {"label": "Termux", "query": "termux"},
         f3.search_flow("termux", f3.RESULTS_TERMUX, details_page=f3.TITLE_ONLY)),
        ("06-stale-then-recovered", "run", {"label": "Termux", "query": "termux"}, [
            f3.status_ok(), f3.launch_ok(page), f3.click_ok(page),
            f3.set_text_ok(page, "termux"),
            f3.evaluate_ok(f3.evaluate_payload(f3.RESULTS_TERMUX, "termux")),
            f3.phone_error("click", "stale_observation", "observation changed"),
            {"action": "observe", "data": page},
            f3.click_ok(detail),
        ]),
        ("07-repeated-stale-stops", "run", {"label": "Termux", "query": "termux"}, [
            f3.status_ok(), f3.launch_ok(page), f3.click_ok(page),
            f3.set_text_ok(page, "termux"),
            f3.evaluate_ok(f3.evaluate_payload(f3.RESULTS_TERMUX, "termux")),
            f3.phone_error("click", "stale_observation"),
            {"action": "observe", "data": page},
            f3.phone_error("click", "stale_observation"),
            f3.click_ok(detail),
        ]),
        ("08-per-call-timeout", "run", {"label": "Termux", "query": "termux", "extra": [
            "--per-call-timeout", "0.7"]}, [
            {"action": "status", "data": {"ready": True, "device_serial": f3.DEVICE_SERIAL,
                                          "avd": f3.AVD}, "delay": 3.0},
            f3.status_ok(),
        ]),
        ("09-parse-real-search-page", "parse",
         {"label": "Termux", "observation": f3.observation_payload(f3.RESULTS_REAL)}, None),
        ("10-parse-synthetic-detail", "parse",
         {"label": "Termux", "observation": f3.observation_payload(f3.DETAIL_TERMUX)}, None),
    ]


def main():
    shutil.rmtree(WORK, ignore_errors=True)
    (WORK).mkdir(parents=True, exist_ok=True)
    (WORK / "adb.exe").write_text("stub-not-a-real-adb", encoding="utf-8")
    (WORK / "instance.json").write_text(
        json.dumps({"avd": f3.AVD, "serial": f3.DEVICE_SERIAL}), encoding="utf-8")

    summary = []
    for name, mode, opts, calls in scenario_calls():
        case_dir = OUT / name
        shutil.rmtree(case_dir, ignore_errors=True)
        case_dir.mkdir(parents=True, exist_ok=True)
        env = dict()
        if mode == "run":
            spec = f3.write_spec(case_dir / "fake-spec.json", calls)
            log = case_dir / "fake-calls.jsonl"
            log.write_text("", encoding="utf-8")
            env = f3.spawn_env(spec, log)
            argv = [PY, str(SCENARIO), "--sandbox", str(f3.FAKE_CLI), "--python", PY,
                    "--adb", str(WORK / "adb.exe"),
                    "--state-file", str(WORK / "instance.json"),
                    "--session-dir", str(WORK / "session"),
                    "--query", opts["query"], "--label", opts["label"],
                    "--report-dir", str(case_dir)] + list(opts.get("extra") or [])
        else:
            saved = case_dir / "saved-observation.json"
            saved.write_text(json.dumps(opts["observation"], ensure_ascii=False), encoding="utf-8")
            argv = [PY, str(SCENARIO), "--parse-observation", str(saved),
                    "--label", opts["label"], "--report-dir", str(case_dir)]
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", env=env or None)
        (case_dir / "scenario-stdout.json").write_text(proc.stdout, encoding="utf-8", newline="\n")
        payload = json.loads(proc.stdout)
        data = payload.get("data") or {}
        (case_dir / "scenario-stderr.txt").write_text(proc.stderr, encoding="utf-8", newline="\n")
        summary.append({
            "case": name,
            "mode": mode,
            "exit_code": proc.returncode,
            "ok": payload.get("ok"),
            "outcome": data.get("outcome"),
            "stage": data.get("stage"),
            "error_code": (payload.get("error") or {}).get("code"),
            "calls": (data.get("calls") or {}).get("sequence"),
            "report_file": Path(data["report_path"]).name if data.get("report_path") else None,
        })
        print("%-28s rc=%s outcome=%-22s stage=%-16s error=%s" % (
            name, proc.returncode, data.get("outcome"), data.get("stage"),
            (payload.get("error") or {}).get("code")))

    (OUT / "SUMMARY.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8", newline="\n")
    shutil.rmtree(WORK, ignore_errors=True)
    print("evidence written to", OUT)


if __name__ == "__main__":
    main()
