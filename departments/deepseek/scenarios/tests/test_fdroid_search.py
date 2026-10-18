#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fdroid_search.py 的离线测试（标准库 unittest）。

全部用**假 phone CLI**（`tests/fake_phone.py` + `tests/fake_specs.py`）驱动：
不接触设备、不运行 ADB、不依赖 `departments/antigravity/**`（本轮尚未交付），
不修改已验收的 `content_adapter.py`。

假响应里的 observation 与 evaluate-search 数据由**真实 XML 快照**经适配器内存 API
生成（节点 id 是真实 hash），但它们仍然是**离线合成**输入，不是实机结果。

运行：
    python -m unittest discover -s tests -v
    python tests/test_fdroid_search.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCENARIO_DIR = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(SCENARIO_DIR) not in sys.path:
    sys.path.insert(0, str(SCENARIO_DIR))

import fake_specs as fs  # noqa: E402
import fdroid_search as fsearch  # noqa: E402

PY = sys.executable
SCENE = str(SCENARIO_DIR / "fdroid_search.py")
FAKE = str(HERE / "fake_phone.py")
TMP_ROOT = SCENARIO_DIR / "evidence" / "_tmp"       # 在 scenarios/ 内，符合报告目录策略

RESULTS = fs.ORIG / "hierarchy-search-results.xml"   # 真实：搜 termux 的 5 条结果页
EMPTY = fs.ORIG / "hierarchy-20261015T024231-c934f0c7.xml"   # 真实：空态页
HOME = fs.ORIG / "hierarchy-home.xml"                # 真实：首页（无 EditText，有 Search 标签）
SETTINGS = fs.ORIG / "hierarchy-20261015T030637-30a5f3db.xml"  # 真实：设置页（无 Search）
MIN_RESULTS = fs.SYN / "synthetic-min-results.xml"   # 合成：1 条结果 + 1 个输入框
MULTI_FIELDS = fs.SYN / "synthetic-multi-search-fields.xml"    # 合成：2 个输入框


class ScenarioCase(unittest.TestCase):
    """公共脚手架：临时目录 + 假 CLI 驱动。"""

    def setUp(self):
        self.tmp = TMP_ROOT / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.adb = self.tmp / "adb.exe"
        self.adb.write_text("stub-not-a-real-adb", encoding="utf-8")
        self.state = self.tmp / "instance-abc.json"
        self.state.write_text('{"avd":"ai-sandbox-baseline"}', encoding="utf-8")
        self.log = self.tmp / "fake.log"
        self.report_dir = self.tmp / "reports"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 驱动 ---------------------------------------------------------
    def run_scenario(self, calls, query="termux", extra=(), spec_name="spec.json",
                     report_dir=None, write_log=True):
        spec = fs.write_spec(self.tmp / spec_name, calls)
        if write_log:
            self.log.write_text("", encoding="utf-8")
        argv = [
            PY, SCENE,
            "--phone-cli", FAKE,
            "--python", PY,
            "--adb", str(self.adb),
            "--state-file", str(self.state),
            "--session-dir", str(self.tmp / "session"),
            "--query", query,
            "--report-dir", str(report_dir if report_dir is not None else self.report_dir),
        ] + [str(a) for a in extra]
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              env=fs.spawn_env(spec, self.log))
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, payload, fs.read_log(self.log)

    @staticmethod
    def node_and_obs_args(entry):
        args = entry["args"]
        oid = args[args.index("--observation-id") + 1]
        nid = args[args.index("--node-id") + 1]
        return oid, nid

    def assertNodeBelongsToObservation(self, entry):
        """动作里的 node_id 必须属于同一个观察（前缀 = observation hash 前 12 位）。"""
        oid, nid = self.node_and_obs_args(entry)
        self.assertTrue(nid.startswith(oid[:12] + ":"), (oid, nid))


# --------------------------------------------------------------------------
# 正常路径
# --------------------------------------------------------------------------

class HappyPathTest(ScenarioCase):
    def test_normal_five_rows_are_verified(self):
        obs = fs.observation_payload(RESULTS)
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "termux"), fs.evaluate_ok(fs.evaluate_payload(RESULTS, "termux")),
        ])
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "fdroid_search")
        data = payload["data"]
        self.assertEqual(data["outcome"], "verified")
        self.assertTrue(data["target_verified"])
        self.assertTrue(data["scenario_completed"])
        self.assertEqual([s["action"] for s in data["steps"]],
                         ["status", "launch", "click", "set-text", "evaluate-search"])
        self.assertEqual(data["verification"]["result_count"], 5)
        self.assertTrue(data["verification"]["content_verified"])
        self.assertTrue(data["verification"]["input_matches_query"])
        self.assertFalse(data["verification"]["empty_state"])
        self.assertEqual(data["verification"]["network_verified"], "unknown")
        self.assertEqual(data["input"]["readback_text"], "termux")
        self.assertEqual(data["limits"]["stale_recovery_used"], 0)
        # 每个动作的 node_id 都属于对应观察
        for entry in log:
            if entry["action"] in ("click", "set-text"):
                self.assertNodeBelongsToObservation(entry)
        # 报告落盘且带 steps
        report = Path(data["report_path"])
        self.assertTrue(report.is_file())
        self.assertTrue(report.is_relative_to(SCENARIO_DIR))
        saved = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(saved["outcome"], "verified")
        self.assertEqual(len(saved["steps"]), 5)

    def test_search_entry_path_is_structural(self):
        """没有 EditText 时先按结构点开唯一 Search 入口，再用新观察重新定位输入框。"""
        home = fs.observation_payload(HOME)
        page = fs.observation_payload(MIN_RESULTS)
        search_entry_id = fs.node_id_with_text(home, "Search")
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(home),
            fs.click_ok(page), fs.click_ok(page), fs.set_text_ok(page, "termux"),
            fs.evaluate_ok(fs.evaluate_payload(MIN_RESULTS, "termux")),
        ])
        self.assertEqual(rc, 0, payload)
        data = payload["data"]
        self.assertEqual(data["outcome"], "verified")
        self.assertEqual([s["action"] for s in data["steps"]],
                         ["status", "launch", "click", "click", "set-text", "evaluate-search"])
        # 第一次 click 用的是首页观察里那个 Search 标签节点
        first_click = [e for e in log if e["action"] == "click"][0]
        self.assertIn(search_entry_id, first_click["args"])
        self.assertTrue(first_click["args"][first_click["args"].index("--observation-id") + 1]
                        .startswith(home["observation_id"][:12]))
        # 第二次 click（聚焦输入框）用的是**新观察**里的输入框，不是首页的节点
        second_click = [e for e in log if e["action"] == "click"][1]
        self.assertNodeBelongsToObservation(second_click)
        self.assertEqual(data["located"]["search_entry_node_id"], search_entry_id)
        self.assertEqual(
            data["located"]["focus_click_node_id"],
            fs.node_id_of_class_suffix(page, "EditText"),
        )
        # 全程不给坐标：argv 里没有任何纯数字参数（没有 tap/swipe 计划）
        for entry in log:
            for token in entry["argv"]:
                self.assertFalse(token.lstrip("-").isdigit(), entry["argv"])


# --------------------------------------------------------------------------
# 未验证 / 失败判定
# --------------------------------------------------------------------------

class VerdictTest(ScenarioCase):
    def test_empty_state_is_reported_as_not_success(self):
        obs = fs.observation_payload(EMPTY)
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "termux"), fs.evaluate_ok(fs.evaluate_payload(EMPTY, "termux")),
        ])
        self.assertEqual(rc, 1)                       # 跑完了，但不是成功
        self.assertFalse(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["outcome"], "empty_state")
        self.assertTrue(data["scenario_completed"])
        self.assertFalse(data["target_verified"])
        self.assertFalse(data["verification"]["content_verified"])
        self.assertTrue(data["verification"]["empty_state"])
        self.assertEqual(data["verification"]["result_count"], 0)
        self.assertEqual(data["verification"]["network_verified"], "unknown")
        self.assertIsNone(payload["error"])           # 不是错误，只是未验证

    def test_wrong_query_keeps_candidates_but_not_verified(self):
        """evaluate-search 报 content_present 但查询未确认：不得当成功。"""
        obs = fs.observation_payload(RESULTS)
        ev = dict(fs.evaluate_payload(RESULTS, "fdroid"))
        rc, payload, _ = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "fdroid"), fs.evaluate_ok(ev),
        ], query="fdroid")
        self.assertEqual(rc, 1)
        data = payload["data"]
        self.assertEqual(data["outcome"], "not_verified")
        self.assertEqual(data["verification"]["result_count"], 5)
        self.assertTrue(data["verification"]["content_verified"] is False)
        self.assertTrue(any(w["code"] == "adapter:query_not_confirmed" for w in data["warnings"]))

    def test_concatenated_readback_is_rejected(self):
        """termuxtermux 读回不算成功：验证失败，且不会继续 evaluate-search。"""
        obs = fs.observation_payload(RESULTS)
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "termux", readback="termuxtermux", verified=False),
            fs.evaluate_ok(fs.evaluate_payload(RESULTS, "termux")),
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "verification_failed")
        self.assertEqual(payload["error"]["detail"]["readback_text"], "termuxtermux")
        self.assertNotIn("evaluate-search", [e["action"] for e in log])
        self.assertEqual([s["action"] for s in payload["data"]["steps"]],
                         ["status", "launch", "click", "set-text"])


# --------------------------------------------------------------------------
# stale 恢复（只允许一次）
# --------------------------------------------------------------------------

class StaleTest(ScenarioCase):
    def test_stale_then_recovery_succeeds(self):
        first = fs.observation_payload(RESULTS)
        second = fs.observation_payload(MIN_RESULTS)     # 重新观察后是"另一份"观察
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(first),
            fs.phone_error("click", "stale_observation", "observation changed"),
            {"action": "observe", "data": second},
            fs.click_ok(second), fs.set_text_ok(second, "termux"),
            fs.evaluate_ok(fs.evaluate_payload(MIN_RESULTS, "termux")),
        ])
        self.assertEqual(rc, 0, payload)
        data = payload["data"]
        self.assertEqual(data["outcome"], "verified")
        self.assertEqual(data["limits"]["stale_recovery_used"], 1)
        self.assertEqual([s["action"] for s in data["steps"]],
                         ["status", "launch", "click", "observe", "click", "set-text", "evaluate-search"])
        stale_step = data["steps"][2]
        self.assertEqual(stale_step["error_code"], "stale_observation")
        # 重试后的动作必须使用**重新观察**得到的 node_id / observation_id
        retry_click = [e for e in log if e["action"] == "click"][1]
        oid, nid = self.node_and_obs_args(retry_click)
        self.assertEqual(oid, second["observation_id"])
        self.assertEqual(nid, fs.node_id_of_class_suffix(second, "EditText"))
        self.assertEqual(data["located"]["focus_click_node_id"], nid)

    def test_repeated_stale_stops_without_loop(self):
        first = fs.observation_payload(RESULTS)
        second = fs.observation_payload(MIN_RESULTS)
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(first),
            fs.phone_error("click", "stale_observation"),
            {"action": "observe", "data": second},
            fs.phone_error("click", "stale_observation"),
            fs.click_ok(second),
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "stale_observation")
        clicks = [e for e in log if e["action"] == "click"]
        self.assertEqual(len(clicks), 2, "只允许一次重定位，不得第三次点击")
        self.assertNotIn("set-text", [e["action"] for e in log])
        self.assertEqual(payload["data"]["limits"]["stale_recovery_used"], 1)
        self.assertTrue(any(w["code"] == "stale_not_recoverable" for w in payload["data"]["warnings"]))


# --------------------------------------------------------------------------
# 定位：不明确就明确失败
# --------------------------------------------------------------------------

class LocationTest(ScenarioCase):
    def test_multiple_input_fields_are_refused(self):
        obs = fs.observation_payload(MULTI_FIELDS)
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "termux"), fs.evaluate_ok(fs.evaluate_payload(MULTI_FIELDS, "termux")),
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "input_field_ambiguous")
        self.assertEqual(len(payload["error"]["detail"]["field_node_ids"]), 2)
        self.assertEqual([e["action"] for e in log], ["status", "launch"], "不得猜第一个输入框")

    def test_missing_search_entry_is_refused(self):
        obs = fs.observation_payload(SETTINGS)     # 设置页：无 EditText、无 Search 标签
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "search_entry_not_found")
        self.assertEqual([e["action"] for e in log], ["status", "launch"])

    def test_ambiguous_search_entries_are_refused(self):
        two = self.tmp / "two-search-labels.xml"
        two.write_text(
            "<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation=\"0\">"
            "<node index=\"0\" class=\"android.widget.FrameLayout\" package=\"org.fdroid.basic\""
            " clickable=\"false\" enabled=\"true\" bounds=\"[0,0][720,1184]\">"
            "<node index=\"0\" class=\"android.view.View\" package=\"org.fdroid.basic\""
            " text=\"Search\" clickable=\"true\" enabled=\"true\" bounds=\"[0,0][720,100]\"/>"
            "<node index=\"1\" class=\"android.view.View\" package=\"org.fdroid.basic\""
            " content-desc=\"Search\" clickable=\"true\" enabled=\"true\" bounds=\"[0,100][720,200]\"/>"
            "</node></hierarchy>", encoding="utf-8")
        obs = fs.observation_payload(two)
        rc, payload, _ = self.run_scenario([fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs)])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "search_entry_ambiguous")
        self.assertEqual(len(payload["error"]["detail"]["entry_node_ids"]), 2)


# --------------------------------------------------------------------------
# phone CLI 协议异常与设备问题
# --------------------------------------------------------------------------

class ProtocolTest(ScenarioCase):
    def test_invalid_json_is_rejected_and_preserved(self):
        rc, payload, _ = self.run_scenario([
            {"action": "status", "raw": "adb daemon starting...\nnot json at all\n", "exit": 0},
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "invalid_json_from_phone")
        self.assertIn("adb daemon starting", payload["error"]["detail"]["stdout_head"])
        first_step = payload["data"]["steps"][0]
        self.assertTrue(first_step["raw_stdout"].strip().startswith("adb daemon"))
        self.assertEqual(first_step["error_code"], "invalid_json_from_phone")
        self.assertIsNone(payload["data"]["device"])

    def test_envelope_inconsistency_is_rejected(self):
        cases = [
            ({"action": "status", "ok": True,
              "data": {"ready": True, "device_serial": fs.DEVICE_SERIAL}, "exit": 7},
             "ok=true 但退出码非零"),
            ({"action": "status", "ok": False, "error": {"code": "device_unavailable",
                                                         "message": "gone"}, "exit": 0},
             "ok=false 但退出码为 0"),
        ]
        for i, (entry, label) in enumerate(cases):
            with self.subTest(label):
                rc, payload, _ = self.run_scenario([entry], spec_name="spec-%d.json" % i)
                self.assertEqual(rc, 3, label)
                self.assertEqual(payload["error"]["code"], "invalid_phone_response", label)

    def test_device_unavailable_stops_before_launch(self):
        rc, payload, log = self.run_scenario([
            fs.phone_error("status", "device_unavailable", "no emulator matching the state file"),
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "device_unavailable")
        self.assertEqual([e["action"] for e in log], ["status"])

    def test_driver_timeout_does_not_retry(self):
        rc, payload, log = self.run_scenario([
            {"action": "status", "data": {"ready": True, "device_serial": fs.DEVICE_SERIAL},
             "delay": 3.0},
            fs.status_ok(),
        ], extra=["--per-call-timeout", "0.6"])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "driver_timeout")
        self.assertEqual([e["action"] for e in log], ["status"])
        self.assertTrue(payload["data"]["steps"][0]["timed_out"])
        self.assertLess(payload["data"]["calls"]["elapsed_ms"], 8000)

    def test_ownership_mismatch_is_detected(self):
        obs = fs.observation_payload(RESULTS)
        ev = fs.evaluate_payload(RESULTS, "termux", serial="emulator-9998")
        rc, payload, _ = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "termux"), fs.evaluate_ok(ev),
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "ownership_mismatch")
        self.assertEqual(payload["error"]["detail"]["reported_serial"], "emulator-9998")
        self.assertEqual(payload["error"]["detail"]["kind"], "evaluate-search")


# --------------------------------------------------------------------------
# 预算、超时与报告目录策略
# --------------------------------------------------------------------------

class GuardTest(ScenarioCase):
    def test_call_budget_is_enforced(self):
        obs = fs.observation_payload(RESULTS)
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
        ], extra=["--max-calls", "3"])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "call_budget_exhausted")
        self.assertEqual(payload["data"]["calls"]["count"], 3)
        self.assertEqual([e["action"] for e in log], ["status", "launch", "click"])
        self.assertEqual(payload["data"]["calls"]["max_calls"], 3)

    def test_total_timeout_guard_stops_before_calling(self):
        cfg = argparse.Namespace(
            total_timeout_s=0.001, call_timeout_s=5.0, max_calls=12,
            python=PY, phone_cli=FAKE, adb=str(self.adb), state_file=str(self.state),
            session_dir=str(self.tmp / "session"), expect_avd=fs.AVD,
        )
        session = fsearch.PhoneSession(cfg)
        with self.assertRaises(fsearch.ScenarioError) as ctx:
            session.call("status")
        self.assertEqual(ctx.exception.code, "total_timeout")
        self.assertEqual(session.steps, [], "不该在预算耗尽后再发起调用")

    def test_report_dir_outside_scenarios_is_refused(self):
        outside = SCENARIO_DIR / ".." / "_should_not_exist_reports"
        rc, payload, log = self.run_scenario([fs.status_ok()], report_dir=outside)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["error"]["code"], "report_dir_outside_scenario_dir")
        self.assertEqual(log, [], "参数不合法时不得调用 phone CLI")
        self.assertFalse(outside.exists())

    def test_invalid_arguments_do_not_touch_the_phone_cli(self):
        cases = [
            ("query with illegal characters", {"--query": "term ux!"}),
            ("empty query", {"--query": ""}),
            ("missing adb file", {"--adb": str(self.tmp / "missing-adb.exe")}),
            ("missing phone CLI", {"--phone-cli": str(self.tmp / "missing-phone.py")}),
            ("zero call budget", {"--max-calls": "0"}),
            ("zero total timeout", {"--total-timeout": "0"}),
        ]
        for i, (label, override) in enumerate(cases):
            with self.subTest(label):
                spec = fs.write_spec(self.tmp / ("bad-%d.json" % i), [fs.status_ok()])
                self.log.write_text("", encoding="utf-8")
                opts = {
                    "--phone-cli": FAKE, "--python": PY, "--adb": str(self.adb),
                    "--state-file": str(self.state), "--session-dir": str(self.tmp / "s"),
                    "--query": "termux", "--report-dir": str(self.report_dir),
                }
                opts.update(override)
                argv = [PY, SCENE]
                for flag, value in opts.items():
                    argv += [flag, value]
                proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                      env=fs.spawn_env(spec, self.log))
                self.assertEqual(proc.returncode, 2, label + ": " + proc.stdout + proc.stderr)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload["error"]["code"], "invalid_argument", label)
                self.assertEqual(fs.read_log(self.log), [], "参数错误不应调用 phone CLI")


# --------------------------------------------------------------------------
# 截图、证据与"不自己操作设备"
# --------------------------------------------------------------------------

class EvidencePolicyTest(ScenarioCase):
    def test_screenshot_is_not_taken_by_default(self):
        obs = fs.observation_payload(RESULTS)
        _, _, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "termux"), fs.evaluate_ok(fs.evaluate_payload(RESULTS, "termux")),
        ])
        self.assertNotIn("screenshot", [e["action"] for e in log])

    def test_screenshot_only_on_failure_and_with_reason(self):
        obs = fs.observation_payload(EMPTY)
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "termux"), fs.evaluate_ok(fs.evaluate_payload(EMPTY, "termux")),
            {"action": "screenshot", "data": {"reason": "diagnostic", "artifact": "shot.png"}},
        ], extra=["--screenshot-on-failure"])
        self.assertEqual(rc, 1)
        shots = [e for e in log if e["action"] == "screenshot"]
        self.assertEqual(len(shots), 1)
        reason = shots[0]["args"][shots[0]["args"].index("--reason") + 1]
        self.assertTrue(reason.strip())
        self.assertEqual(payload["data"]["limits"]["screenshots_taken"], 1)

    def test_failure_steps_are_preserved_in_report(self):
        obs = fs.observation_payload(MULTI_FIELDS)
        rc, payload, _ = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
        ])
        self.assertEqual(rc, 3)
        data = payload["data"]
        report = json.loads(Path(data["report_path"]).read_text(encoding="utf-8"))
        self.assertEqual(report["outcome"], "error")
        self.assertEqual([s["action"] for s in report["steps"]], ["status", "launch"])
        self.assertEqual(report["steps"][0]["exit_code"], 0)
        self.assertTrue(report["steps"][0]["data_keys"])
        self.assertEqual(report["error"]["code"], "input_field_ambiguous")
        self.assertEqual(payload["artifacts"][0], data["report_path"])

    def test_scenario_only_ever_calls_the_phone_cli(self):
        src = Path(SCENE).read_text(encoding="utf-8")
        self.assertEqual(src.count("subprocess.run"), 1, "只允许通过 subprocess 调 phone CLI 这一处")
        self.assertIn("str(self.cfg.phone_cli)", src, "唯一的外部进程必须是 phone CLI")
        self.assertIn('"--adb"', src, "adb 只作为参数透传")
        for forbidden in ("os.system", "popen", "shell=True"):
            self.assertNotIn(forbidden, src)
        # 不改已验收的适配器，也不 import 它的私有实现
        self.assertNotIn("content_adapter", src)


class VerdictStrictnessTest(ScenarioCase):
    """dispatch/25 反例回归：`_verdict` 必须 fail closed，不能把矛盾/缺字段读成成功。

    前三个 case 与 Codex 独立探针 `integration/probe_scenario.py` 的输入逐字一致
    （该探针不修改、只照跑）。
    """

    @staticmethod
    def base(**overrides):
        data = {
            "content_verified": True, "input_matches_query": True, "empty_state": False,
            "result_count": 1, "page_kind": "search_results", "network_verified": "unknown",
            "observed_at": "2026-10-15T09:00:00Z", "real_result_candidates": [{"label": "Termux"}],
        }
        data.update(overrides)
        return data

    def verdict(self, data, query="termux"):
        outcome, summary, warnings = fsearch._verdict(data, query)   # 形状：固定三元组
        self.assertIsInstance(summary, dict)
        self.assertIsInstance(warnings, list)
        return outcome, summary

    def test_codex_probe_cases_are_rejected(self):
        cases = {
            "positive count but empty candidate list": {"real_result_candidates": []},
            "unsupported page reported as verified": {"page_kind": "unsupported_app"},
            "boolean result count": {"result_count": True},
        }
        for label, override in cases.items():
            with self.subTest(label):
                outcome, summary = self.verdict(self.base(**override))
                self.assertNotEqual(outcome, "verified", label)
                self.assertEqual(outcome, "invalid_response", label)
                self.assertTrue(summary["violations"], label)

    def test_more_contradictions_and_missing_fields_are_rejected(self):
        cases = {
            "empty dict": ({}, "termux"),
            "candidates not a list": (self.base(real_result_candidates="Termux"), "termux"),
            "count mismatches candidate list": (self.base(result_count=2), "termux"),
            "boolean empty_state": (self.base(empty_state=0), "termux"),
            "boolean content_verified as string": (self.base(content_verified="true"), "termux"),
            "missing page_kind": (self.base(page_kind=None), "termux"),
            "success without input_evidence": (self.base(), "termux"),
            "evidence raw_text differs": (
                self.base(input_evidence={"field_count": 1, "ambiguous": False, "exact_match": True,
                                          "raw_text": "termuxx", "node_id": "n",
                                          "matches_query_node_ids": ["n"]}), "termux"),
            "evidence ambiguous while claiming match": (
                self.base(input_evidence={"field_count": 2, "ambiguous": True, "exact_match": True,
                                          "raw_text": "termux", "node_id": "n",
                                          "matches_query_node_ids": ["n"]}), "termux"),
            "evidence node not listed as matching": (
                self.base(input_evidence={"field_count": 1, "ambiguous": False, "exact_match": True,
                                          "raw_text": "termux", "node_id": "n",
                                          "matches_query_node_ids": ["other"]}), "termux"),
        }
        for label, (data, query) in cases.items():
            with self.subTest(label):
                outcome, summary = self.verdict(data, query)
                self.assertEqual(outcome, "invalid_response", label)
                self.assertTrue(summary["violations"], label)

    def test_verdict_never_raises_on_junk(self):
        for junk in (None, [], "text", 3, {"content_verified": None}, {"real_result_candidates": None}):
            with self.subTest(repr(junk)):
                outcome, _, warnings = fsearch._verdict(junk, "termux")
                self.assertNotEqual(outcome, "verified")
                self.assertIsInstance(warnings, list)

    def test_legit_outcomes_are_still_classified(self):
        # 真实正例（适配器输出）仍然 verified
        positive = fs.evaluate_payload(RESULTS, "termux")
        self.assertEqual(self.verdict(positive)[0], "verified")
        # 空态：类型齐全、不声称成功 → empty_state（不是 invalid）
        empty = fs.evaluate_payload(EMPTY, "termux")
        self.assertEqual(self.verdict(empty)[0], "empty_state")
        # 非搜索页：0 候选、无成功声明 → not_verified
        plain = fs.evaluate_payload(fs.ORIG / "theme-dialog.xml", "termux")
        self.assertEqual(self.verdict(plain)[0], "not_verified")
        # 换词：有候选但查询未确认 → not_verified
        wrong = fs.evaluate_payload(RESULTS, "fdroid")
        self.assertEqual(self.verdict(wrong, "fdroid")[0], "not_verified")

    def test_contradictory_result_count_is_invalid_not_verified(self):
        data = self.base(real_result_candidates=[], result_count=1)
        outcome, summary = self.verdict(data)
        self.assertEqual(outcome, "invalid_response")
        self.assertTrue(any("does not match" in v for v in summary["violations"]))


class BindingTest(ScenarioCase):
    """dispatch/25：状态/观察绑定必须明确，缺字段不得跳过对比。"""

    def _flow(self, obs, payload_ev=None, launch_pkg="org.fdroid.basic", status=None):
        return [
            status if status is not None else fs.status_ok(),
            {"action": "launch", "data": {"package": launch_pkg, "after_observation": obs}},
            fs.click_ok(obs), fs.set_text_ok(obs, "termux"),
            fs.evaluate_ok(payload_ev if payload_ev is not None
                           else fs.evaluate_payload(RESULTS, "termux")),
        ]

    def test_observation_without_device_serial_is_refused(self):
        obs = fs.observation_payload(RESULTS)
        obs.pop("device_serial")
        rc, payload, log = self.run_scenario(self._flow(obs))
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "ownership_mismatch")
        self.assertIn("device_serial", payload["error"]["message"])
        self.assertNotIn("set-text", [e["action"] for e in log])

    def test_observation_for_another_device_is_refused(self):
        obs = fs.observation_payload(RESULTS, serial="emulator-5556")
        rc, payload, _ = self.run_scenario(self._flow(obs))
        self.assertEqual(payload["error"]["code"], "ownership_mismatch")
        self.assertEqual(payload["error"]["detail"]["reported_serial"], "emulator-5556")

    def test_observation_without_avd_is_refused(self):
        obs = fs.observation_payload(RESULTS)
        obs.pop("avd")
        rc, payload, _ = self.run_scenario(self._flow(obs))
        self.assertEqual(payload["error"]["code"], "ownership_mismatch")
        self.assertIn("avd", payload["error"]["message"])

    def test_status_avd_and_serial_are_validated(self):
        cases = [
            (fs.status_ok(avd="wrong-avd"), "different AVD"),
            (fs.status_ok(serial="emulator-5555"), "legal emulator serial"),
            (fs.status_ok(serial="R58M123ABC"), "legal emulator serial"),
        ]
        for i, (status, expect_msg) in enumerate(cases):
            with self.subTest(expect_msg):
                obs = fs.observation_payload(RESULTS)
                rc, payload, log = self.run_scenario(
                    self._flow(obs, status=status), spec_name="bind-%d.json" % i)
                self.assertEqual(rc, 3)
                self.assertEqual(payload["error"]["code"], "ownership_mismatch")
                self.assertIn(expect_msg, payload["error"]["message"])
                self.assertEqual([e["action"] for e in log], ["status"])

    def test_launch_must_report_the_whitelisted_package(self):
        obs = fs.observation_payload(RESULTS)
        for pkg in ("com.example.other", None):
            with self.subTest(repr(pkg)):
                rc, payload, log = self.run_scenario(
                    self._flow(obs, launch_pkg=pkg), spec_name="pkg.json")
                self.assertEqual(rc, 3)
                self.assertEqual(payload["error"]["code"], "invalid_phone_response")
                self.assertEqual([e["action"] for e in log], ["status", "launch"])

    def test_evaluate_search_must_report_the_same_device(self):
        obs = fs.observation_payload(RESULTS)
        ev = fs.evaluate_payload(RESULTS, "termux")
        ev.pop("device_serial")
        rc, payload, _ = self.run_scenario(self._flow(obs, payload_ev=ev))
        self.assertEqual(payload["error"]["code"], "ownership_mismatch")
        self.assertIn("evaluate-search", payload["error"]["message"])

    def test_readback_must_be_exactly_equal(self):
        obs = fs.observation_payload(RESULTS)
        rc, payload, log = self.run_scenario([
            fs.status_ok(), fs.launch_ok(obs), fs.click_ok(obs),
            fs.set_text_ok(obs, "termux", readback="termux ", verified=True),
            fs.evaluate_ok(fs.evaluate_payload(RESULTS, "termux")),
        ])
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "verification_failed")
        self.assertIn("readback_text", " ".join(payload["error"]["detail"]["problems"]))
        self.assertNotIn("evaluate-search", [e["action"] for e in log])

    def test_contradictory_evaluate_payload_is_an_error_not_success(self):
        obs = fs.observation_payload(RESULTS)
        ev = fs.evaluate_payload(RESULTS, "termux")
        ev["real_result_candidates"] = []          # 与 result_count=5 矛盾
        rc, payload, _ = self.run_scenario(self._flow(obs, payload_ev=ev))
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "invalid_phone_response")
        self.assertTrue(payload["error"]["detail"]["violations"])
        self.assertFalse(payload["ok"])

    def test_boolean_result_count_payload_is_rejected(self):
        obs = fs.observation_payload(RESULTS)
        ev = fs.evaluate_payload(RESULTS, "termux")
        ev["result_count"] = True
        rc, payload, _ = self.run_scenario(self._flow(obs, payload_ev=ev))
        self.assertEqual(rc, 3)
        self.assertEqual(payload["error"]["code"], "invalid_phone_response")


if __name__ == "__main__":
    unittest.main(verbosity=2)
