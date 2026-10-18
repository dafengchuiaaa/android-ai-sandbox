#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
round4_profiles.py —— 两个 F-Droid 场景共用的**固定 profile 接线**（第四轮收尾）。

背景（dispatch/41）：搜索与详情场景原来只转发 `--adb/--state-file/--output-dir`，
非 baseline 的 profile 会被静默丢掉，回落到 baseline 后被 phone/sandbox 拒绝；
而且两个场景的 `--expect-avd` 仍写死 baseline，即使 profile 传过去了也会因为
AVD 期望不符而失败。本模块只做三件事，不碰控制逻辑：

1. **单一来源**：期望 AVD / API / ABI 一律从受版本控制的固定定义
   `integration/instance_profiles.py` 推导，本模块不复制任何第二份常量。
2. **冲突早失败**：显式 `--expect-avd` 与所选 profile 不一致时**在任何调用之前**拒绝，
   不能靠传一个任意 `--expect-avd` 绕过身份检查。
3. **转发与绑定**：给出要追加到 phone/sandbox argv 的 profile 参数，并校验响应
   （status / 观察 / evaluate-search）的 `profile_id` 与所选 profile 相符；
   非 baseline 时缺失 `profile_id` 也算不合格（防止拿到 baseline 形状的响应冒充）。

依赖：必须与核心补丁一起使用；缺少 `integration/instance_profiles.py` 时**明确报错**，
不静默回落到硬编码的 AVD。
"""

from __future__ import annotations

import sys
from pathlib import Path

MODULE_VERSION = "1.0.0"

SCENARIO_DIR = Path(__file__).resolve().parent          # departments/deepseek/scenarios
PROJECT_ROOT = SCENARIO_DIR.parents[2]                  # 项目根
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from integration import instance_profiles as _profiles
except ImportError as exc:                              # pragma: no cover - 环境缺核心补丁
    raise ImportError(
        "round4_profiles requires the round-4 core patch "
        "(integration/instance_profiles.py). Apply that patch together with this one. "
        "Underlying error: %s" % exc
    )

DEFAULT_PROFILE_ID = _profiles.DEFAULT_PROFILE_ID
BASELINE_PROFILE_ID = _profiles.BASELINE_PROFILE_ID


class ProfileSelectionError(Exception):
    """profile 选择阶段的错误：场景层再包成自己的 ScenarioError。"""

    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail


class ProfileSelection:
    """一次场景运行所选定的固定 profile。"""

    __slots__ = ("profile_id", "avd", "api_level", "abi", "require_state_identity",
                 "enforce_live_api", "explicit")

    def __init__(self, profile, explicit):
        self.profile_id = profile.profile_id
        self.avd = profile.avd
        self.api_level = profile.api_level
        self.abi = profile.abi
        self.require_state_identity = profile.require_state_identity
        self.enforce_live_api = profile.enforce_live_api
        self.explicit = explicit

    def is_baseline(self):
        return self.profile_id == BASELINE_PROFILE_ID

    def phone_args(self):
        """要追加到 phone/sandbox argv（公共参数之后、子命令之前）的参数。

        baseline 是两端的共同缺省值，省略与显式等价（冻结接口 v1：
        `--profile` 是显式选择非 baseline 实例的手段）；非 baseline **必须**显式传，
        否则调用会被拒绝。
        """
        if self.is_baseline():
            return []
        return ["--profile", self.profile_id]

    def as_dict(self):
        return {
            "profile_id": self.profile_id,
            "avd": self.avd,
            "api_level": self.api_level,
            "abi": self.abi,
            "require_state_identity": self.require_state_identity,
            "enforce_live_api": self.enforce_live_api,
            "explicit": self.explicit,
            "expect_avd_source": "fixed_profile_definition",
        }


def phone_args(profile_id):
    """要追加到 phone/sandbox argv 的 profile 参数（公共参数之后、子命令之前）。

    baseline 是两端的共同缺省值，省略与显式等价（冻结接口 v1：`--profile` 是
    显式选择非 baseline 实例的手段）；非 baseline **必须**显式传，否则调用会被拒绝。
    """
    if not profile_id or profile_id == BASELINE_PROFILE_ID:
        return []
    return ["--profile", profile_id]


def select(profile_id=None, expect_avd=None):
    """解析 `--profile`（缺省 baseline）并与显式 `--expect-avd` 对账。

    任何不一致都在这里失败，调用方**不得到达任何 phone/sandbox 调用**。
    """
    explicit = profile_id is not None and str(profile_id).strip() != ""
    if not explicit:
        profile_id = DEFAULT_PROFILE_ID
    try:
        profile = _profiles.resolve(profile_id)
    except _profiles.ProfileError as exc:
        raise ProfileSelectionError(
            exc.code, exc.message,
            profile_id=profile_id, known_profiles=list(_profiles.ids()),
        )

    if expect_avd is not None:
        text = str(expect_avd).strip()
        if not text:
            raise ProfileSelectionError(
                "invalid_argument", "--expect-avd must not be empty",
                profile_id=profile.profile_id, profile_avd=profile.avd,
            )
        if text != profile.avd:
            raise ProfileSelectionError(
                "expect_avd_conflict",
                "--expect-avd %r conflicts with profile '%s' (fixed AVD %r); "
                "refusing to bypass the profile identity check"
                % (text, profile.profile_id, profile.avd),
                expect_avd=text, profile_id=profile.profile_id, profile_avd=profile.avd,
            )
    return ProfileSelection(profile, explicit)


def profile_binding_problems(profile_id, data):
    """响应/载荷的 `profile_id` 是否与所选 profile 相符（返回问题列表，不抛异常）。

    规则与冻结的独立校验器一致：非 baseline 必须显式声明且完全相等；
    baseline 允许省略（旧快照没有该字段），但不得声明成别的 profile。
    """
    if not isinstance(data, dict):
        return ["payload is not an object, cannot check the profile binding"]
    found = data.get("profile_id")
    if found is None:
        if profile_id != BASELINE_PROFILE_ID:
            return ["payload does not report profile_id, required by profile '%s'" % profile_id]
        return []
    if found != profile_id:
        return ["payload profile_id %r does not match the selected profile %r" % (found, profile_id)]
    return []
