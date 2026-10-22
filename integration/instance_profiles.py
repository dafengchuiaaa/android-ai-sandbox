"""Fixed, version-controlled instance profile definitions (round 4).

A profile binds one control session to one concrete Android instance: the AVD
name, the Android API level, the CPU ABI and the planned runtime paths. This
module is the single source of truth for those values.

Design rules (dispatch/33 "冻结协作接口 v1"):

* Callers select a profile **explicitly**. The default stays the round-3
  baseline, so old commands and old state files keep working.
* Nothing here is derived from a state file, from an arbitrary path or from an
  environment variable. A state file is data to be *checked against* the fixed
  definition, never a source of authority for it.
* An unknown profile id is a hard error, not a fallback.

Relative runtime paths are anchored on the project root (the parent of this
``integration`` package) so the canonical absolute value is identical in the
real project tree and in an isolated verification mirror.
"""

from __future__ import annotations

import os
from pathlib import Path

MODULE_VERSION = "1.0.0"

# project root == parent of this file's directory (…/integration/instance_profiles.py)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

BASELINE_PROFILE_ID = "baseline"
API35_PROFILE_ID = "api35-google-x86_64"
DEFAULT_PROFILE_ID = BASELINE_PROFILE_ID

# --- Fixed round-4 definitions ------------------------------------------------
# Paths are fixed by dispatch 33. They are NOT configurable by state/env.
API35_AVD_HOME = (
    PROJECT_ROOT / "departments" / "workbuddy" / "runtime" / "round4" / "avd"
)
API35_STATE_DIR = (
    PROJECT_ROOT / "departments" / "workbuddy" / "runtime" / "round4" / "instances"
)
API35_OBSERVE_ROOT = PROJECT_ROOT / "departments" / "antigravity" / "runtime" / "round4"
API35_IMAGE_DIR = Path(
    r"C:\Users\LOCAL_USER\AppData\Local\Android\Sdk"
    r"\system-images\android-35\google_apis\x86_64"
)


class ProfileError(Exception):
    """A profile id could not be resolved against the fixed definition table."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def normalize_path(value):
    """Canonical absolute path used for every profile/state comparison.

    ``realpath`` resolves symlinks/junctions and ``normcase`` removes
    Windows case differences, so an equivalent spelling of the right path is
    accepted while a different location never is.
    """
    if value is None:
        return None
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(value))))


class Profile:
    """One immutable, fixed instance definition."""

    __slots__ = (
        "profile_id",
        "api_level",
        "abi",
        "avd",
        "avd_home",
        "image_dir",
        "state_dir",
        "observe_root",
        "require_state_identity",
        "enforce_live_api",
    )

    def __init__(self, profile_id, api_level, abi, avd, avd_home=None, image_dir=None,
                 state_dir=None, observe_root=None,
                 require_state_identity=False, enforce_live_api=False):
        self.profile_id = profile_id
        self.api_level = api_level
        self.abi = abi
        self.avd = avd
        self.avd_home = normalize_path(avd_home) if avd_home is not None else None
        self.image_dir = normalize_path(image_dir) if image_dir is not None else None
        self.state_dir = str(state_dir) if state_dir is not None else None
        self.observe_root = str(observe_root) if observe_root is not None else None
        # A profile that carries new identity fields must have every one of them
        # present and exact in the state file before any device action.
        self.require_state_identity = require_state_identity
        # Baseline keeps the round-3 live check set (device presence, boot,
        # `emu avd name`). Only a profile that opts in additionally pins the
        # live Android API level and ABI, so baseline default behaviour is
        # unchanged rather than tightened.
        self.enforce_live_api = enforce_live_api

    def as_dict(self):
        return {
            "profile_id": self.profile_id,
            "api_level": self.api_level,
            "abi": self.abi,
            "avd": self.avd,
            "avd_home": self.avd_home,
            "image_dir": self.image_dir,
            "state_dir": self.state_dir,
            "observe_root": self.observe_root,
            "require_state_identity": self.require_state_identity,
            "enforce_live_api": self.enforce_live_api,
        }


_PROFILES = {
    BASELINE_PROFILE_ID: Profile(
        profile_id=BASELINE_PROFILE_ID,
        api_level=33,
        abi="x86_64",
        avd="ai-sandbox-baseline",
        require_state_identity=False,
        enforce_live_api=False,
    ),
    API35_PROFILE_ID: Profile(
        profile_id=API35_PROFILE_ID,
        api_level=35,
        abi="x86_64",
        avd="ai-sandbox-api35-google",
        avd_home=API35_AVD_HOME,
        image_dir=API35_IMAGE_DIR,
        state_dir=API35_STATE_DIR,
        observe_root=API35_OBSERVE_ROOT,
        require_state_identity=True,
        enforce_live_api=True,
    ),
}


def ids():
    """All known profile ids, in fixed definition order."""
    return tuple(_PROFILES)


def resolve(profile_id=None):
    """Return the fixed Profile for ``profile_id`` (default: baseline)."""
    if profile_id is None:
        return _PROFILES[DEFAULT_PROFILE_ID]
    if not isinstance(profile_id, str) or profile_id not in _PROFILES:
        raise ProfileError(
            "unknown_profile",
            "Unknown instance profile %r; known profiles: %s"
            % (profile_id, ", ".join(_PROFILES)),
        )
    return _PROFILES[profile_id]


def default():
    return _PROFILES[DEFAULT_PROFILE_ID]


def avd_for(profile_id=None):
    return resolve(profile_id).avd


def state_identity_problems(state, profile):
    """Check a state dict against the identity fields ``profile`` requires.

    Returns a list of ``{"code", "message", "detail"}`` problems; an empty list
    means the state carries this profile's identity exactly. A missing field is
    an incomplete state (``invalid_argument``); a present but different value is
    an ownership mismatch. Never raises.
    """
    if not profile.require_state_identity:
        return []
    if not isinstance(state, dict):
        return [{"code": "invalid_argument",
                 "message": "state must be a JSON object",
                 "detail": {}}]

    problems = []

    def check_text(field, expected):
        found = state.get(field)
        if field not in state or found is None:
            problems.append({
                "code": "invalid_argument",
                "message": "state is missing the %s field required by profile %s"
                           % (field, profile.profile_id),
                "detail": {"field": field, "expected": expected, "found": found},
            })
        elif not isinstance(found, str) or found != expected:
            problems.append({
                "code": "ownership_mismatch",
                "message": "state %s is %r, profile %s requires %r"
                           % (field, found, profile.profile_id, expected),
                "detail": {"field": field, "expected": expected, "found": found},
            })

    def check_path(field, expected):
        found = state.get(field)
        if field not in state or found is None:
            problems.append({
                "code": "invalid_argument",
                "message": "state is missing the %s field required by profile %s"
                           % (field, profile.profile_id),
                "detail": {"field": field, "expected": expected, "found": found},
            })
        elif not isinstance(found, str) or normalize_path(found) != expected:
            problems.append({
                "code": "ownership_mismatch",
                "message": "state %s resolves to %r, profile %s requires %r"
                           % (field, normalize_path(found) if isinstance(found, str) else found,
                              profile.profile_id, expected),
                "detail": {"field": field, "expected": expected,
                           "found": normalize_path(found) if isinstance(found, str) else found},
            })

    check_text("profile_id", profile.profile_id)
    check_text("abi", profile.abi)
    check_path("avd_home", profile.avd_home)
    check_path("image_dir", profile.image_dir)

    level = state.get("api_level")
    if "api_level" not in state or level is None:
        problems.append({
            "code": "invalid_argument",
            "message": "state is missing the api_level field required by profile %s"
                       % profile.profile_id,
            "detail": {"field": "api_level", "expected": profile.api_level, "found": level},
        })
    elif type(level) is not int or level != profile.api_level:
        problems.append({
            "code": "ownership_mismatch",
            "message": "state api_level is %r, profile %s requires %r"
                       % (level, profile.profile_id, profile.api_level),
            "detail": {"field": "api_level", "expected": profile.api_level, "found": level},
        })

    return problems
