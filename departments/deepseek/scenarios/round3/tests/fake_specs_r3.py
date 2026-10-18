#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round3 测试工具：把 round3 的（合成）页面与**真实**搜索快照变成 sandbox CLI 形状的
假响应脚本，复用第二轮已交付的 `scenarios/tests/fake_specs.py` 与 `fake_phone.py`
（只读引用，不修改）。

* 合成 fixture 只用于离线用例，文件与注释都标 SYNTHETIC；**不是**真机支持证据。
* 真实快照（`departments/deepseek/fixtures/originals/`）是第二轮已验证的真实 dump，
  这里只读复用。
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROUND3_DIR = HERE.parent                      # scenarios/round3
SCEN_DIR = ROUND3_DIR.parent                  # scenarios/
for extra in (str(SCEN_DIR / "tests"), str(SCEN_DIR)):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import fake_specs as r2          # noqa: E402  第二轮交付的离线工具（只读复用）

# 假 CLI：round3 通过 sandbox.py 调用，argv 外形与 phone CLI 相同，
# 所以第二轮那个假 phone CLI 可以直接当"假 sandbox"用。
FAKE_CLI = SCEN_DIR / "tests" / "fake_phone.py"

SYN = ROUND3_DIR / "fixtures" / "synthetic"
RESULTS_TERMUX = SYN / "synthetic-results-termux.xml"          # 合成：含精确 Termux 行
RESULTS_DUPLICATE = SYN / "synthetic-results-duplicate-label.xml"  # 合成：两个同名行
DETAIL_TERMUX = SYN / "synthetic-detail-termux.xml"            # 合成：详情页样例
TITLE_ONLY = SYN / "synthetic-title-only.xml"                   # 合成：只有标题文本
RESULTS_REAL = r2.ORIG / "hierarchy-search-results.xml"         # 真实：Termux 系结果页
EMPTY_REAL = r2.ORIG / "hierarchy-20261015T024231-c934f0c7.xml"  # 真实：空态页

# 直接转出第二轮的工具函数
observation_payload = r2.observation_payload
evaluate_payload = r2.evaluate_payload
status_ok = r2.status_ok
launch_ok = r2.launch_ok
click_ok = r2.click_ok
set_text_ok = r2.set_text_ok
evaluate_ok = r2.evaluate_ok
phone_error = r2.phone_error
write_spec = r2.write_spec
read_log = r2.read_log
read_spec = r2.read_spec
spawn_env = r2.spawn_env
AVD = r2.AVD
DEVICE_SERIAL = r2.DEVICE_SERIAL
OBSERVED_AT = r2.OBSERVED_AT


def search_flow(query, page, evaluate_from=None, details_page=None, extra_tail=()):
    """一段完整的假脚本：status → launch → click → set-text → evaluate-search → click 目标行。

    * `after_observation` 默认仍是**同一个搜索页**（模拟"点了但没进详情"，用于反例）；
    * 传 `details_page` 才表示点击后页面变成了详情页。
    """
    page_obs = observation_payload(page)
    after = observation_payload(details_page) if details_page is not None else page_obs
    calls = [
        status_ok(),
        launch_ok(page_obs),
        click_ok(page_obs),
        set_text_ok(page_obs, query),
        evaluate_ok(evaluate_payload(evaluate_from or page, query)),
        click_ok(after),
    ]
    calls.extend(extra_tail)
    return calls
