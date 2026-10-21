#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task 97 Gemini Turnaround Inspector and Compact Briefing Tool (r3).

Provides a unified, compact, single-turn inspection entry for external AI decisions:
- Verification of authoritative current-session.json commit boundary:
    * phase, next_seq, last_response_sha256 matching response byte SHA-256
    * uncommitted newer responses return pending
    * file tampering or hash mismatch returns tampered_or_hash_mismatch
    * historical/explicit file analysis strictly marked diagnostic_only with actions disabled
    * detects state race conditions if state changes during inspection
- Unified Turnaround Context Sidecar (turnaround-context.json):
    * shared by embedded briefing and standalone CLI
    * tracks refresh_dispatched lock, seen_in_progress, ui_verified_completed, observe counts
    * missing, expired, or desynchronized sidecar disables action suggestions (never guesses)
- Accurate realtime budget evaluation:
    * deadline-based monotonic decrement against reliable session deadline contract
    * clock rollback or anomaly triggers unknown_clock_anomaly and forbids clicks
    * unconfirmed or missing budget strictly defaults to can_click = False
- Refresh phase awareness:
    * once refresh is dispatched, click is strictly forbidden
    * distinguishes "button looks enabled" from "independently verified completed"
    * if seen_in_progress is False, awaits progress observation before claiming completed
    * when observe quota is exhausted, observe is never recommended
    * once ui_verified_completed, recommends finish for network logcat extraction
    * clearly distinguishes budget viability from Policy authorization
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Dict, List, Optional, Tuple, Union

# ----------------- Threshold Constants -----------------
CLICK_THRESHOLD_S = 70.0    # Worker requires CLICK_BUDGET_S(50) + OBSERVE_BUDGET_S(20)
OBSERVE_THRESHOLD_S = 20.0  # Worker requires OBSERVE_BUDGET_S(20)
FINISH_THRESHOLD_S = 20.0   # Supervisor reserved finish submit timeout
MAX_REFRESH_OBSERVES = 2    # Maximum observe actions allowed during refresh phase
MAX_TOTAL_OBSERVES = 3      # Maximum total observe actions allowed in session
TARGET_PACKAGE = "org.fdroid.basic"
SIDECAR_FILENAME = "turnaround-context.json"


def _resolve_stepwise_session_imports():
    """Use the worker classifier; unavailable dependencies never authorize candidates."""
    tools_dir = Path(__file__).resolve().parents[3] / "opensource/android-ai-sandbox/tools"
    if str(tools_dir) not in sys.path:
        sys.path.insert(0, str(tools_dir))
    try:
        from stepwise_session import is_permitted_refresh_click_target, is_navigation_recovery_response, official_repository_visible
        return is_permitted_refresh_click_target
    except ImportError:
        return None


is_permitted_fn = _resolve_stepwise_session_imports()


def _dependency_unavailable_briefing() -> Dict[str, Any]:
    return {
        "ok": False,
        "status": "dependency_unavailable",
        "reason": "stepwise_session classifier could not be imported",
        "diagnostic_only": True,
        "diagnostic_warning": "Worker classifier unavailable; action suggestions disabled.",
        "candidates": [],
        "permitted_actions": [],
        "recommended_action": None,
        "recommended_target_node_id": None,
    }


def extract_observation_from_payload(payload: Any) -> Optional[Dict[str, Any]]:
    """Extract observation dict from response data."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data") if "data" in payload and isinstance(payload["data"], dict) else payload
    if "after_observation" in data and isinstance(data["after_observation"], dict):
        return data["after_observation"]
    if "nodes" in data and isinstance(data["nodes"], list) and "observation_id" in data:
        return data
    if "observation" in data and isinstance(data["observation"], dict):
        return data["observation"]
    return None


def extract_repo_info(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Extract repository title, address, and verification of official F-Droid repo."""
    info = {
        "repo_title": None,
        "repo_address": None,
        "is_official_fdroid": False,
        "last_update_status": None,
    }
    url_pattern = re.compile(r"https?://[^\s]+", re.IGNORECASE)

    for n in nodes:
        text = (n.get("text") or "").strip()
        desc = (n.get("content_desc") or "").strip()

        # Check for URL address
        m = url_pattern.search(text) or url_pattern.search(desc)
        if m and not info["repo_address"]:
            info["repo_address"] = m.group(0).rstrip(".,;")

        # Check for repo title
        if text == "F-Droid" and not info["repo_title"]:
            info["repo_title"] = "F-Droid"

        # Check for last check status
        if "last check" in text.lower() or "published" in text.lower():
            info["last_update_status"] = text

    if is_permitted_fn is not None:
        from stepwise_session import official_repository_visible
        info['is_official_fdroid'] = official_repository_visible({'nodes': nodes})
        info['identity_proof'] = ('full_url' if any(n.get('package') == TARGET_PACKAGE and
            n.get('text') == 'https://f-droid.org/repo' for n in nodes) else 'pinned_fingerprint') if info['is_official_fdroid'] else None
    return info


def detect_progress_and_refresh_state(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Detect ProgressBars, spinning indicators, and assess whether UI refresh is in-progress."""
    progress_nodes = []
    in_progress = False

    progress_keywords = ("updating", "checking for update", "syncing", "refreshing", "fetching")

    for n in nodes:
        cls_name = (n.get("class") or "").lower()
        text = (n.get("text") or "").lower()
        desc = (n.get("content_desc") or "").lower()

        is_pb = (
            "progressbar" in cls_name
            or "progressindicator" in cls_name
            or "circularprogress" in cls_name
        )
        has_kw = any(kw in text or kw in desc for kw in progress_keywords)

        if is_pb or has_kw:
            progress_nodes.append({
                "node_id": n.get("node_id"),
                "class": n.get("class"),
                "bounds": n.get("bounds"),
                "text": n.get("text"),
                "content_desc": n.get("content_desc"),
                "indicator_type": "widget" if is_pb else "text_indicator"
            })
            in_progress = True

    return {
        "progress_bars_detected": len(progress_nodes) > 0,
        "progress_count": len(progress_nodes),
        "progress_nodes": progress_nodes,
        "ui_refresh_in_progress": in_progress,
    }


def refresh_control_state(obs: Dict[str, Any]) -> Dict[str, bool]:
    """Observe one semantic control and its worker-admitted clickable target.

    A nonclickable semantic child is not disabled. Missing/ambiguous targets
    cannot establish completion; the worker retains exact identity authority.
    """
    unknown = dict(enabled=False, disabled=False)
    if is_permitted_fn is None:
        return unknown
    nodes = [n for n in obs.get('nodes', []) if isinstance(n, dict)]
    controls = [n for n in nodes if n.get('package') == TARGET_PACKAGE and
                n.get('content_desc') == 'Check for updates']
    if len(controls) != 1:
        return unknown
    semantic = controls[0]
    if semantic.get('bounds_valid') is not True or semantic.get('password') is True:
        return unknown
    if semantic.get('enabled') is False:
        return dict(enabled=False, disabled=True)
    if semantic.get('enabled') is not True:
        return unknown
    for node in nodes:
        ok, reason, sem = is_permitted_fn(node, obs)
        if (ok and reason.startswith('check_for_updates') and sem == semantic and
                node.get('enabled') is True and node.get('clickable') is True):
            return dict(enabled=True, disabled=False)
    return unknown


def extract_navigation_candidates(nodes: List[Dict[str, Any]], obs: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract permitted navigation candidates and record disabled/ineligible controls."""
    if is_permitted_fn is None:
        return [], []
    candidates = []
    disabled_controls = []
    seen_target_ids = set()
    control = refresh_control_state(obs)

    for node in nodes:
        node_id = node.get("node_id")
        desc = (node.get("content_desc") or "").strip()
        text = (node.get("text") or "").strip()
        enabled = node.get("enabled", True)
        clickable = node.get("clickable", False)

        # 1. Check if node is permitted via production classifier
        ok, reason, sem_node = is_permitted_fn(node, obs)
        if ok and reason.startswith('check_for_updates') and not extract_repo_info(nodes)['is_official_fdroid']:
            ok = False
        if ok and sem_node:
            if node_id not in seen_target_ids:
                seen_target_ids.add(node_id)
                cand_type = reason.replace("_container", "")
                is_wrapper = (node_id != sem_node.get("node_id"))
                candidates.append({
                    "candidate_type": cand_type,
                    "target_node_id": node_id,
                    "target_bounds": node.get("bounds"),
                    "target_clickable": clickable,
                    "semantic_node_id": sem_node.get("node_id"),
                    "semantic_label": sem_node.get("text") or sem_node.get("content_desc"),
                    "wrapper_relationship": "tight_wrapper" if is_wrapper else "direct_target",
                    "policy_permitted": True,
                    "policy_reason": reason,
                    "enabled": enabled,
                    "actions": node.get("actions", ["click"])
                })

        # 2. Check for disabled or non-clickable refresh controls (preserving disabled state)
        if desc == "Check for updates" and node.get('package') == TARGET_PACKAGE:
            if control['disabled'] and enabled is False:
                disabled_controls.append({
                    "node_id": node_id,
                    "class": node.get("class"),
                    "content_desc": desc or text,
                    "enabled": enabled,
                    "clickable": clickable,
                    "bounds": node.get("bounds"),
                    "reason": "disabled_refresh_button"
                })

    return candidates, disabled_controls


def infer_screen_type(nodes: List[Dict[str, Any]],
                      candidates: List[Dict[str, Any]],
                      repo_info: Dict[str, Any],
                      refresh_dispatched: bool = False,
                      seen_in_progress: bool = False,
                      ui_verified_completed: bool = False,
                      progress_info: Optional[Dict[str, Any]] = None,
                      disabled_controls: Optional[List[Dict[str, Any]]] = None) -> str:
    """Infer the current application screen with refresh phase awareness."""
    cand_types = {c["candidate_type"] for c in candidates}
    has_cfu = "check_for_updates" in cand_types
    has_disabled_cfu = any("check for updates" in d.get("content_desc", "").lower() for d in (disabled_controls or []))
    in_prog = progress_info.get("ui_refresh_in_progress", False) if progress_info else False

    if refresh_dispatched:
        if ui_verified_completed:
            return "refresh_completed"
        if in_prog or has_disabled_cfu:
            return "refresh_in_progress"
        if seen_in_progress and has_cfu and not in_prog:
            return "refresh_completed"
        return "refresh_dispatched_awaiting_progress"

    if has_cfu or 'repository_fingerprint_header' in cand_types:
        return "repo_details"
    if "dialog_got_it" in cand_types:
        return "repo_list_intro_dialog"
    if "fdroid_repo_item" in cand_types:
        return "repo_list"
    if "repositories_tab" in cand_types:
        return "home_screen"

    # Secondary text heuristics
    all_texts = " ".join((n.get("text") or "") + " " + (n.get("content_desc") or "") for n in nodes).lower()
    if "repository details" in all_texts:
        return "repo_details"
    if "repository list" in all_texts and "got it" in all_texts:
        return "repo_list_intro_dialog"
    if "repositories" in all_texts and ("last check for repo update" in all_texts or "new repository" in all_texts):
        return "repo_list"
    if "new apps" in all_texts and "recently updated" in all_texts:
        return "home_screen"

    return "unknown"


def evaluate_budget_and_actions(cur_session: Optional[Dict[str, Any]],
                                sidecar_data: Optional[Dict[str, Any]],
                                screen_type: str,
                                candidates: List[Dict[str, Any]],
                                ui_in_progress: bool,
                                now_monotonic: Optional[float] = None,
                                now_epoch: Optional[float] = None,
                                is_failed_step: bool = False,
                                is_diagnostic_only: bool = False) -> Dict[str, Any]:
    """Evaluate realtime budget and phase-driven actionable suggestions.

    Strict guarantees:
    - Same-host monotonic clock domain ("windows_monotonic") shared between supervisor and CLI.
    - Unconfirmed, missing, domain-mismatched, or anomalous budget strictly defaults to can_click = False.
    - Uses reliable deadline contract from sidecar; monotonic time strictly prevents wall-clock manipulation.
    - Refresh phase driven by verified sidecar:
      * once refresh_dispatched, click is strictly forbidden.
      * distinguishes button looks enabled from independently verified completion.
      * enforces observe quotas (never recommends observe if quota exhausted).
    """
    eval_utc = datetime.now(timezone.utc).isoformat()
    if now_monotonic is None:
        now_monotonic = time.monotonic()
    if now_epoch is None:
        now_epoch = time.time()

    cur_phase = cur_session.get("phase") if cur_session else None
    snapshot_rem_s = cur_session.get("remaining_operational_s") if cur_session else None
    elapsed_s = cur_session.get("elapsed_s") if cur_session else None
    next_seq = cur_session.get("next_seq") if cur_session else None
    serial = cur_session.get("serial") if cur_session else None

    # Sidecar fields
    sidecar_valid = False
    refresh_dispatched = False
    seen_in_progress = False
    ui_verified_completed = False
    total_observes_used = 0
    refresh_observes_used = 0
    max_total_observes = MAX_TOTAL_OBSERVES
    max_refresh_observes = MAX_REFRESH_OBSERVES
    operational_deadline_monotonic = None
    last_updated_monotonic = None
    clock_domain = None
    clock_error_msg = None

    if sidecar_data and isinstance(sidecar_data, dict):
        clock_domain = sidecar_data.get("clock_domain")
        # Validate clock domain
        if clock_domain not in ("windows_monotonic", "monotonic"):
            sidecar_valid = False
            clock_error_msg = f"unsupported_clock_domain: '{clock_domain}'"
        else:
            sidecar_valid = True
            refresh_dispatched = bool(sidecar_data.get("refresh_dispatched", False))
            seen_in_progress = bool(sidecar_data.get("seen_in_progress", False))
            ui_verified_completed = bool(sidecar_data.get("ui_verified_completed", False))
            total_observes_used = int(sidecar_data.get("total_observes_used", 0))
            refresh_observes_used = int(sidecar_data.get("refresh_observes_used", 0))
            max_total_observes = int(sidecar_data.get("max_total_observes", MAX_TOTAL_OBSERVES))
            max_refresh_observes = int(sidecar_data.get("max_refresh_observes", MAX_REFRESH_OBSERVES))
            operational_deadline_monotonic = sidecar_data.get("operational_deadline_monotonic")
            last_updated_monotonic = sidecar_data.get("last_updated_monotonic")

    # 1. Realtime Budget Evaluation & Clock Anomaly Check
    rem_op_s: Optional[float] = None
    budget_evaluated_mode = "unknown"
    is_clock_anomaly = False

    if not sidecar_valid:
        rem_op_s = None
        if clock_error_msg:
            is_clock_anomaly = True
            budget_evaluated_mode = "unknown_clock_domain_mismatch"
        else:
            budget_evaluated_mode = "unknown_unconfirmed_budget"
    elif operational_deadline_monotonic is not None:
        if last_updated_monotonic is not None and now_monotonic < (last_updated_monotonic - 0.001):
            # Monotonic clock anomaly: now_monotonic is smaller than last_updated_monotonic!
            is_clock_anomaly = True
            budget_evaluated_mode = "unknown_clock_anomaly"
            rem_op_s = None
        else:
            raw_rem = operational_deadline_monotonic - now_monotonic
            # Cap by maximum possible remaining based on worker elapsed_s if known
            if elapsed_s is not None and cur_session and cur_session.get("max_session_s") and cur_session.get("cleanup_reserved_s"):
                max_op = cur_session["max_session_s"] - cur_session["cleanup_reserved_s"]
                max_rem_by_worker = max(0.0, max_op - elapsed_s)
                raw_rem = min(raw_rem, max_rem_by_worker)
            rem_op_s = max(0.0, float(raw_rem))
            budget_evaluated_mode = "sidecar_monotonic_deadline_realtime"
    else:
        rem_op_s = None
        budget_evaluated_mode = "unknown_unconfirmed_budget"

    # Compute viability
    can_click = (rem_op_s is not None and rem_op_s >= CLICK_THRESHOLD_S)
    can_observe = (rem_op_s is not None and rem_op_s >= OBSERVE_THRESHOLD_S)
    can_finish = (rem_op_s is not None and rem_op_s >= FINISH_THRESHOLD_S)

    budget_viable_actions = []
    if can_click:
        budget_viable_actions.append("click")
    if can_observe:
        budget_viable_actions.append("observe")
    if can_finish:
        budget_viable_actions.append("finish")

    # Budget status text
    if is_clock_anomaly:
        budget_status = "unknown_clock_anomaly"
        if clock_error_msg:
            explanation = f"Clock domain mismatch or unsupported: {clock_error_msg}; budget cannot be confirmed."
        else:
            explanation = f"Monotonic clock anomaly detected (now_monotonic {now_monotonic:.3f} < last_updated_monotonic {last_updated_monotonic:.3f}); budget cannot be confirmed."
    elif rem_op_s is None:
        budget_status = "unknown"
        explanation = "Operational budget is unconfirmed (missing or untrusted sidecar deadline contract); cannot confirm realtime balance."
    elif can_click:
        budget_status = "sufficient"
        explanation = f"Realtime operational budget ({rem_op_s:.1f}s) is sufficient for click (threshold={CLICK_THRESHOLD_S}s: 50s click + 20s observe)."
    elif can_observe:
        budget_status = "insufficient_for_click_observe_only"
        explanation = (
            f"Realtime operational budget ({rem_op_s:.1f}s) is below click threshold ({CLICK_THRESHOLD_S}s: 50s click + 20s observe). "
            f"Click will be rejected by worker TimeoutError; observe ({OBSERVE_THRESHOLD_S}s) or finish ({FINISH_THRESHOLD_S}s) allowed."
        )
    elif can_finish:
        budget_status = "insufficient_finish_only"
        explanation = f"Realtime operational budget ({rem_op_s:.1f}s) only allows finish ({FINISH_THRESHOLD_S}s)."
    else:
        budget_status = "exhausted"
        explanation = f"Realtime operational budget ({rem_op_s:.1f}s) is fully exhausted."

    # 2. Phase-Driven Permitted Actions and Quotas
    total_observes_remaining = max(0, max_total_observes - total_observes_used)
    refresh_observes_remaining = max(0, max_refresh_observes - refresh_observes_used)

    permitted_actions: List[str] = []
    recommended_action: Optional[str] = None
    recommended_target_node_id: Optional[str] = None
    recommended_reason: Optional[str] = None

    session_mode = (cur_session.get("mode") if cur_session else None) or (sidecar_data.get("mode") if sidecar_data else "application_refresh")
    if session_mode == "stepwise_input":
        permitted_actions = []
        recommended_action = None
        recommended_target_node_id = None
        recommended_reason = "Mode is stepwise_input; refresh action suggestions disabled. Action decisions must be derived directly from full formal observation."
    elif is_diagnostic_only or not sidecar_valid or is_clock_anomaly:
        permitted_actions = []
        recommended_action = None
        recommended_reason = explanation if is_clock_anomaly else "Diagnostic only mode or unconfirmed turnaround context; action suggestions disabled."
    elif is_failed_step or cur_phase in ("stopping", "stopped", "failed", "completed"):
        permitted_actions = []
        recommended_action = None
        recommended_reason = "Step failed or session terminal/completed; no further actions permitted."
    elif screen_type == 'navigation_observation_required':
        if can_observe and total_observes_used == 0 and not refresh_dispatched:
            permitted_actions.append('observe')
        if can_finish:
            permitted_actions.append('finish')
        recommended_action = 'observe' if 'observe' in permitted_actions else ('finish' if 'finish' in permitted_actions else None)
        recommended_reason = 'Previous navigation sent no input; obtain a new formal observation before a new decision.'
    elif refresh_dispatched:
        # REFRESH DISPATCHED: CLICK IS PERMANENTLY FORBIDDEN!
        allow_observe = can_observe and (refresh_observes_remaining > 0) and (total_observes_remaining > 0)
        if allow_observe:
            permitted_actions.append("observe")
        if can_finish:
            permitted_actions.append("finish")

        if ui_verified_completed:
            recommended_action = "finish"
            recommended_reason = "Repository refresh independently verified (progress seen and button re-enabled); submit finish for network logcat extraction."
        elif ui_in_progress or screen_type == "refresh_in_progress":
            if allow_observe:
                recommended_action = "observe"
                recommended_reason = f"Refresh in progress; observe UI state to detect completion and button re-enable (observes remaining: refresh={refresh_observes_remaining}, total={total_observes_remaining})."
            else:
                recommended_action = "finish"
                recommended_reason = "Observe quota or budget exhausted during refresh in progress; submit finish for network logcat extraction."
        else:
            # Button looks enabled but seen_in_progress is False: cannot claim verified completed!
            if allow_observe:
                recommended_action = "observe"
                recommended_reason = f"Refresh dispatched; observe screen to capture sync transition before declaring completion (observes remaining={refresh_observes_remaining})."
            else:
                recommended_action = "finish"
                recommended_reason = "Observe quota exhausted before sync in-progress could be observed; proceed to finish."
    else:
        # PRE-REFRESH NAVIGATION PHASE:
        permitted_actions = list(budget_viable_actions)
        if not can_click:
            recommended_action = "finish"
            recommended_reason = f"Realtime budget ({rem_op_s if rem_op_s is not None else 0.0:.1f}s) below 70.0s click threshold; click prohibited, finish recommended."
        else:
            cand_map = {c["candidate_type"]: c for c in candidates}
            if 'repository_fingerprint_header' in cand_map:
                c = cand_map['repository_fingerprint_header']
                recommended_action = 'click'
                recommended_target_node_id = c['target_node_id']
                recommended_reason = 'Expand only the signing fingerprint header to verify the pinned official repository identity.'
            elif "check_for_updates" in cand_map:
                c = cand_map["check_for_updates"]
                recommended_action = "click"
                recommended_target_node_id = c["target_node_id"]
                recommended_reason = "Trigger Check for updates on official repository details"
            elif "fdroid_repo_item" in cand_map:
                c = cand_map["fdroid_repo_item"]
                recommended_action = "click"
                recommended_target_node_id = c["target_node_id"]
                recommended_reason = "Open official F-Droid repository details from repository list"
            elif "dialog_got_it" in cand_map:
                c = cand_map["dialog_got_it"]
                recommended_action = "click"
                recommended_target_node_id = c["target_node_id"]
                recommended_reason = "Dismiss repository list introduction dialog"
            elif "repositories_tab" in cand_map:
                c = cand_map["repositories_tab"]
                recommended_action = "click"
                recommended_target_node_id = c["target_node_id"]
                recommended_reason = "Navigate from home screen to Repositories tab"
            else:
                if can_observe and total_observes_remaining > 0:
                    recommended_action = "observe"
                    recommended_reason = "No target identified; observe screen to re-evaluate"
                else:
                    recommended_action = "finish"
                    recommended_reason = "No target identified and cannot observe; submit finish"

    return {
        "budget_evaluation_utc": eval_utc,
        "budget_evaluated_mode": budget_evaluated_mode,
        "is_snapshot": False,
        "snapshot_remaining_operational_s": snapshot_rem_s,
        "current_remaining_operational_s": round(rem_op_s, 3) if rem_op_s is not None else None,
        "elapsed_s": elapsed_s,
        "click_threshold_s": CLICK_THRESHOLD_S,
        "observe_threshold_s": OBSERVE_THRESHOLD_S,
        "finish_threshold_s": FINISH_THRESHOLD_S,
        "can_click": can_click,
        "can_observe": can_observe,
        "can_finish": can_finish,
        "budget_status": budget_status,
        "explanation": explanation,
        "budget_viable_actions": budget_viable_actions,
        "permitted_actions": permitted_actions,
        "recommended_action": recommended_action,
        "recommended_target_node_id": recommended_target_node_id,
        "recommended_reason": recommended_reason,
        "turnaround_context": {
            "sidecar_valid": sidecar_valid,
            "clock_domain": clock_domain,
            "refresh_dispatched": refresh_dispatched,
            "seen_in_progress": seen_in_progress,
            "ui_verified_completed": ui_verified_completed,
            "total_observes_used": total_observes_used,
            "total_observes_remaining": total_observes_remaining,
            "refresh_observes_used": refresh_observes_used,
            "refresh_observes_remaining": refresh_observes_remaining,
            "operational_deadline_monotonic": operational_deadline_monotonic,
            "last_updated_monotonic": last_updated_monotonic,
            "session_start_epoch": sidecar_data.get("session_start_epoch") if sidecar_data else None,
            "operational_deadline_epoch": sidecar_data.get("operational_deadline_epoch") if sidecar_data else None,
            "last_updated_epoch": sidecar_data.get("last_updated_epoch") if sidecar_data else None,
        },
        "next_seq": next_seq,
        "device_serial": serial,
        "policy_notice": "Budget viability and phase suggestions do NOT imply Policy admission. Production Policy and stable direct UI read retain final authority."
    }


def recalculate_cached_briefing(cached_briefing: Union[Dict[str, Any], Path, str],
                               session_dir: Optional[Path] = None,
                               current_session_file: Optional[Path] = None,
                               now_monotonic: Optional[float] = None,
                               now_epoch: Optional[float] = None) -> Dict[str, Any]:
    """Re-evaluate realtime budget and action viability of a cached briefing without re-parsing XML/trees.

    Guarantees:
    - Verifies current-session.json and turnaround-context.json still match the briefing's response_sha256.
    - If desynchronized, tampered, or state advanced, reports pending or diagnostic_only.
    - Re-evaluates remaining operational budget using sidecar deadline contract:
      * wait time is accurately deducted against monotonic / sidecar deadline.
      * clock anomaly triggers unknown_clock_anomaly and strictly forbids clicks.
      * unconfirmed budget strictly defaults to can_click = False.
    - Updates permitted_actions and recommended_action without re-extracting nodes.
    """
    if is_permitted_fn is None:
        return _dependency_unavailable_briefing()

    if now_monotonic is None:
        now_monotonic = time.monotonic()
    if now_epoch is None:
        now_epoch = time.time()

    if isinstance(cached_briefing, (str, Path)):
        p = Path(cached_briefing)
        if not p.is_file():
            return {
                "ok": False,
                "status": "cached_briefing_not_found",
                "error": f"cached_briefing_not_found: {p}",
                "diagnostic_only": True,
                "permitted_actions": []
            }
        try:
            briefing = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            return {
                "ok": False,
                "status": "malformed_cached_briefing",
                "error": f"malformed_cached_briefing: {exc}",
                "diagnostic_only": True,
                "permitted_actions": []
            }
    elif isinstance(cached_briefing, dict):
        briefing = copy.deepcopy(cached_briefing)
    else:
        return {
            "ok": False,
            "status": "invalid_cached_briefing_type",
            "error": "cached_briefing must be dict, Path, or str",
            "diagnostic_only": True,
            "permitted_actions": []
        }

    # Resolve session_dir and files
    cur_sess_file: Optional[Path] = None
    sidecar_data: Optional[Dict[str, Any]] = None
    s_dir: Optional[Path] = None

    if session_dir and Path(session_dir).is_dir():
        s_dir = Path(session_dir).resolve()
    elif current_session_file and Path(current_session_file).is_file():
        s_dir = Path(current_session_file).resolve().parent
    elif briefing.get("response_file"):
        rf = Path(briefing["response_file"])
        if rf.parent.name == "responses":
            s_dir = rf.parent.parent

    if s_dir and s_dir.is_dir():
        cand = s_dir / "current-session.json"
        if cand.is_file():
            cur_sess_file = cand
        sc_cand = s_dir / SIDECAR_FILENAME
        if sc_cand.is_file():
            try:
                sidecar_data = json.loads(sc_cand.read_text(encoding="utf-8"))
            except Exception:
                sidecar_data = None

    if not cur_sess_file or not cur_sess_file.is_file():
        briefing["diagnostic_only"] = True
        briefing["diagnostic_warning"] = "No current-session.json found to verify state; cannot re-evaluate realtime budget."
        briefing["budget"] = {
            "budget_evaluation_utc": datetime.now(timezone.utc).isoformat(),
            "budget_evaluated_mode": "unknown_unconfirmed_budget",
            "is_snapshot": False,
            "current_remaining_operational_s": None,
            "can_click": False,
            "can_observe": False,
            "can_finish": False,
            "budget_status": "unknown",
            "explanation": "No current-session.json found to verify state.",
            "permitted_actions": []
        }
        briefing["permitted_actions"] = []
        briefing["recommended_action"] = None
        return briefing

    try:
        cur_sess_data = json.loads(cur_sess_file.read_text(encoding="utf-8"))
    except Exception:
        cur_sess_data = None

    # Check state alignment with briefing
    committed_hash = cur_sess_data.get("last_response_sha256") if cur_sess_data else None
    briefing_hash = briefing.get("response_sha256")
    cur_phase = cur_sess_data.get("phase") if cur_sess_data else None

    if cur_phase in ("stopping", "stopped", "failed"):
        return inspect_turnaround(session_dir=s_dir, now_monotonic=now_monotonic, now_epoch=now_epoch)

    if committed_hash and briefing_hash and committed_hash != briefing_hash:
        return {
            "ok": False,
            "status": "pending",
            "reason": "session_state_advanced_beyond_cached_briefing",
            "committed_sha256": committed_hash,
            "briefing_sha256": briefing_hash,
            "diagnostic_only": True,
            "permitted_actions": []
        }

    # Re-evaluate budget and actions
    screen_type = briefing.get("screen_type", "unknown")
    candidates = briefing.get("candidates", [])
    ui_in_progress = briefing.get("progress", {}).get("ui_refresh_in_progress", False)
    is_failed_step = briefing.get("is_failed_step", False)

    budget_eval = evaluate_budget_and_actions(
        cur_sess_data, sidecar_data, screen_type, candidates, ui_in_progress,
        now_monotonic=now_monotonic,
        now_epoch=now_epoch,
        is_failed_step=is_failed_step,
        is_diagnostic_only=briefing.get("diagnostic_only", False)
    )

    briefing["budget"] = budget_eval
    briefing["permitted_actions"] = budget_eval["permitted_actions"]
    briefing["recommended_action"] = budget_eval["recommended_action"]
    briefing["recommended_target_node_id"] = budget_eval["recommended_target_node_id"]
    briefing["recommended_reason"] = budget_eval["recommended_reason"]
    return briefing


def inspect_turnaround(session_dir: Optional[Path] = None,
                       response_file: Optional[Path] = None,
                       current_session_file: Optional[Path] = None,
                       now_monotonic: Optional[float] = None,
                       now_epoch: Optional[float] = None,
                       cached_briefing: Optional[Union[Dict[str, Any], Path, str]] = None,
                       max_commit_retries: int = 3,
                       commit_retry_interval_s: float = 0.05,
                       **kwargs) -> Dict[str, Any]:
    """Perform authoritative single-turn compact inspection.

    Enforces:
    - Real worker current-session.json commit boundary (phase, next_seq, last_response_sha256).
    - Detects state race condition if current-session.json changes during inspection.
    - Loads unified turnaround context sidecar (turnaround-context.json) for refresh phase and realtime deadline.
    - Missing or unconfirmed context strictly disables action suggestions.
    - Explicitly halts on business failure or terminal state, never fishing up old trees.
    """
    if is_permitted_fn is None:
        return _dependency_unavailable_briefing()

    if now_monotonic is None:
        now_monotonic = time.monotonic()
    if now_epoch is None:
        now_epoch = time.time()

    if cached_briefing is not None:
        return recalculate_cached_briefing(
            cached_briefing=cached_briefing,
            session_dir=session_dir,
            current_session_file=current_session_file,
            now_monotonic=now_monotonic,
            now_epoch=now_epoch
        )

    res_file: Optional[Path] = None
    cur_sess_file: Optional[Path] = None
    cur_sess_data: Optional[Dict[str, Any]] = None
    sidecar_data: Optional[Dict[str, Any]] = None
    is_diagnostic_only = False
    diagnostic_warning = None

    # 1. Resolve session directory, current-session.json, and sidecar
    if session_dir and Path(session_dir).is_dir():
        s_dir = Path(session_dir).resolve()
        cand = s_dir / "current-session.json"
        if cand.is_file():
            cur_sess_file = cand
        sc_cand = s_dir / SIDECAR_FILENAME
        if sc_cand.is_file():
            try:
                sidecar_data = json.loads(sc_cand.read_text(encoding="utf-8"))
            except Exception:
                sidecar_data = None

    if current_session_file and Path(current_session_file).is_file():
        cur_sess_file = Path(current_session_file).resolve()
        sc_cand = cur_sess_file.parent / SIDECAR_FILENAME
        if sc_cand.is_file() and not sidecar_data:
            try:
                sidecar_data = json.loads(sc_cand.read_text(encoding="utf-8"))
            except Exception:
                sidecar_data = None

    # Load session state if available
    if cur_sess_file and cur_sess_file.is_file():
        try:
            cur_sess_data = json.loads(cur_sess_file.read_text(encoding="utf-8"))
        except Exception:
            cur_sess_data = None

    # Check session phase
    cur_phase = cur_sess_data.get("phase") if cur_sess_data else None

    # If worker is still booting, report pending
    if cur_phase == "booting":
        return {
            "ok": False,
            "status": "pending",
            "reason": "worker_booting",
            "phase": "booting",
            "diagnostic_only": True,
            "permitted_actions": []
        }

    # If worker is terminal (stopping/stopped/failed/completed)
    if cur_phase in ("stopping", "stopped", "failed", "completed"):
        is_terminal = True
        term_status = f"session_{cur_phase}"
    else:
        is_terminal = False
        term_status = None

    # 2. Check pre-launch initial ready state (next_seq=1 and no committed response yet)
    if cur_phase == "ready" and cur_sess_data and cur_sess_data.get("next_seq") == 1 and not cur_sess_data.get("last_response_sha256"):
        is_diag = (sidecar_data is None)
        diag_warn = "turnaround-context.json not found in session directory; pre-launch budget unconfirmed, launch suggestion disabled." if is_diag else None
        budget_eval = evaluate_budget_and_actions(
            cur_sess_data, sidecar_data, "initial_ready", [], False,
            now_monotonic=now_monotonic,
            now_epoch=now_epoch,
            is_diagnostic_only=is_diag
        )
        return {
            "ok": not is_diag,
            "status": "initial_ready" if not is_diag else "diagnostic_only",
            "briefing_version": "3.0-task97",
            "phase": "ready",
            "next_seq": 1,
            "screen_type": "initial_ready",
            "response_file": None,
            "response_sha256": None,
            "observation_id": None,
            "candidates": [],
            "budget": budget_eval,
            "permitted_actions": ["launch"] if (not is_diag and budget_eval.get("can_finish")) else [],
            "recommended_action": "launch" if not is_diag else None,
            "recommended_reason": "Worker is ready; dispatch pre-staged initial launch decision." if not is_diag else diag_warn,
            "diagnostic_only": is_diag,
            "diagnostic_warning": diag_warn,
            "disclaimer": "Pre-launch ready state verified from current-session.json."
        }

    # 3. Resolve authoritative response file and verify commit boundary
    committed_hash = cur_sess_data.get("last_response_sha256") if cur_sess_data else None
    next_seq = cur_sess_data.get("next_seq") if cur_sess_data else None
    expected_seq = (next_seq - 1) if (next_seq and next_seq > 1) else None

    # Resolve candidate response file
    if response_file and Path(response_file).is_file():
        res_file = Path(response_file).resolve()
        if committed_hash:
            actual_bytes = res_file.read_bytes()
            actual_hash = hashlib.sha256(actual_bytes).hexdigest()
            if actual_hash != committed_hash:
                is_diagnostic_only = True
                diagnostic_warning = f"Explicit response file ({res_file.name}) does not match committed last_response_sha256; diagnostic_only mode enabled, action suggestions disabled."
        else:
            is_diagnostic_only = True
            diagnostic_warning = "No current-session.json provided to verify commit boundary; diagnostic_only mode enabled, action suggestions disabled."
    elif session_dir and Path(session_dir).is_dir():
        s_dir = Path(session_dir).resolve()
        resp_dir = s_dir / "responses"

        if expected_seq is not None:
            expected_p = resp_dir / f"{expected_seq:04d}.json"
            newer_cand = resp_dir / f"{expected_seq + 1:04d}.json"
            if newer_cand.is_file():
                initial_next_seq = next_seq
                for _ in range(max_commit_retries):
                    time.sleep(commit_retry_interval_s)
                    try:
                        reloaded = json.loads(cur_sess_file.read_text(encoding="utf-8")) if cur_sess_file else None
                        if reloaded and reloaded.get("next_seq", 0) > initial_next_seq:
                            cur_sess_data = reloaded
                            next_seq = cur_sess_data.get("next_seq")
                            expected_seq = next_seq - 1
                            committed_hash = cur_sess_data.get("last_response_sha256")
                            expected_p = resp_dir / f"{expected_seq:04d}.json"
                            break
                    except Exception:
                        pass

                if newer_cand.is_file() and (not next_seq or next_seq <= initial_next_seq):
                    return {
                        "ok": False,
                        "status": "pending",
                        "reason": "new_response_uncommitted_to_session_state",
                        "uncommitted_response_file": str(newer_cand),
                        "diagnostic_only": True,
                        "permitted_actions": []
                    }

            if expected_p.is_file():
                res_file = expected_p
            else:
                return {
                    "ok": False,
                    "status": "state_error",
                    "error": f"committed_response_file_missing: {expected_p}",
                    "diagnostic_only": True,
                    "permitted_actions": []
                }
        else:
            if resp_dir.is_dir():
                resp_files = sorted(resp_dir.glob("*.json"))
                if resp_files:
                    res_file = resp_files[-1]
                    is_diagnostic_only = True
                    diagnostic_warning = "Directory inspected without session commit seq; diagnostic_only mode enabled, action suggestions disabled."

    if not res_file or not res_file.is_file():
        return {
            "ok": False,
            "status": "no_response_file_found",
            "error": "no_response_file_found",
            "session_dir": str(session_dir) if session_dir else None,
            "current_session": cur_sess_data,
            "diagnostic_only": True,
            "permitted_actions": []
        }

    # 4. Read response bytes and verify SHA-256 against committed state
    resp_raw_bytes = res_file.read_bytes()
    resp_sha256 = hashlib.sha256(resp_raw_bytes).hexdigest()

    if committed_hash and not is_diagnostic_only:
        if resp_sha256 != committed_hash:
            return {
                "ok": False,
                "status": "tampered_or_hash_mismatch",
                "error": f"response_sha256_mismatch: current-session.json expects {committed_hash}, but file has {resp_sha256}",
                "expected_sha256": committed_hash,
                "actual_sha256": resp_sha256,
                "response_file": str(res_file),
                "diagnostic_only": True,
                "permitted_actions": []
            }

    # Verify sidecar alignment with committed response and session scope
    if sidecar_data and not is_diagnostic_only:
        sc_hash = sidecar_data.get("response_sha256")
        sc_seq = sidecar_data.get("seq")
        sc_domain = sidecar_data.get("clock_domain")
        sc_sid = sidecar_data.get("session_id")
        cur_sid = cur_sess_data.get("session_id") if cur_sess_data else None

        if sc_domain not in ("windows_monotonic", "monotonic"):
            is_diagnostic_only = True
            diagnostic_warning = f"turnaround-context.json clock_domain '{sc_domain}' is unsupported; action suggestions disabled."
            sidecar_data = None
        elif cur_sid and sc_sid and sc_sid != cur_sid:
            is_diagnostic_only = True
            diagnostic_warning = f"turnaround-context.json session_id mismatch: expected {cur_sid}, got {sc_sid}; action suggestions disabled."
            sidecar_data = None
        elif sc_hash != resp_sha256 or (expected_seq is not None and sc_seq != expected_seq):
            is_diagnostic_only = True
            diagnostic_warning = f"turnaround-context.json ({sc_hash[:12] if sc_hash else 'None'}.., seq={sc_seq}) is desynchronized with committed response ({resp_sha256[:12]}.., seq={expected_seq}); action suggestions disabled."
            sidecar_data = None
    elif not sidecar_data and not is_diagnostic_only:
        is_diagnostic_only = True
        diagnostic_warning = "turnaround-context.json not found in session directory; cannot verify refresh phase or realtime deadline, action suggestions disabled."

    session_mode = (cur_sess_data.get("mode") if cur_sess_data else None) or (sidecar_data.get("mode") if sidecar_data else "application_refresh")

    try:
        resp_data = json.loads(resp_raw_bytes.decode("utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "status": "malformed_response_json",
            "error": f"malformed_response_json: {exc}",
            "response_file": str(res_file),
            "response_sha256": resp_sha256,
            "diagnostic_only": True,
            "permitted_actions": []
        }

    # 5. Extract payload and check for step failure or finish
    payload = resp_data.get("payload") if (isinstance(resp_data, dict) and "payload" in resp_data) else resp_data
    from stepwise_session import is_navigation_recovery_response
    if (not is_terminal and not is_diagnostic_only and
            is_navigation_recovery_response(resp_data)):
        budget_eval = evaluate_budget_and_actions(
            cur_sess_data, sidecar_data, 'navigation_observation_required', [], False,
            now_monotonic=now_monotonic, now_epoch=now_epoch)
        return {
            'ok': False, 'status': 'navigation_observation_required',
            'screen_type': 'navigation_observation_required', 'is_failed_step': False,
            'is_terminal': False, 'diagnostic_only': False,
            'response_file': str(res_file), 'response_sha256': resp_sha256,
            'observation_id': None, 'candidates': [], 'budget': budget_eval,
            'permitted_actions': budget_eval['permitted_actions'],
            'recommended_action': budget_eval['recommended_action'],
            'recommended_target_node_id': None,
            'recommended_reason': budget_eval['recommended_reason'],
        }
    is_failed_step = False
    is_finished_step = False
    step_error = None
    step_error_code = None

    if isinstance(payload, dict):
        if payload.get("failed") is True or payload.get("error") or payload.get("error_code") or payload.get("status") in ("failed", "error"):
            is_failed_step = True
            step_error = str(payload.get("error") or payload.get("status") or "worker_reported_failure")
            step_error_code = str(payload.get("error_code") or "")
        elif payload.get("finished") is True:
            is_finished_step = True
        elif "session_ended" in payload:
            se = payload["session_ended"]
            if isinstance(se, dict) and (se.get("status") in ("failed", "error") or se.get("error")):
                is_failed_step = True
                step_error = f"session_ended_failure: {se.get('error') or se.get('status')}"
            elif isinstance(se, dict) and se.get("status") == "completed":
                is_finished_step = True

    # 6. Extract observation from latest response
    obs = extract_observation_from_payload(payload)

    # CRITICAL: IF LATEST STEP FAILED OR IS TERMINAL, NEVER FISH UP OLD TREES AS ACTIONABLE!
    if is_failed_step or is_terminal or is_finished_step:
        budget_eval = evaluate_budget_and_actions(
            cur_sess_data, sidecar_data,
            "worker_business_failure" if is_failed_step else (term_status or "finished"),
            [], False,
            now_monotonic=now_monotonic,
            now_epoch=now_epoch,
            is_failed_step=True,
            is_diagnostic_only=is_diagnostic_only
        )
        return {
            "ok": False if is_failed_step else True,
            "status": "worker_business_failure" if is_failed_step else (term_status or "finished"),
            "briefing_version": "3.0-task97",
            "is_failed_step": is_failed_step,
            "is_terminal": is_terminal or is_failed_step or is_finished_step,
            "step_error": step_error,
            "step_error_code": step_error_code,
            "response_file": str(res_file),
            "response_sha256": resp_sha256,
            "observation_id": None,
            "screen_type": "worker_business_failure" if is_failed_step else (term_status or "finished"),
            "repo_info": {"repo_title": None, "repo_address": None, "is_official_fdroid": False, "last_update_status": None},
            "check_for_updates_button": {"present": False, "state": "not_on_screen"},
            "progress": {"progress_bars_detected": False, "progress_count": 0, "progress_nodes": [], "ui_refresh_in_progress": False},
            "candidates": [],
            "disabled_refresh_controls": [],
            "historical_diagnostic_nodes": [],
            "budget": budget_eval,
            "permitted_actions": [],
            "recommended_action": None,
            "recommended_target_node_id": None,
            "recommended_reason": f"Execution halted: {step_error or term_status or 'finished'}",
            "diagnostic_only": is_diagnostic_only,
            "diagnostic_warning": diagnostic_warning,
            "disclaimer": "Halted on business failure or terminal phase. Historical node reuse is strictly forbidden."
        }

    # If response is not failed, but contains no observation tree
    if not obs or not isinstance(obs, dict):
        budget_eval = evaluate_budget_and_actions(
            cur_sess_data, sidecar_data, "unknown_no_tree", [], False,
            now_monotonic=now_monotonic,
            now_epoch=now_epoch,
            is_diagnostic_only=is_diagnostic_only
        )
        return {
            "ok": False,
            "status": "no_observation_tree",
            "briefing_version": "3.0-task97",
            "is_failed_step": False,
            "response_file": str(res_file),
            "response_sha256": resp_sha256,
            "observation_id": None,
            "screen_type": "unknown_no_tree",
            "candidates": [],
            "budget": budget_eval,
            "permitted_actions": [],
            "recommended_action": None,
            "diagnostic_only": is_diagnostic_only,
            "diagnostic_warning": diagnostic_warning,
            "disclaimer": "Latest response contains no observation tree. Cannot generate actionable candidates."
        }

    obs_id = obs.get("observation_id")
    nodes = obs.get("nodes", [])

    # 7. Extract structural data from authoritative tree
    repo_info = extract_repo_info(nodes)
    progress_info = detect_progress_and_refresh_state(nodes)
    candidates, disabled_controls = extract_navigation_candidates(nodes, obs)

    ref_dispatched = bool(sidecar_data and sidecar_data.get("refresh_dispatched"))
    seen_in_prog = bool(sidecar_data and sidecar_data.get("seen_in_progress"))
    ui_ver_comp = bool(sidecar_data and sidecar_data.get("ui_verified_completed"))

    screen_type = infer_screen_type(
        nodes, candidates, repo_info,
        refresh_dispatched=ref_dispatched,
        seen_in_progress=seen_in_prog,
        ui_verified_completed=ui_ver_comp,
        progress_info=progress_info,
        disabled_controls=disabled_controls
    )

    budget_eval = evaluate_budget_and_actions(
        cur_sess_data, sidecar_data, screen_type, candidates, progress_info["ui_refresh_in_progress"],
        now_monotonic=now_monotonic,
        now_epoch=now_epoch,
        is_diagnostic_only=is_diagnostic_only
    )

    # 8. Check for Check for updates specific button
    cfu_cand = next((c for c in candidates if c["candidate_type"] == "check_for_updates"), None)
    cfu_disabled = next((d for d in disabled_controls if "check for updates" in d.get("content_desc", "").lower()), None)

    if cfu_cand:
        check_for_updates_button = {
            "present": True,
            "enabled": True,
            "clickable": cfu_cand.get("target_clickable", True),
            "target_node_id": cfu_cand["target_node_id"],
            "semantic_node_id": cfu_cand["semantic_node_id"],
            "bounds": cfu_cand["target_bounds"],
            "state": "ready_to_click" if not ref_dispatched else "refresh_already_dispatched"
        }
    elif cfu_disabled:
        check_for_updates_button = {
            "present": True,
            "enabled": False,
            "clickable": cfu_disabled.get("clickable", False),
            "node_id": cfu_disabled["node_id"],
            "bounds": cfu_disabled.get("bounds"),
            "state": "disabled_refresh_in_progress"
        }
    else:
        check_for_updates_button = {
            "present": False,
            "state": "not_on_screen"
        }

    # 9. Race Condition Detection: Re-read state to ensure state did not advance during inspection
    if cur_sess_file and cur_sess_file.is_file() and not is_diagnostic_only:
        try:
            recheck_state = json.loads(cur_sess_file.read_text(encoding="utf-8"))
            if (recheck_state.get("next_seq") != cur_sess_data.get("next_seq") or
                recheck_state.get("last_response_sha256") != cur_sess_data.get("last_response_sha256") or
                recheck_state.get("phase") != cur_sess_data.get("phase")):
                return {
                    "ok": False,
                    "status": "pending",
                    "reason": "session_state_advanced_during_inspection",
                    "diagnostic_only": True,
                    "permitted_actions": []
                }
        except Exception:
            pass

    # If diagnostic only or in stepwise_input mode, strictly clear actionable refresh candidates from output
    if is_diagnostic_only or session_mode == "stepwise_input":
        actionable_candidates = []
        historical_diag = candidates
    else:
        actionable_candidates = candidates
        historical_diag = []

    return {
        "ok": True,
        "status": "ready" if not is_diagnostic_only else "diagnostic_only",
        "briefing_version": "3.0-task97",
        "response_file": str(res_file),
        "response_sha256": resp_sha256,
        "observation_id": obs_id,
        "screen_type": screen_type,
        "repo_info": repo_info,
        "check_for_updates_button": check_for_updates_button,
        "progress": progress_info,
        "candidates": actionable_candidates,
        "disabled_refresh_controls": disabled_controls,
        "historical_diagnostic_nodes": historical_diag,
        "budget": budget_eval,
        "permitted_actions": budget_eval["permitted_actions"],
        "recommended_action": budget_eval["recommended_action"],
        "recommended_target_node_id": budget_eval["recommended_target_node_id"],
        "recommended_reason": budget_eval["recommended_reason"],
        "diagnostic_only": is_diagnostic_only,
        "diagnostic_warning": diagnostic_warning,
        "disclaimer": "Candidates are navigational suggestions extracted from the observation tree; production Policy admission and stable direct UI read with full XML equality verification remain the authoritative gatekeepers."
    }


def format_human_summary(briefing: Dict[str, Any]) -> str:
    """Format a clean, concise 8-12 line human-readable executive briefing."""
    if not briefing.get("ok"):
        return (
            f"[TURNAROUND BRIEFING WARNING] status={briefing.get('status')} | "
            f"error={briefing.get('error') or briefing.get('reason')} | "
            f"diagnostic_only={briefing.get('diagnostic_only')}"
        )

    lines = []
    lines.append("=== Task 97 Turnaround Compact Executive Briefing ===")
    res_name = Path(briefing['response_file']).name if briefing.get('response_file') else "None"
    resp_hash = (briefing.get('response_sha256') or '')[:16]
    lines.append(f"Response: {res_name} (SHA256: {resp_hash}...) | Commit Status: {briefing.get('status')}")
    lines.append(f"Observation ID: {briefing.get('observation_id')}")

    if briefing.get("diagnostic_only"):
        lines.append(f"DIAGNOSTIC ONLY: {briefing.get('diagnostic_warning')}")

    if briefing.get("is_failed_step"):
        lines.append(f"WARNING: Latest response failed: {briefing.get('step_error')} (code={briefing.get('step_error_code')})")

    p_in_prog = briefing.get("progress", {}).get("ui_refresh_in_progress", False)
    lines.append(f"Screen Type: {briefing['screen_type']} | UI In-Progress: {p_in_prog}")

    repo = briefing.get("repo_info", {})
    if repo.get("repo_title") or repo.get("repo_address"):
        lines.append(f"Repo Info: Title='{repo.get('repo_title')}', Addr='{repo.get('repo_address')}', Official={repo.get('is_official_fdroid')}")

    cfu = briefing.get("check_for_updates_button", {})
    if cfu.get("present"):
        lines.append(f"Check for updates button: state={cfu.get('state')}, enabled={cfu.get('enabled')}, target={cfu.get('target_node_id') or cfu.get('node_id')}")

    cands = briefing.get("candidates", [])
    lines.append(f"Actionable Candidates ({len(cands)}):")
    for c in cands:
        rel = f"[{c.get('wrapper_relationship')}]"
        lines.append(f"  * {c.get('candidate_type')}: target={c.get('target_node_id')} {rel} sem={c.get('semantic_node_id')} label='{c.get('semantic_label')}'")

    b = briefing.get("budget", {})
    lines.append(f"Budget: remaining_op={b.get('current_remaining_operational_s')}s | status={b.get('budget_status')} | can_click={b.get('can_click')}")
    lines.append(f"Permitted Actions: {briefing.get('permitted_actions')} | Recommended: action='{briefing.get('recommended_action')}' target='{briefing.get('recommended_target_node_id')}'")
    lines.append(f"Reason: {briefing.get('recommended_reason')}")
    lines.append("====================================================")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Task 97 Compact Turnaround Inspector")
    parser.add_argument("--session-dir", type=Path, default=None, help="Path to session output directory")
    parser.add_argument("--response-file", type=Path, default=None, help="Path to specific response JSON file")
    parser.add_argument("--current-session-file", type=Path, default=None, help="Path to current-session.json")
    parser.add_argument("--cached-briefing", type=Path, default=None, help="Path to cached step_briefing.json to re-evaluate budget without re-parsing XML/trees")
    parser.add_argument("--now-monotonic", type=float, default=None, help="Explicit monotonic timestamp for testing")
    parser.add_argument("--now-epoch", type=float, default=None, help="Explicit epoch timestamp for testing")
    parser.add_argument("--json", action="store_true", help="Print raw JSON only")
    parser.add_argument("--quiet", action="store_true", help="Suppress human summary, print only JSON")

    args = parser.parse_args()

    sess_dir = args.session_dir
    if not sess_dir and not args.response_file and not args.cached_briefing:
        local_cand = Path(__file__).resolve().parent / "session"
        if local_cand.is_dir():
            sess_dir = local_cand

    briefing = inspect_turnaround(
        session_dir=sess_dir,
        response_file=args.response_file,
        current_session_file=args.current_session_file,
        cached_briefing=args.cached_briefing,
        now_monotonic=args.now_monotonic,
        now_epoch=args.now_epoch
    )

    if args.json or args.quiet:
        print(json.dumps(briefing, indent=2, ensure_ascii=False))
    else:
        print(format_human_summary(briefing))
        print("\nFull JSON:")
        print(json.dumps(briefing, indent=2, ensure_ascii=False))

    return 0 if briefing.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
