#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
content_adapter.py 的标准库 unittest。

全部用例只用离线 XML 快照与小型 fixture，不连设备、不联网、不做负载/长采样。
运行：
    python -m unittest discover -s tests -v
    python tests/test_content_adapter.py

用例分四组：真实快照（observe）、字段规则、resolve、evaluate-search，
另加 CLI 契约与输出路径策略。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # departments/deepseek
sys.path.insert(0, str(BASE))

import content_adapter as ca  # noqa: E402

PY = sys.executable
ORIG = BASE / "fixtures" / "originals"
SYN = BASE / "fixtures" / "synthetic"
TMP = BASE / "tests" / "_tmp"
CLI = str(BASE / "content_adapter.py")

EMPTY_SEARCH = ORIG / "hierarchy-20261015T024231-c934f0c7.xml"
SETTINGS = ORIG / "hierarchy-20261015T030637-30a5f3db.xml"
RESULTS = ORIG / "hierarchy-search-results.xml"
HOME = ORIG / "hierarchy-home.xml"
THEME_DIALOG = ORIG / "theme-dialog.xml"

# 上游 zcode/review-wb02/input/SNAPSHOT.md 与 zcode/review-ds01/input/SNAPSHOT.md 记录的原始 SHA-256
RECORDED_SHA = {
    "hierarchy-20261015T024231-c934f0c7.xml":
        "c13e5d5c5c601cb7c7dcb5b3d623f79b60985d92a00e418a5ca58567e34c1406",
    "hierarchy-20261015T030637-30a5f3db.xml":
        "bcdb1fc39a1a6940367606df2b787e45da9ecbf733e72681732caf87d1dad29c",
    "hierarchy-search-results.xml":
        "c797c0e0b79d7b997ecf4c4a8942e21c9aec13dea762db2089e64e124433cecd",
    "hierarchy-home.xml":
        "6ca81834c3cb69cac848057e6a0c9ef1440d08313ecfd23b86eeb581d2a585e1",
    "theme-dialog.xml":
        "d6af514c4828f430814939c5c53b8adb87ad1672aa8da919c9f8c0684c8eec75",
}


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run_cli(*args, expect_ok=None):
    """跑一次 CLI，返回 (returncode, payload_or_None, stderr)。"""
    proc = subprocess.run(
        [PY, CLI] + [str(a) for a in args],
        cwd=str(BASE), capture_output=True, text=True, encoding="utf-8",
    )
    payload = None
    if proc.stdout.strip():
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            payload = None
    if expect_ok is not None:
        assert payload is not None, "stdout was not JSON: %r" % proc.stdout[:400]
        if payload.get("ok") != expect_ok:
            raise AssertionError("expected ok=%s, got %r (stderr=%s)"
                                 % (expect_ok, payload, proc.stderr))
    return proc.returncode, payload, proc.stderr


def observe_to_tmp(xml, name, expect_ok=True):
    out = TMP / name
    rc, payload, err = run_cli("observe", "--xml", xml, "--output", out, expect_ok=expect_ok)
    return rc, payload, err, out


def node_by_text(payload, text):
    for n in payload["nodes"]:
        if n["text"] == text:
            return n
    raise AssertionError("no node with text %r" % text)


def ids(payload):
    return [n["node_id"] for n in payload["nodes"]]


def reason_for(d, text=None, desc=None):
    """在 evaluate-search 的 excluded 里按 text/content_desc 精确找原因。"""
    for e in d["excluded"]:
        if e["text"] == text and e["content_desc"] == desc:
            return e["reason"]
    raise AssertionError("no excluded entry with text=%r desc=%r" % (text, desc))


class TmpMixin(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        shutil.rmtree(TMP, ignore_errors=True)


# --------------------------------------------------------------------------
# 真实快照
# --------------------------------------------------------------------------

class RealSnapshotObserveTest(TmpMixin):
    def test_observe_contract_and_ids(self):
        for xml in (EMPTY_SEARCH, SETTINGS, RESULTS, HOME):
            rc, payload, err, out = observe_to_tmp(xml, "obs-%s.json" % xml.stem)
            self.assertEqual(rc, 0, err)
            self.assertEqual(payload["source"], "ui")
            self.assertEqual(payload["coverage"], "partial")
            self.assertIsNone(payload["observed_at"])
            self.assertEqual(payload["observation_id"], sha256_file(xml))
            self.assertEqual(payload["source_xml"]["sha256"], sha256_file(xml))
            # stdout JSON 与落盘 JSON 必须一致
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), payload)
            # 每个节点有唯一 node_id，node_id 前缀 = observation hash 前 12 位
            self.assertEqual(len(ids(payload)), len(set(ids(payload))))
            for nid in ids(payload):
                self.assertTrue(nid.startswith(payload["observation_id"][:12] + ":"), nid)

    def test_root_has_no_package_and_apps_come_from_real_nodes(self):
        _, payload, _, _ = observe_to_tmp(EMPTY_SEARCH, "obs-root.json")
        root = payload["nodes"][0]
        self.assertIsNone(root["parent_id"])
        self.assertIsNone(root["package"])      # <hierarchy> 无 package 属性
        self.assertEqual(payload["apps"], ["org.fdroid.basic"])  # 从真实节点取
        # 不能返回假空包名：package 只能是 null 或非空字符串
        for n in payload["nodes"]:
            pkg = n["package"]
            self.assertTrue(pkg is None or (isinstance(pkg, str) and pkg.strip()),
                            "fabricated package value: %r" % pkg)
        self.assertIn("root_package_missing", [w["code"] for w in payload["warnings"]])

    def test_snapshot_files_match_upstream_hashes(self):
        for name, expected in RECORDED_SHA.items():
            self.assertEqual(sha256_file(ORIG / name), expected, name)

    def test_input_xml_is_never_modified(self):
        before = sha256_file(SETTINGS)
        observe_to_tmp(SETTINGS, "obs-nomod.json")
        self.assertEqual(sha256_file(SETTINGS), before)

    def test_observe_is_deterministic(self):
        _, a, _, _ = observe_to_tmp(EMPTY_SEARCH, "obs-det-a.json")
        _, b, _, _ = observe_to_tmp(EMPTY_SEARCH, "obs-det-b.json")
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))

    def test_parent_links_follow_tree_paths(self):
        _, payload, _, _ = observe_to_tmp(RESULTS, "obs-tree.json")
        by_id = {n["node_id"]: n for n in payload["nodes"]}
        for n in payload["nodes"]:
            if n["parent_id"] is None:
                continue
            parent = by_id[n["parent_id"]]
            self.assertTrue(n["path"].startswith(parent["path"] + "."), n["path"])
            self.assertEqual(parent["path"], n["path"].rsplit(".", 1)[0])


# --------------------------------------------------------------------------
# 字段规则
# --------------------------------------------------------------------------

class FieldRulesTest(TmpMixin):
    def test_duplicate_and_empty_resource_ids_are_not_merged(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-duplicate-ids.xml", "obs-dup.json")
        self.assertEqual(len(payload["nodes"]), 7)   # 1 根 + 6 个 node
        self.assertEqual(len(set(ids(payload))), 7)
        rids = [n["resource_id"] for n in payload["nodes"]]
        self.assertEqual(rids.count("org.fdroid.basic:id/title"), 3)  # 重复 id 全保留
        self.assertEqual(rids.count(None), 4)                          # 空串/缺失 -> null
        self.assertEqual([n["text"] for n in payload["nodes"] if n["text"]], ["First", "Second", "Third", "Fourth"])

    def test_missing_attributes_become_null_not_fabricated(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-no-package.xml", "obs-nopkg.json")
        self.assertEqual(payload["apps"], [])
        for n in payload["nodes"]:
            self.assertIsNone(n["package"])
        self.assertIn("no_package_information", [w["code"] for w in payload["warnings"]])
        btn = node_by_text(payload, "Hello")
        self.assertIsNone(btn["resource_id"])
        self.assertIsNone(btn["content_desc"])
        self.assertEqual(btn["actions"], ["click"])

    def test_invalid_bounds_produce_no_coordinates_and_no_action(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-bad-bounds.xml", "obs-bounds.json")
        codes = [w["code"] for w in payload["warnings"]]
        self.assertIn("invalid_bounds", codes)
        for text in ("A", "B", "C", "D"):
            n = node_by_text(payload, text)
            self.assertIsNone(n["bounds"], text)
            self.assertFalse(n["bounds_valid"])
            self.assertEqual(n["actions"], [], text)
        good = node_by_text(payload, "E")
        self.assertEqual(good["bounds"], [100, 100, 300, 300])
        self.assertEqual(good["actions"], ["click"])

    def test_unreadable_boolean_is_unknown_not_false(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-bad-bounds.xml", "obs-bool.json")
        n = node_by_text(payload, "F")
        self.assertIsNone(n["enabled"])       # "yes" 不是 true/false -> null，不是 False
        self.assertEqual(n["actions"], [])
        codes = [w["code"] for w in payload["warnings"]]
        self.assertIn("unexpected_boolean_value", codes)
        self.assertIn("unknown_enabled_state", codes)

    def test_screen_text_is_inert_data(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-injection-text.xml", "obs-inject.json")
        texts = [n["text"] for n in payload["nodes"] if n["text"]]
        self.assertIn('"; rm -rf / # $(whoami) `id` && del /S /Q C:\\*', texts)
        self.assertIn("../../../../etc/passwd", texts)
        self.assertIn("<script>alert(1)</script>", texts)

    def test_module_never_shells_out(self):
        src = (BASE / "content_adapter.py").read_text(encoding="utf-8")
        for forbidden in ("import subprocess", "os.system", "os.popen", "shell=True", "eval(", "exec("):
            self.assertNotIn(forbidden, src, forbidden)


# --------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------

class ResolveRealDataTest(TmpMixin):
    def _settings_nodes(self):
        return observe_to_tmp(SETTINGS, "obs-set.json")[1]

    def test_theme_text_climbs_to_real_clickable_parent(self):
        payload = self._settings_nodes()
        theme = node_by_text(payload, "Theme")
        self.assertFalse(theme["clickable"])
        self.assertEqual(theme["actions"], [])
        rc, res, err = run_cli(
            "resolve", "--xml", SETTINGS, "--node-id", theme["node_id"], "--action", "click",
            expect_ok=True,
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual(res["resolved_via"], "ancestor")
        self.assertEqual(res["hop_count"], 1)
        self.assertEqual(res["target_node"]["bounds"], [0, 280, 720, 432])
        self.assertEqual([n for n in res["ancestor_chain"]], [theme["node_id"], res["target_node_id"]])
        self.assertEqual(res["plan"], {"action": "tap", "x": 360, "y": 356})
        self.assertTrue(res["plan_only"])
        self.assertEqual(res["target_node"]["subtree_texts"], ["Theme", "Follow system"])

    def test_plan_point_is_inside_target_bounds(self):
        payload = self._settings_nodes()
        theme = node_by_text(payload, "Theme")
        _, res, _ = run_cli("resolve", "--xml", SETTINGS, "--node-id", theme["node_id"],
                            "--action", "click", expect_ok=True)
        x1, y1, x2, y2 = res["bounds_used"]
        self.assertTrue(x1 < res["plan"]["x"] < x2)
        self.assertTrue(y1 < res["plan"]["y"] < y2)

    def test_clickable_node_resolves_to_itself(self):
        payload = self._settings_nodes()
        row_id = [n["node_id"] for n in payload["nodes"]
                  if n["bounds"] == [0, 280, 720, 432]][0]
        _, res, _ = run_cli("resolve", "--xml", SETTINGS, "--node-id", row_id,
                            "--action", "click", expect_ok=True)
        self.assertEqual(res["resolved_via"], "self")
        self.assertEqual(res["hop_count"], 0)
        self.assertEqual(res["target_node_id"], row_id)

    def test_decoy_rectangle_is_not_chosen(self):
        """父链才是依据；矩形包含关系不算依据。"""
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-decoy-ancestor.xml", "obs-decoy.json")
        label = node_by_text(payload, "Theme")
        row = [n for n in payload["nodes"]
               if n["resource_id"] == "org.fdroid.basic:id/theme_row"][0]
        decoy = [n for n in payload["nodes"]
                 if n["resource_id"] == "decoy:id/fullscreen_overlay"][0]
        # 诱饵矩形完全包住 label 中心，且在文档序中排在前面
        x, y = (label["bounds"][0] + label["bounds"][2]) // 2, (label["bounds"][1] + label["bounds"][3]) // 2
        self.assertTrue(decoy["bounds"][0] <= x <= decoy["bounds"][2])
        self.assertTrue(decoy["bounds"][1] <= y <= decoy["bounds"][3])
        self.assertLess(payload["nodes"].index(decoy), payload["nodes"].index(row))
        _, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-decoy-ancestor.xml",
                            "--node-id", label["node_id"], "--action", "click", expect_ok=True)
        self.assertEqual(res["target_node_id"], row["node_id"])
        self.assertNotEqual(res["target_node_id"], decoy["node_id"])


class ResolveRejectionTest(TmpMixin):
    def test_stale_node_id_from_another_observation_is_rejected(self):
        results = observe_to_tmp(RESULTS, "obs-t1.json")[1]
        stale = node_by_text(results, "Termux Hub")["node_id"]
        rc, res, err = run_cli("resolve", "--xml", EMPTY_SEARCH, "--node-id", stale,
                              "--action", "click")
        self.assertEqual(rc, 1)
        self.assertFalse(res["ok"])
        self.assertEqual(res["error"]["code"], "stale_node_id")
        self.assertIsNone(res["plan"])
        self.assertIsNone(res["target_node_id"])
        self.assertTrue(err.strip())

    def test_unknown_and_malformed_node_id_are_rejected(self):
        payload = observe_to_tmp(SETTINGS, "obs-t2.json")[1]
        prefix = payload["observation_id"][:12]
        rc, res, _ = run_cli("resolve", "--xml", SETTINGS, "--node-id", prefix + ":R.9.9.9",
                             "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "unknown_node_id"))
        rc, res, _ = run_cli("resolve", "--xml", SETTINGS, "--node-id", "not-a-node-id",
                             "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "invalid_node_id"))

    def test_disabled_node_is_rejected(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-disabled-node.xml", "obs-dis.json")
        disabled = [n for n in payload["nodes"] if n["clickable"] is True][0]
        self.assertEqual(disabled["enabled"], False)
        self.assertEqual(disabled["actions"], [])
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-disabled-node.xml",
                             "--node-id", disabled["node_id"], "--action", "click")
        # v1.1.0：请求节点自身 enabled 非 true 时用更精确的错误码
        self.assertEqual((rc, res["error"]["code"]), (1, "requested_node_disabled"))
        self.assertIsNone(res["plan"])

    def test_label_in_disabled_row_still_hits_disabled_target(self):
        """标签 enabled 但最近的可点击祖先禁用：target_disabled，且不越过它。"""
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-disabled-node.xml", "obs-dis2.json")
        label = node_by_text(payload, "Disabled row")
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-disabled-node.xml",
                             "--node-id", label["node_id"], "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "target_disabled"))
        self.assertEqual(res["error"]["detail"]["enabled_state"], "false")

    def test_node_without_action_and_without_actionable_ancestor_is_rejected(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-disabled-node.xml", "obs-lone.json")
        lonely = node_by_text(payload, "Lonely label")
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-disabled-node.xml",
                             "--node-id", lonely["node_id"], "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "no_supported_target"))
        self.assertIn("ancestor_chain", res["error"]["detail"])

    def test_invalid_bounds_target_is_rejected(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-bad-bounds.xml", "obs-rb.json")
        a = node_by_text(payload, "A")
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-bad-bounds.xml",
                             "--node-id", a["node_id"], "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "invalid_bounds"))

    def test_unknown_action_is_rejected_without_guessing(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-bad-bounds.xml", "obs-ra.json")
        a = node_by_text(payload, "A")
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-bad-bounds.xml",
                             "--node-id", a["node_id"], "--action", "swipe")
        self.assertEqual((rc, res["error"]["code"]), (1, "unsupported_action"))
        self.assertEqual(res["error"]["detail"]["supported_actions"], ["click", "long_click", "scroll"])
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-bad-bounds.xml",
                             "--node-id", a["node_id"], "--action", "Click")
        self.assertEqual((rc, res["error"]["code"]), (1, "unsupported_action"))

    def test_scroll_needs_direction_and_stays_inside_bounds(self):
        _, payload, _, _ = observe_to_tmp(SYN / "synthetic-scrollable.xml", "obs-scr.json")
        lst = [n for n in payload["nodes"] if n["resource_id"] == "org.fdroid.basic:id/list"][0]
        strip = [n for n in payload["nodes"] if n["resource_id"] == "org.fdroid.basic:id/strip"][0]
        off = [n for n in payload["nodes"] if n["resource_id"] == "org.fdroid.basic:id/disabled_list"][0]
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-scrollable.xml",
                             "--node-id", lst["node_id"], "--action", "scroll")
        self.assertEqual((rc, res["error"]["code"]), (1, "missing_or_unsupported_direction"))
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-scrollable.xml",
                             "--node-id", lst["node_id"], "--action", "scroll", "--direction", "up",
                             expect_ok=True)
        plan = res["plan"]
        self.assertEqual(plan["action"], "swipe")
        self.assertEqual((plan["x1"], plan["x2"]), (360, 360))
        self.assertLess(plan["y2"], plan["y1"])          # up = 手指往上
        for key in ("y1", "y2"):
            self.assertTrue(0 <= plan[key] <= 1024, plan)
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-scrollable.xml",
                             "--node-id", strip["node_id"], "--action", "scroll", "--direction", "up")
        self.assertEqual((rc, res["error"]["code"]), (1, "target_too_small"))
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-scrollable.xml",
                             "--node-id", off["node_id"], "--action", "scroll", "--direction", "up")
        self.assertEqual((rc, res["error"]["code"]), (1, "requested_node_disabled"))

    def test_real_scrollable_node_reports_scroll_action(self):
        _, payload, _, _ = observe_to_tmp(HOME, "obs-home-scr.json")
        scrollers = [n for n in payload["nodes"] if "scroll" in n["actions"]]
        self.assertTrue(scrollers)
        for n in scrollers:
            self.assertTrue(n["scrollable"] is True and n["enabled"] is True)


# --------------------------------------------------------------------------
# evaluate-search
# --------------------------------------------------------------------------

class EvaluateSearchTest(TmpMixin):
    def test_empty_state_page_yields_zero_results(self):
        """WB-02 回归：空态页曾被算成 4 条结果。"""
        rc, d, err = run_cli("evaluate-search", "--xml", EMPTY_SEARCH, "--query", "termux",
                             expect_ok=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(d["result_count"], 0)
        self.assertEqual(d["real_result_candidates"], [])
        self.assertTrue(d["empty_state"])
        self.assertFalse(d["content_present"])
        self.assertFalse(d["content_verified"])
        self.assertEqual(d["page_kind"], "search_empty_state")
        # 空态文案、输入回显、底部导航都必须标明"不算结果"
        self.assertEqual(
            reason_for(d, text="No apps found\n\nTry using fewer search terms or add more repositories"),
            "empty_state_text",
        )
        self.assertEqual(reason_for(d, text="termux"), "search_input_echo")
        for label in ("Discover", "Search", "My apps"):
            self.assertEqual(reason_for(d, text=label), "bottom_nav_label", label)
        self.assertEqual(reason_for(d, desc="Back"), "toolbar_or_action_label")

    def test_real_search_results_page_is_verified(self):
        _, d, _ = run_cli("evaluate-search", "--xml", RESULTS, "--query", "termux", expect_ok=True)
        self.assertEqual(d["page_kind"], "search_results")
        self.assertEqual(d["result_count"], 5)
        self.assertTrue(d["content_present"])
        self.assertTrue(d["content_verified"])
        self.assertTrue(d["input_matches_query"])
        self.assertEqual(d["input_evidence"]["field_count"], 1)
        self.assertFalse(d["input_evidence"]["ambiguous"])
        self.assertEqual(
            [c["label"] for c in d["real_result_candidates"]],
            ["Termux Hub", "Termux:Float", "Termux:Widget", "Termux:Tasker", "Termux:API"],
        )
        for c in d["real_result_candidates"]:
            self.assertTrue(c["summary"])
            self.assertTrue(c["row_node_id"].startswith(d["observation_id"][:12] + ":"))
            self.assertIsNotNone(c["label_node_id"])
            self.assertTrue(c["bounds"])
        # 'Apps' 标题不能被当成条目
        self.assertNotIn("Apps", [c["label"] for c in d["real_result_candidates"]])

    def test_every_candidate_points_at_a_real_node(self):
        _, obs, _, _ = observe_to_tmp(RESULTS, "obs-ev.json")
        known = set(ids(obs))
        _, d, _ = run_cli("evaluate-search", "--xml", RESULTS, "--query", "termux", expect_ok=True)
        for c in d["real_result_candidates"]:
            self.assertIn(c["row_node_id"], known)
            self.assertIn(c["label_node_id"], known)
            self.assertIn(c["summary_node_id"], known)

    def test_home_page_is_not_treated_as_search_screen(self):
        _, d, _ = run_cli("evaluate-search", "--xml", HOME, "--query", "termux", expect_ok=True)
        self.assertEqual(d["page_kind"], "not_a_search_screen")
        self.assertEqual(d["result_count"], 0)
        self.assertFalse(d["content_verified"])
        self.assertIn("no_search_input_field", [w["code"] for w in d["warnings"]])

    def test_concatenated_echo_fails_exact_match(self):
        _, d, _ = run_cli("evaluate-search", "--xml", SYN / "synthetic-echo-concat.xml",
                          "--query", "termux", expect_ok=True)
        self.assertFalse(d["input_matches_query"])
        self.assertTrue(d["input_evidence"]["substring_match"])
        self.assertEqual(d["input_evidence"]["raw_text"], "termuxtermux")
        self.assertEqual(d["result_count"], 0)
        self.assertFalse(d["content_verified"])

    def test_network_is_never_claimed_verified(self):
        for xml in (EMPTY_SEARCH, RESULTS, HOME):
            _, d, _ = run_cli("evaluate-search", "--xml", xml, "--query", "termux", expect_ok=True)
            self.assertEqual(d["network_verified"], "unknown")

    def test_synthetic_fixture_is_labelled_and_real_evidence_is_separate(self):
        syn = SYN / "synthetic-min-results.xml"
        self.assertIn("SYNTHETIC FIXTURE", syn.read_text(encoding="utf-8"))
        _, d, _ = run_cli("evaluate-search", "--xml", syn, "--query", "termux", expect_ok=True)
        self.assertEqual(d["page_kind"], "search_results")
        self.assertTrue(d["content_verified"])
        self.assertEqual([c["label"] for c in d["real_result_candidates"]], ["Termux"])
        # 真实正例来自 workbuddy explore 的既有 XML，不是合成
        real = RESULTS.read_text(encoding="utf-8")
        self.assertNotIn("SYNTHETIC", real)

    def test_non_fdroid_package_is_unsupported(self):
        other = TMP / "other-app.xml"
        other.write_text(
            "<?xml version='1.0' encoding='UTF-8'?><hierarchy rotation=\"0\">"
            "<node index=\"0\" class=\"android.widget.FrameLayout\" package=\"com.example.other\""
            " enabled=\"true\" clickable=\"false\" bounds=\"[0,0][720,1184]\">"
            "<node index=\"0\" class=\"android.widget.EditText\" package=\"com.example.other\""
            " text=\"termux\" clickable=\"true\" enabled=\"true\" bounds=\"[0,0][720,100]\"/>"
            "<node index=\"1\" class=\"android.view.View\" package=\"com.example.other\""
            " clickable=\"true\" enabled=\"true\" bounds=\"[0,100][720,300]\">"
            "<node index=\"0\" class=\"android.widget.TextView\" package=\"com.example.other\""
            " text=\"Termux\" clickable=\"false\" enabled=\"true\" bounds=\"[0,100][720,160]\"/>"
            "</node></node></hierarchy>",
            encoding="utf-8",
        )
        _, d, _ = run_cli("evaluate-search", "--xml", other, "--query", "termux", expect_ok=True)
        self.assertEqual(d["page_kind"], "unsupported_app")
        self.assertEqual(d["result_count"], 0)
        self.assertFalse(d["content_verified"])
        self.assertIn("not_fdroid_basic", [w["code"] for w in d["warnings"]])

    def test_empty_query_is_rejected(self):
        rc, d, err = run_cli("evaluate-search", "--xml", EMPTY_SEARCH, "--query", "   ")
        self.assertEqual(rc, 2)
        self.assertEqual(d["error"]["code"], "empty_query")
        self.assertTrue(err.strip())


# --------------------------------------------------------------------------
# v1.1.0 修补验收（dispatch/16）
# --------------------------------------------------------------------------

class QueryScopeTest(TmpMixin):
    """修补 1：content_present（页面有候选的事实）与 content_verified（绑定查询词）分开。"""

    def test_real_page_with_wrong_query_is_present_but_not_verified(self):
        """真实结果页 + 换词：候选照报，但不得判 verified。"""
        rc, d, err = run_cli("evaluate-search", "--xml", RESULTS, "--query", "fdroid",
                             expect_ok=True)
        self.assertEqual(rc, 0, err)
        self.assertEqual(d["result_count"], 5)              # 页面事实照实报
        self.assertEqual([c["label"] for c in d["real_result_candidates"]][0], "Termux Hub")
        self.assertTrue(d["content_present"])
        self.assertFalse(d["content_verified"])             # 查询词没有回显
        self.assertFalse(d["input_matches_query"])
        self.assertEqual(d["page_kind"], "search_results")  # page_kind 仍描述页面
        self.assertIn("query_not_confirmed", [w["code"] for w in d["warnings"]])

    def test_real_page_with_concatenated_echo_is_present_but_not_verified(self):
        """派生输入（不是新采集）：真实结果页字节，仅把回显改成 termuxtermux。

        原始快照未被改动，独立测试 `test_snapshot_files_match_upstream_hashes` 校验其 hash。
        """
        src = RESULTS.read_text(encoding="utf-8")
        self.assertEqual(src.count('text="termux"'), 1)
        derived = TMP / "derived-echo-concat.xml"
        derived.write_text(src.replace('text="termux"', 'text="termuxtermux"', 1), encoding="utf-8")

        _, d, _ = run_cli("evaluate-search", "--xml", derived, "--query", "termux", expect_ok=True)
        self.assertEqual(d["result_count"], 5)
        self.assertTrue(d["content_present"])
        self.assertFalse(d["content_verified"])
        self.assertFalse(d["input_matches_query"])           # 拼接词不是精确命中
        self.assertTrue(d["input_evidence"]["substring_match"])
        self.assertEqual(d["input_evidence"]["raw_text"], "termuxtermux")
        self.assertIn("query_not_confirmed", [w["code"] for w in d["warnings"]])
        self.assertEqual(sha256_file(RESULTS), RECORDED_SHA["hierarchy-search-results.xml"])

    def test_multiple_search_fields_never_auto_pick_the_first(self):
        """两个搜索框：即使树序第一个精确回显，也不得当作命中。"""
        _, d, _ = run_cli("evaluate-search", "--xml", SYN / "synthetic-multi-search-fields.xml",
                          "--query", "termux", expect_ok=True)
        self.assertEqual(d["input_evidence"]["field_count"], 2)
        self.assertTrue(d["input_evidence"]["ambiguous"])
        self.assertTrue(d["input_evidence"]["exact_match"])          # 第一个框确实回显 termux
        self.assertFalse(d["input_matches_query"])                   # 但仍不据此判命中
        self.assertEqual(d["result_count"], 1)
        self.assertTrue(d["content_present"])
        self.assertFalse(d["content_verified"])
        self.assertIn("multiple_search_fields", [w["code"] for w in d["warnings"]])
        self.assertTrue(d["input_evidence"]["reason_if_ambiguous"])
        # 回显证据指向第一个框，且该框没有变成候选
        self.assertEqual(
            d["input_evidence"]["matches_query_node_ids"],
            [d["input_evidence"]["node_id"]],
        )
        for cand in d["real_result_candidates"]:
            self.assertNotIn("EditText", cand["row_class"])

    def test_verified_scope_denies_freshness_and_network(self):
        _, d, _ = run_cli("evaluate-search", "--xml", RESULTS, "--query", "termux", expect_ok=True)
        self.assertTrue(d["content_verified"])
        self.assertEqual(d["network_verified"], "unknown")
        note = d["content_verified_note"].lower()
        self.assertIn("does not prove", note)
        self.assertIn("fresh", note)


class ChromeButtonTest(TmpMixin):
    """修补 2：工具栏/对话框动作按钮在候选生成阶段排除，不只是写进 excluded。"""

    def test_chrome_action_buttons_are_not_results(self):
        _, d, _ = run_cli("evaluate-search", "--xml", SYN / "synthetic-chrome-buttons.xml",
                          "--query", "termux", expect_ok=True)
        labels = [c["label"] for c in d["real_result_candidates"]]
        self.assertEqual(d["result_count"], 2)
        self.assertEqual(labels, ["Termux", "Settings Sync"])
        for bad in ("Cancel", "Settings", "OK"):
            self.assertNotIn(bad, labels)
            self.assertEqual(reason_for(d, text=bad), "toolbar_or_action_label", bad)

    def test_rows_merely_containing_a_chrome_word_are_kept(self):
        """整串精确匹配才排除：含 "settings" 词的 App 名与介绍不能被误删。"""
        _, d, _ = run_cli("evaluate-search", "--xml", SYN / "synthetic-chrome-buttons.xml",
                          "--query", "termux", expect_ok=True)
        kept = {c["label"]: c for c in d["real_result_candidates"]}
        self.assertIn("Settings Sync", kept)                 # 不是整串 "settings"
        self.assertEqual(kept["Settings Sync"]["summary"], "Sync your app settings across devices")
        # 真实五行的正例不受影响
        _, real, _ = run_cli("evaluate-search", "--xml", RESULTS, "--query", "termux", expect_ok=True)
        self.assertEqual(real["result_count"], 5)
        self.assertTrue(real["content_verified"])

    def test_real_theme_dialog_is_not_a_search_screen(self):
        rc, d, _ = run_cli("evaluate-search", "--xml", THEME_DIALOG, "--query", "termux",
                           expect_ok=True)
        self.assertEqual(rc, 0)
        self.assertEqual(d["page_kind"], "not_a_search_screen")
        self.assertEqual(d["result_count"], 0)
        self.assertFalse(d["content_present"])
        self.assertFalse(d["content_verified"])
        # 对话框里的 Cancel / Light / Dark 都没被凑成结果
        self.assertEqual(d["real_result_candidates"], [])
        _, obs, _, _ = observe_to_tmp(THEME_DIALOG, "obs-dialog.json")
        self.assertEqual(len(obs["nodes"]), 23)
        self.assertEqual(obs["apps"], ["org.fdroid.basic"])


class ResolveEnabledContractTest(TmpMixin):
    """修补 3：请求节点、途中节点、动作目标 enabled 都必须为 true。"""

    def _chain_nodes(self):
        return observe_to_tmp(SYN / "synthetic-disabled-chain.xml", "obs-chain.json")[1]

    def test_real_theme_and_real_dialog_dark_still_resolve(self):
        _, settings, _, _ = observe_to_tmp(SETTINGS, "obs-set-v11.json")
        theme = node_by_text(settings, "Theme")
        _, res, _ = run_cli("resolve", "--xml", SETTINGS, "--node-id", theme["node_id"],
                            "--action", "click", expect_ok=True)
        self.assertEqual(res["resolved_via"], "ancestor")
        self.assertEqual(res["plan"], {"action": "tap", "x": 360, "y": 356})

        _, dialog, _, _ = observe_to_tmp(THEME_DIALOG, "obs-dialog-v11.json")
        dark = node_by_text(dialog, "Dark")
        _, res2, _ = run_cli("resolve", "--xml", THEME_DIALOG, "--node-id", dark["node_id"],
                             "--action", "click", expect_ok=True)
        self.assertEqual(res2["resolved_via"], "ancestor")
        self.assertEqual(res2["bounds_used"], [40, 560, 680, 656])
        self.assertEqual(res2["plan"], {"action": "tap", "x": 360, "y": 608})

    def test_blocks_on_disabled_intermediate_node(self):
        d = self._chain_nodes()
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-disabled-chain.xml",
                             "--node-id", node_by_text(d, "Deep label")["node_id"], "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "blocked_by_disabled_node"))
        self.assertEqual(res["error"]["detail"]["enabled_state"], "false")
        self.assertIn("R.0.0.0", res["error"]["detail"]["blocked_node_id"])

    def test_disabled_and_unknown_targets_are_refused(self):
        d = self._chain_nodes()
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-disabled-chain.xml",
                             "--node-id", node_by_text(d, "In half row")["node_id"], "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "target_disabled"))
        self.assertEqual(res["error"]["detail"]["enabled_state"], "false")
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-disabled-chain.xml",
                             "--node-id", node_by_text(d, "In unknown row")["node_id"], "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "target_disabled"))
        self.assertEqual(res["error"]["detail"]["enabled_state"], "unknown")

    def test_requested_node_without_readable_enabled_is_refused(self):
        d = self._chain_nodes()
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-disabled-chain.xml",
                             "--node-id", node_by_text(d, "Label without enabled")["node_id"],
                             "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "requested_node_disabled"))
        self.assertEqual(res["error"]["detail"]["enabled_state"], "unknown")

    def test_dump_envelope_is_not_treated_as_a_blocking_node(self):
        """<hierarchy> 包裹元素没有 enabled 属性，不能因此把父链判成被阻断。"""
        d = self._chain_nodes()
        rc, res, _ = run_cli("resolve", "--xml", SYN / "synthetic-disabled-chain.xml",
                             "--node-id", node_by_text(d, "Label enabled")["node_id"],
                             "--action", "click")
        self.assertEqual((rc, res["error"]["code"]), (1, "no_supported_target"))

    def test_resolve_still_plans_for_a_button_that_search_excluded(self):
        """evaluate-search 不把 Cancel 当结果，但显式 resolve 仍能给它出计划（职责分离）。"""
        _, dialog, _, _ = observe_to_tmp(THEME_DIALOG, "obs-cancel.json")
        cancel = node_by_text(dialog, "Cancel")
        _, res, _ = run_cli("resolve", "--xml", THEME_DIALOG, "--node-id", cancel["node_id"],
                            "--action", "click", expect_ok=True)
        self.assertEqual(res["resolved_via"], "ancestor")
        self.assertEqual(res["plan"], {"action": "tap", "x": 563, "y": 824})


class OutputOverwriteFixTest(TmpMixin):
    """修补 4：覆盖保护用精确标识 + 基本结构校验。"""

    def _write(self, name, payload):
        p = TMP / name
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_generator_with_suffix_cannot_clobber(self):
        p = self._write("evil.json", {
            "generator": "content_adapter.py-evil", "module_version": "1.0.0",
            "observation_id": "x", "nodes": [], "source": "ui",
        })
        rc, d, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", p)
        self.assertEqual(rc, 2)
        self.assertEqual(d["error"]["code"], "refusing_to_overwrite_unknown_file")
        self.assertEqual(json.loads(p.read_text(encoding="utf-8"))["generator"], "content_adapter.py-evil")

    def test_exact_generator_without_output_structure_cannot_clobber(self):
        for name, payload in (
            ("bare.json", {"generator": "content_adapter.py"}),
            ("wrong-source.json", {
                "generator": "content_adapter.py", "module_version": "1.1.0",
                "observation_id": "x", "nodes": [], "source": "ocr",
            }),
        ):
            p = self._write(name, payload)
            rc, d, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", p)
            self.assertEqual(rc, 2, name)
            self.assertEqual(d["error"]["code"], "refusing_to_overwrite_unknown_file", name)

    def test_previous_version_output_is_still_overwritable(self):
        """旧版（v1.0.0）本工具输出兼容策略：结构与标识都满足即允许覆盖。"""
        p = self._write("old-own.json", {
            "ok": True, "generator": "content_adapter.py", "module_version": "1.0.0",
            "observation_id": "0" * 64, "source": "ui", "nodes": [{"node_id": "0:R"}],
        })
        rc, d, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", p, expect_ok=True)
        self.assertEqual(rc, 0)
        self.assertEqual(d["module_version"], "1.1.0")
        self.assertTrue(d["observation_id"].startswith("c13e5d5c"))


# --------------------------------------------------------------------------
# CLI 契约与输出策略
# --------------------------------------------------------------------------

class CliContractTest(TmpMixin):
    def test_stdout_is_a_single_json_line(self):
        out = TMP / "single.json"
        for args in (
            ("observe", "--xml", EMPTY_SEARCH, "--output", out),
            ("resolve", "--xml", SETTINGS, "--node-id", "x:y", "--action", "click"),
            ("evaluate-search", "--xml", EMPTY_SEARCH, "--query", "termux"),
        ):
            proc = subprocess.run([PY, CLI] + [str(a) for a in args], cwd=str(BASE),
                                  capture_output=True, text=True, encoding="utf-8")
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1, (args, proc.stdout[:200]))
            json.loads(lines[0])

    def test_malformed_xml_fails_with_diagnostics(self):
        rc, payload, err = run_cli("observe", "--xml", SYN / "synthetic-malformed.xml",
                                   "--output", TMP / "never.json")
        self.assertEqual(rc, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"]["code"], "xml_parse_error")
        self.assertTrue(err.strip())
        self.assertFalse((TMP / "never.json").exists())

    def test_missing_and_empty_xml_are_reported(self):
        rc, payload, _ = run_cli("observe", "--xml", TMP / "nope.xml", "--output", TMP / "x.json")
        self.assertEqual((rc, payload["error"]["code"]), (2, "input_not_found"))
        empty = TMP / "empty.xml"
        empty.write_bytes(b"   \n")
        rc, payload, _ = run_cli("observe", "--xml", empty, "--output", TMP / "y.json")
        self.assertEqual((rc, payload["error"]["code"]), (1, "xml_empty"))

    def test_help_is_not_json_and_exits_zero(self):
        proc = subprocess.run([PY, CLI, "--help"], cwd=str(BASE), capture_output=True,
                              text=True, encoding="utf-8")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("observe", proc.stdout)


class OutputPolicyTest(TmpMixin):
    def test_output_must_stay_inside_module_dir(self):
        outside = BASE.parent / "_should_not_exist.json"
        rc, payload, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", outside)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["error"]["code"], "output_path_outside_department")
        self.assertFalse(outside.exists())

    def test_output_extension_must_be_json(self):
        for target in (SYN / "synthetic-no-package.xml", BASE / "fixtures" / "SNAPSHOT.md"):
            rc, payload, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", target)
            self.assertEqual(rc, 2)
            self.assertEqual(payload["error"]["code"], "output_extension_not_allowed")
        self.assertIn("SYNTHETIC", (SYN / "synthetic-no-package.xml").read_text(encoding="utf-8"))

    def test_refuses_to_overwrite_unknown_json(self):
        decoy = TMP / "decoy.json"
        decoy.write_text('{"hello": "world"}', encoding="utf-8")
        rc, payload, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", decoy)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["error"]["code"], "refusing_to_overwrite_unknown_file")
        self.assertEqual(json.loads(decoy.read_text(encoding="utf-8")), {"hello": "world"})

    def test_overwrites_its_own_output(self):
        target = TMP / "mine.json"
        rc, first, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", target, expect_ok=True)
        self.assertEqual(rc, 0)
        rc, second, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", target, expect_ok=True)
        self.assertEqual(rc, 0)
        self.assertEqual(first["observation_id"], second["observation_id"])

    def test_directory_with_json_suffix_is_rejected(self):
        d = TMP / "dir.json"
        d.mkdir(parents=True, exist_ok=True)
        rc, payload, _ = run_cli("observe", "--xml", EMPTY_SEARCH, "--output", d)
        self.assertEqual(rc, 2)
        self.assertEqual(payload["error"]["code"], "output_is_directory")


class ModuleUnitTest(TmpMixin):
    """直接调用模块内部函数，覆盖 CLI 不便触发的分支。"""

    def test_bounds_parser_rules(self):
        self.assertEqual(ca.parse_bounds("[0,0][720,1184]"), [0, 0, 720, 1184])
        self.assertIsNone(ca.parse_bounds("[0,0][0,0]"))
        self.assertIsNone(ca.parse_bounds("[10,10][5,5]"))
        self.assertIsNone(ca.parse_bounds("garbage"))
        self.assertIsNone(ca.parse_bounds(None))
        self.assertEqual(ca.parse_bounds(" [0, 0] [2,2] "), [0, 0, 2, 2])

    def test_norm_rules(self):
        self.assertIsNone(ca.norm_str("   "))
        self.assertEqual(ca.norm_str(" x "), "x")
        self.assertIs(None, ca.norm_bool(None))
        self.assertIs(True, ca.norm_bool("true"))
        self.assertIs(False, ca.norm_bool("false"))
        self.assertIsNone(ca.norm_bool("yes"))

    def test_observation_ids_differ_between_snapshots(self):
        a = ca.Observation(EMPTY_SEARCH.read_bytes())
        b = ca.Observation(RESULTS.read_bytes())
        self.assertNotEqual(a.obs_key, b.obs_key)
        self.assertEqual(a.obs_key, a.sha256[:12])

    def test_depth_guard(self):
        deep = b"<hierarchy>" + b"<node>" * (ca.MAX_DEPTH + 5) + b"</node>" * (ca.MAX_DEPTH + 5) + b"</hierarchy>"
        with self.assertRaises(ca.AdapterError) as ctx:
            ca.Observation(deep)
        self.assertEqual(ctx.exception.code, "xml_too_deep")


if __name__ == "__main__":
    unittest.main(verbosity=2)
