#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fdroid_details.py 的离线测试（标准库 unittest）。

全部用**假 sandbox CLI**（复用 `scenarios/tests/fake_phone.py`，argv 外形与
sandbox.py 相同）驱动：不接触设备、不运行 ADB、不依赖任何真实手机。

页面来源：
* **合成** fixture（`fixtures/synthetic/`，均标 SYNTHETIC）：详情页样例、含精确
  "Termux" 行的搜索页、同名重复行、只有标题文本的页面 —— 只用于离线用例，
  **不是**真机支持证据。
* **真实**快照（`departments/deepseek/fixtures/originals/`，第二轮已验证的 dump）：
  用于"相近名称不得被当成 Termux"和"空态不得当成功"这两个反例。

每个失败用例都断言 **stage** 与**实际调用序列**，证明失败发生在预期阶段，
而不是因为路径/参数错误提前退出。

运行：
    python -m unittest discover -s tests -v
    python tests/test_fdroid_details.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND3_DIR = HERE.parent
for extra in (str(HERE), str(ROUND3_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import fake_specs_r3 as f3            # noqa: E402
import fdroid_details as fd           # noqa: E402

PY = sys.executable
SCENARIO = str(ROUND3_DIR / "fdroid_details.py")
TMP_ROOT = ROUND3_DIR / "evidence" / "_tmp"

SYN = f3.SYN
DETAIL = f3.RESULTS_TERMUX            # 合成：含精确 Termux 行的搜索页
DETAIL_PAGE = f3.DETAIL_TERMUX        # 合成：详情页样例
DUP = f3.RESULTS_DUPLICATE            # 合成：两个同名行
BARE = f3.TITLE_ONLY                  # 合成：只有标题文本
REAL_RESULTS = f3.RESULTS_REAL        # 真实：Termux 系结果页（没有裸 Termux）
REAL_EMPTY = f3.EMPTY_REAL            # 真实：空态页


class R3Case(unittest.TestCase):
    def setUp(self):
        self.tmp = TMP_ROOT / self._testMethodName
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.adb = self.tmp / "adb.exe"
        self.adb.write_text("stub-not-a-real-adb", encoding="utf-8")
        self.state = self.tmp / "instance.json"
        self.state.write_text(json.dumps({"avd": f3.AVD, "serial": f3.DEVICE_SERIAL}),
                              encoding="utf-8")
        self.log = self.tmp / "fake.log"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 驱动 ---------------------------------------------------------
    def argv(self, extra=(), label="Termux", query="termux", report_dir=None, parse=None):
        if parse is not None:
            return [PY, SCENARIO, "--parse-observation", str(parse), "--label", label,
                    "--report-dir", str(report_dir or (self.tmp / "reports"))]
        return [
            PY, SCENARIO,
            "--sandbox", str(f3.FAKE_CLI),
            "--python", PY,
            "--adb", str(self.adb),
            "--state-file", str(self.state),
            "--session-dir", str(self.tmp / "session"),
            "--query", query,
            "--label", label,
            "--report-dir", str(report_dir or (self.tmp / "reports")),
        ] + [str(a) for a in extra]

    def run_scenario(self, calls, label="Termux", query="termux", extra=(), spec_name="spec.json",
                     report_dir=None):
        spec = f3.write_spec(self.tmp / spec_name, calls)
        self.log.write_text("", encoding="utf-8")
        proc = subprocess.run(self.argv(extra=extra, label=label, query=query, report_dir=report_dir),
                              capture_output=True, text=True, encoding="utf-8",
                              env=f3.spawn_env(spec, self.log))
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, payload, f3.read_log(self.log)

    def run_parse(self, observation, label="Termux"):
        path = self.tmp / "saved-observation.json"
        path.write_text(json.dumps(observation, ensure_ascii=False), encoding="utf-8")
        proc = subprocess.run(self.argv(parse=path, label=label),
                              capture_output=True, text=True, encoding="utf-8")
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        return proc.returncode, payload

    @staticmethod
    def sequence(payload):
        return (payload.get("data") or {}).get("calls", {}).get("sequence", [])

    def assertStage(self, payload, code, stage):
        self.assertEqual(payload["error"]["code"], code)
        self.assertEqual(payload["data"]["stage"], stage)
        self.assertFalse(payload["ok"])


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

class HappyPathTest(R3Case):
    def test_unique_exact_result_opens_and_reads_detail(self):
        rc, payload, log = self.run_scenario(
            f3.search_flow("termux", DETAIL, details_page=DETAIL_PAGE))
        self.assertEqual(rc, 0, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "fdroid_details")
        data = payload["data"]
        self.assertEqual(data["outcome"], "details_verified")
        self.assertEqual(data["stage"], "extract_details")
        self.assertTrue(data["details_verified"])
        self.assertEqual(self.sequence(payload),
                         ["status", "launch", "click", "set-text", "evaluate-search", "click"])
        # 定位与详情记录
        self.assertEqual(data["target"]["label"], "Termux")
        self.assertTrue(data["target"]["matched_exactly"])
        det = data["details"]
        self.assertEqual(det["title"]["text"], "Termux")
        self.assertEqual(det["fields"]["package_name"]["text"], "com.termux")
        self.assertEqual(det["fields"]["version"]["text"], "0.118.3")
        self.assertIn("terminal emulator", det["fields"]["summary"]["text"])
        self.assertEqual(det["unknown_fields"], [])
        self.assertTrue(det["fields_reliable"])
        self.assertEqual([a["label"] for a in det["action_controls"]],
                         ["Install", "Share", "Source Code", "Website"])
        self.assertGreaterEqual(len(det["structure_evidence"]["signals"]), 3)
        self.assertTrue(det["structure_evidence"]["positive_signals"])
        # 观察标识/采集时间/设备都记下来了
        self.assertEqual(det["observation_id"], f3.observation_payload(DETAIL_PAGE)["observation_id"])
        self.assertEqual(det["observed_at"], f3.OBSERVED_AT)
        self.assertEqual(det["device_serial"], f3.DEVICE_SERIAL)
        self.assertEqual(det["avd"], f3.AVD)
        # 识别规则明确标注"待实机确认"
        self.assertEqual(det["recognition"]["status"], "provisional_pending_real_sample")
        # 每一步都有宿主 UTC 起止时间；全程无坐标、无截图
        for step in data["steps"]:
            self.assertRegex(step["host_started_utc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
            self.assertRegex(step["host_ended_utc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertTrue(data["no_coordinates_sent"])
        for entry in log:
            self.assertNotIn("screenshot", entry["action"])
            for token in entry["argv"]:
                self.assertFalse(str(token).lstrip("-").isdigit(), entry["argv"])
        # 报告落在 round3 内
        report = Path(data["report_path"])
        self.assertTrue(report.is_file())
        self.assertTrue(report.is_relative_to(ROUND3_DIR))
        saved = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(saved["outcome"], "details_verified")
        self.assertEqual(len(saved["steps"]), 6)

    def test_stale_once_is_recovered_and_relocated(self):
        page = f3.observation_payload(DETAIL)
        detail = f3.observation_payload(DETAIL_PAGE)
        calls = [
            f3.status_ok(), f3.launch_ok(page), f3.click_ok(page),
            f3.set_text_ok(page, "termux"), f3.evaluate_ok(f3.evaluate_payload(DETAIL, "termux")),
            f3.phone_error("click", "stale_observation", "observation changed"),
            {"action": "observe", "data": page},          # 重新观察：还是搜索结果页
            f3.click_ok(detail),                          # 重新定位后再点
        ]
        rc, payload, _ = self.run_scenario(calls)
        self.assertEqual(rc, 0, payload)
        data = payload["data"]
        self.assertTrue(data["details_verified"])
        self.assertEqual(data["limits"]["stale_recovery_used"], 1)
        self.assertEqual(self.sequence(payload),
                         ["status", "launch", "click", "set-text", "evaluate-search",
                          "click", "observe", "click"])
        self.assertEqual(data["steps"][5]["error_code"], "stale_observation")


# --------------------------------------------------------------------------
# 反例：必须失败，且失败在预期阶段
# --------------------------------------------------------------------------

class NegativeTest(R3Case):
    def test_search_page_with_same_text_is_not_a_detail_page(self):
        """点击后页面还是搜索列表（含同名文本）→ 不得算详情。"""
        rc, payload, log = self.run_scenario(f3.search_flow("termux", DETAIL))  # 不换页
        self.assertEqual(rc, 1)
        self.assertStage(payload, "details_not_confirmed", "extract_details")
        self.assertEqual(self.sequence(payload),
                         ["status", "launch", "click", "set-text", "evaluate-search", "click"])
        det = payload["data"]["details"]
        self.assertEqual(det["title"]["text"], "Termux")         # 同名文本确实在
        self.assertFalse(det["structure_evidence"]["satisfied"])  # 但没有独立结构证据
        self.assertEqual(det["structure_evidence"]["signals"], [])
        self.assertFalse(det["fields_reliable"])

    def test_near_names_are_not_accepted(self):
        """真实结果页里只有 Termux:API / Termux Hub 之类：不得当 Termux。"""
        rc, payload, log = self.run_scenario(
            f3.search_flow("termux", REAL_RESULTS, details_page=DETAIL_PAGE))
        self.assertEqual(rc, 1)
        self.assertStage(payload, "target_not_found", "locate_target")
        self.assertEqual(payload["error"]["detail"]["label"], "Termux")
        self.assertEqual(payload["data"]["search"]["verdict"]["result_count"], 5,
                         "搜索本身是成功的（找到 5 条），只是没有精确叫 Termux 的那条")
        self.assertEqual(self.sequence(payload),
                         ["status", "launch", "click", "set-text", "evaluate-search"])
        # 最后一次 click 只是给输入框聚焦，没有点任何结果行
        clicks = [e for e in log if e["action"] == "click"]
        self.assertEqual(len(clicks), 1)
        self.assertEqual(clicks[0]["args"][-1], payload["data"]["search"]["located"]["focus_click_node_id"])

    def test_duplicate_labels_are_refused(self):
        rc, payload, log = self.run_scenario(
            f3.search_flow("termux", DUP, details_page=DETAIL_PAGE))
        self.assertEqual(rc, 1)
        self.assertStage(payload, "target_ambiguous", "locate_target")
        self.assertEqual(len(payload["error"]["detail"]["row_node_ids"]), 2)
        self.assertEqual(self.sequence(payload),
                         ["status", "launch", "click", "set-text", "evaluate-search"])

    def test_title_without_independent_evidence_is_refused(self):
        rc, payload, _ = self.run_scenario(
            f3.search_flow("termux", DETAIL, details_page=BARE))
        self.assertEqual(rc, 1)
        self.assertStage(payload, "details_not_confirmed", "extract_details")
        det = payload["data"]["details"]
        self.assertEqual(det["title"]["text"], "Termux")
        self.assertEqual(det["structure_evidence"]["positive_signals"], [],
                         "只有'缺什么'的证据不能算通过")
        self.assertIn("has_back_control", det["structure_evidence"]["missing"])

    def test_empty_search_page_stops_before_locating(self):
        rc, payload, log = self.run_scenario(
            f3.search_flow("termux", REAL_EMPTY, details_page=DETAIL_PAGE))
        self.assertEqual(rc, 1)
        self.assertStage(payload, "search_not_verified", "search_evaluate")
        self.assertEqual(payload["data"]["search"]["adapter_outcome"], "empty_state")
        self.assertEqual(self.sequence(payload),
                         ["status", "launch", "click", "set-text", "evaluate-search"])
        self.assertEqual(len([e for e in log if e["action"] == "click"]), 1, "只点了输入框")

    def test_repeated_stale_stops(self):
        page = f3.observation_payload(DETAIL)
        calls = [
            f3.status_ok(), f3.launch_ok(page), f3.click_ok(page),
            f3.set_text_ok(page, "termux"), f3.evaluate_ok(f3.evaluate_payload(DETAIL, "termux")),
            f3.phone_error("click", "stale_observation"),
            {"action": "observe", "data": page},
            f3.phone_error("click", "stale_observation"),
            f3.click_ok(f3.observation_payload(DETAIL_PAGE)),
        ]
        rc, payload, log = self.run_scenario(calls)
        self.assertEqual(rc, 3)
        self.assertStage(payload, "stale_observation", "open_details")
        target_clicks = [e for e in log if e["action"] == "click"][1:]
        self.assertEqual(len(target_clicks), 2, "只允许重新定位一次，不得第三次点击")
        self.assertEqual(payload["data"]["limits"]["stale_recovery_used"], 1)

    def test_subprocess_nonzero_and_timeout(self):
        rc, payload, _ = self.run_scenario([
            {"action": "status", "ok": True,
             "data": {"ready": True, "device_serial": f3.DEVICE_SERIAL, "avd": f3.AVD}, "exit": 7},
        ])
        self.assertEqual(rc, 3)
        self.assertStage(payload, "invalid_phone_response", "status")

        rc, payload, log = self.run_scenario([
            f3.phone_error("status", "device_unavailable", "no emulator")], spec_name="s2.json")
        self.assertEqual(rc, 3)
        self.assertStage(payload, "device_unavailable", "status")
        self.assertEqual([e["action"] for e in log], ["status"])

        rc, payload, log = self.run_scenario([
            {"action": "status", "data": {"ready": True, "device_serial": f3.DEVICE_SERIAL,
                                          "avd": f3.AVD}, "delay": 3.0}],
            extra=["--per-call-timeout", "0.7"], spec_name="s3.json")
        self.assertEqual(rc, 3)
        self.assertStage(payload, "driver_timeout", "status")
        self.assertEqual([e["action"] for e in log], ["status"])
        self.assertTrue(payload["data"]["steps"][0]["timed_out"])


# --------------------------------------------------------------------------
# 只读解析保存的观察 JSON
# --------------------------------------------------------------------------

class ParseObservationTest(R3Case):
    def test_parses_synthetic_detail_sample(self):
        rc, payload = self.run_parse(f3.observation_payload(DETAIL_PAGE))
        self.assertEqual(rc, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "fdroid_details_parse")
        det = payload["data"]["details"]
        self.assertTrue(det["details_verified"])
        self.assertEqual(det["fields"]["version"]["text"], "0.118.3")
        self.assertEqual(payload["data"]["calls"]["count"], 0)

    def test_parses_real_search_page_as_not_a_detail(self):
        rc, payload = self.run_parse(f3.observation_payload(REAL_RESULTS), label="Termux")
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "details_not_confirmed")
        det = payload["data"]["details"]
        self.assertIsNone(det["title"])                      # 真实页里没有裸 Termux
        self.assertIn("no_search_field", det["structure_evidence"]["missing"])
        self.assertFalse(det["details_verified"])

    def test_parses_full_envelope_json(self):
        rc, payload = self.run_parse({"ok": True, "action": "observe",
                                      "data": f3.observation_payload(DETAIL_PAGE)})
        self.assertEqual(rc, 0)
        self.assertTrue(payload["data"]["details"]["details_verified"])

    def test_rejects_json_without_observation(self):
        path = self.tmp / "not-an-observation.json"
        path.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        proc = subprocess.run(self.argv(parse=path), capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 2)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], "invalid_argument")


# --------------------------------------------------------------------------
# 纯函数与策略
# --------------------------------------------------------------------------

class StructuralRulesTest(R3Case):
    def test_label_rows_is_exact_and_structural(self):
        obs = f3.observation_payload(DETAIL)
        self.assertEqual(len(fd.label_rows(obs, "Termux")), 1)
        self.assertEqual(len(fd.label_rows(obs, "termux")), 0, "大小写不同不算同名")
        self.assertEqual(len(fd.label_rows(obs, "Termux Hub")), 1)

        real = f3.observation_payload(REAL_RESULTS)
        self.assertEqual(fd.label_rows(real, "Termux"), [], "真实页里没有裸 Termux")
        self.assertEqual(len(fd.label_rows(real, "Termux:API")), 1)

    def test_extract_never_invents_missing_fields(self):
        obs = f3.observation_payload(BARE)
        det = fd.extract_details(obs, "Termux")
        self.assertEqual(det["unknown_fields"], ["package_name", "summary", "version"])
        for name in ("package_name", "summary", "version"):
            self.assertIsNone(det["fields"][name])
        self.assertFalse(det["details_verified"])

    def test_report_dir_outside_round3_is_refused(self):
        outside = ROUND3_DIR / ".." / "_should_not_exist_reports"
        rc, payload, log = self.run_scenario([f3.status_ok()], report_dir=outside)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["error"]["code"], "report_dir_outside_round3_dir")
        self.assertEqual(log, [])
        self.assertFalse(outside.exists())

    def test_invalid_arguments_do_not_reach_the_cli(self):
        cases = [
            ("query charset", {"--query": "term ux!"}),
            ("empty label", {"--label": ""}),
            ("zero budget", {"--max-calls": "0"}),
        ]
        for i, (label, override) in enumerate(cases):
            with self.subTest(label):
                spec = f3.write_spec(self.tmp / ("bad-%d.json" % i), [f3.status_ok()])
                self.log.write_text("", encoding="utf-8")
                argv = self.argv()
                for flag, value in override.items():
                    if flag in argv:
                        argv[argv.index(flag) + 1] = value
                    else:
                        argv += [flag, value]
                proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                                      env=f3.spawn_env(spec, self.log))
                self.assertEqual(proc.returncode, 2, label + proc.stdout)
                payload = json.loads(proc.stdout)
                self.assertEqual(payload["error"]["code"], "invalid_argument")
                self.assertEqual(f3.read_log(self.log), [], "参数错误不得调用 sandbox")

        # 设备参数缺失
        spec = f3.write_spec(self.tmp / "bad-missing.json", [f3.status_ok()])
        argv = [PY, SCENARIO, "--sandbox", str(f3.FAKE_CLI), "--python", PY,
                "--state-file", str(self.state), "--session-dir", str(self.tmp / "s"),
                "--query", "termux", "--label", "Termux", "--report-dir", str(self.tmp / "reports")]
        self.log.write_text("", encoding="utf-8")
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                              env=f3.spawn_env(spec, self.log))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(json.loads(proc.stdout)["error"]["code"], "invalid_argument")

    def test_module_only_calls_the_sandbox_entry(self):
        src = Path(SCENARIO).read_text(encoding="utf-8")
        self.assertEqual(src.count("subprocess.run"), 0, "本模块不自己起子进程")
        self.assertIn("import fdroid_search as ds", src)
        self.assertIn("import content_adapter as ca", src)
        self.assertIn('"--adb"', src, "adb 只作为参数交给 sandbox.py")
        self.assertNotIn("evaluate-details", src, "不发明后端命令")
        for forbidden in ("os.system", "popen", "shell=True"):
            self.assertNotIn(forbidden, src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
