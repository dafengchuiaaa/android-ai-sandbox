# !/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Offline XML hierarchy diff & target-chain forensics tool (standard library only).

Diagnostic tool ONLY:
- Compares an expected observation XML and an actual pre-input current XML.
- Strictly requires and verifies both expected and current 64-char lowercase hex
  SHA-256 hashes before claiming hash_verified=True.
- Inspects XML element tags, attributes, and non-whitespace element.text / element.tail.
- Distinguishes:
  1) raw_byte_only_diff (bytes differ due to formatting/whitespace/declaration only,
     while parsed tags, attributes, and semantic element text/tail are identical)
  2) attribute_or_text_diffs (nodes whose attributes or element text/tail changed)
  3) added_nodes / removed_nodes / unconfirmed_replacements (structural additions,
     removals, or unconfirmed replacements at the same path)
  4) order_or_index_changes (uniquely anchored nodes whose tree path shifted)
- Flags ambiguous/non-unique anchors and missing target anchors as unconfirmed
  instead of silently aligning by tree path/class and claiming unique identity.
- Evaluates the requested node_id and its resolved clickable ancestor chain.
- NEVER used to accept stale observations, ignore dynamic regions, or auto-retry clicks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEEPSEEK_DIR = PROJECT_ROOT / "departments" / "deepseek"
if str(DEEPSEEK_DIR) not in sys.path:
    sys.path.insert(0, str(DEEPSEEK_DIR))

import content_adapter as ca  # noqa: E402

TOOL_VERSION = "1.1.0"
SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")

COMPARE_ATTRS = (
    "package",
    "class",
    "resource-id",
    "text",
    "content-desc",
    "bounds",
    "enabled",
    "clickable",
    "focusable",
    "focused",
    "selected",
    "checked",
    "checkable",
    "scrollable",
    "long-clickable",
    "password",
    "NAF",
    "index",
)


def _norm_elem_text(raw: Optional[str]) -> str:
    """Normalize XML element character data (element.text or element.tail).

     Pure indentation/formatting whitespace normalizes to empty string '',
    while any non-whitespace character data is preserved verbatim so semantic
    XML element text changes are never mistaken for raw_byte_only_diff.
    Note: Android UI text is stored in the 'text' XML attribute, whereas
    element.text / element.tail are XML character data outside attributes.
    """
    if not raw or not raw.strip():
        return ""
    return raw.strip()


def _node_attrs(el: ET.Element) -> Dict[str, Optional[str]]:
    out: Dict[str, Optional[str]] = {
        "__tag__": el.tag,
        "__element_text__": _norm_elem_text(el.text),
        "__element_tail__": _norm_elem_text(el.tail),
    }
    for k in COMPARE_ATTRS:
        out[k] = el.get(k)
    for k, v in sorted(el.attrib.items()):
        if k not in out:
            out[k] = v
    return out


def _anchor_key(it: ca.Item) -> Optional[Tuple[str, ...]]:
    """Build a semantic anchor key if the node is a standard <node> carrying identifying attributes.

    Nodes that are purely anonymous layout containers (no resource-id, no text,
    no content-desc) or non-standard XML tags return None so we never pretend
    they have a path-independent unique semantic identity.
    """
    el = it.el
    if el.tag != "node":
        return None
    pkg = (el.get("package") or "").strip()
    cls = (el.get("class") or "").strip()
    res_id = (el.get("resource-id") or "").strip()
    text = (el.get("text") or "").strip()
    desc = (el.get("content-desc") or "").strip()
    if not (res_id or text or desc):
        return None
    return (el.tag, pkg, cls, res_id, text, desc)


def _node_summary(obs: ca.Observation, it: ca.Item) -> Dict[str, Any]:
    d = obs.node_dict(it)
    return {
        "node_id": d["node_id"],
        "path": d["path"],
        "tag": it.el.tag,
        "package": d["package"],
        "class": d["class"],
        "resource_id": d["resource_id"],
        "text": d["text"],
        "content_desc": d["content_desc"],
        "element_text": _norm_elem_text(it.el.text) or None,
        "element_tail": _norm_elem_text(it.el.tail) or None,
        "bounds": d["bounds"],
        "bounds_raw": d["bounds_raw"],
        "enabled": d["enabled"],
        "clickable": d["clickable"],
        "focused": d["focused"],
        "selected": d["selected"],
        "scrollable": d["scrollable"],
        "subtree_texts": obs.subtree_texts(it)[:6],
    }


def _diff_node_attributes(exp_it: ca.Item, cur_it: ca.Item) -> Dict[str, Dict[str, Optional[str]]]:
    exp_a = _node_attrs(exp_it.el)
    cur_a = _node_attrs(cur_it.el)
    all_keys = sorted(set(exp_a.keys()) | set(cur_a.keys()))
    changes: Dict[str, Dict[str, Optional[str]]] = {}
    for k in all_keys:
        if exp_a.get(k) != cur_a.get(k):
            changes[k] = {"expected": exp_a.get(k), "current": cur_a.get(k)}
    return changes


def _collect_unsupported_xml_elements(obs: ca.Observation, side: str) -> List[Dict[str, Any]]:
    """Detect non-standard XML tags or non-whitespace XML element character data."""
    anomalies: List[Dict[str, Any]] = []
    for it in obs.items:
        el = it.el
        expected_tag = "hierarchy" if it.path == "R" else "node"
        if el.tag != expected_tag:
            anomalies.append({
                "side": side,
                "path": it.path,
                "kind": "unexpected_element_tag",
                "tag": el.tag,
                "expected_tag": expected_tag,
            })
        elem_text = _norm_elem_text(el.text)
        if elem_text:
            anomalies.append({
                "side": side,
                "path": it.path,
                "kind": "non_empty_element_text",
                "tag": el.tag,
                "element_text": elem_text,
            })
        elem_tail = _norm_elem_text(el.tail)
        if elem_tail:
            anomalies.append({
                "side": side,
                "path": it.path,
                "kind": "non_empty_element_tail",
                "tag": el.tag,
                "element_tail": elem_tail,
            })
    return anomalies


def _resolve_expected_target_chain(
    exp_obs: ca.Observation,
    requested_node_id: Optional[str],
) -> Dict[str, Any]:
    if not requested_node_id:
        return {"requested_node_id": None, "status": "not_provided", "chain": []}
    try:
        req_item = exp_obs.item_by_node_id(requested_node_id)
    except ca.AdapterError as exc:
        return {
            "requested_node_id": requested_node_id,
            "status": "lookup_failed",
            "error_code": exc.code,
            "error_message": exc.message,
            "chain": [],
        }

    anc_chain = exp_obs.ancestor_chain(req_item)
    clickable_target: Optional[ca.Item] = None
    blocked_node: Optional[ca.Item] = None

    if ca.norm_bool(req_item.attr("clickable")) is True and ca.norm_bool(req_item.attr("enabled")) is True:
        clickable_target = req_item
    else:
        for anc in anc_chain[1:]:
            if ca._is_dump_envelope(anc):
                continue
            if ca.norm_bool(anc.attr("clickable")) is True:
                clickable_target = anc
                break
            if ca.norm_bool(anc.attr("enabled")) is not True:
                blocked_node = anc
                break

    if clickable_target is not None:
        relevant = anc_chain[: anc_chain.index(clickable_target) + 1]
    else:
        relevant = anc_chain

    return {
        "requested_node_id": requested_node_id,
        "status": "resolved" if clickable_target is not None else "unresolved",
        "requested_path": req_item.path,
        "resolved_clickable_target_node_id": clickable_target.node_id if clickable_target else None,
        "resolved_clickable_target_path": clickable_target.path if clickable_target else None,
        "blocked_node_id": blocked_node.node_id if blocked_node else None,
        "chain_items": relevant,
    }


def _subtree_has_unique_preserved_anchor(
    exp_obs: ca.Observation,
    cur_obs: ca.Observation,
    exp_it: ca.Item,
    cur_it: ca.Item,
    exp_anchor_counts: Dict[Tuple[str, ...], int],
    cur_anchor_counts: Dict[Tuple[str, ...], int],
) -> bool:
    """Check whether an anonymous container's subtree carries at least one unique
    semantic anchor that is identically preserved in cur_it's subtree.
    """
    exp_sub_texts = exp_obs.subtree_texts(exp_it)
    cur_sub_texts = cur_obs.subtree_texts(cur_it)
    if not exp_sub_texts or exp_sub_texts != cur_sub_texts:
        return False

    exp_sub_anchors = [
        _anchor_key(sub) for sub in exp_obs.subtree(exp_it)
        if _anchor_key(sub) is not None
    ]
    cur_sub_anchors = [
        _anchor_key(sub) for sub in cur_obs.subtree(cur_it)
        if _anchor_key(sub) is not None
    ]
    if not exp_sub_anchors or exp_sub_anchors != cur_sub_anchors:
        return False

    return all(
        exp_anchor_counts.get(k, 0) == 1 and cur_anchor_counts.get(k, 0) == 1
        for k in exp_sub_anchors
        if k is not None
    )


def _evaluate_chain_in_current(
    exp_obs: ca.Observation,
    cur_obs: ca.Observation,
    chain_info: Dict[str, Any],
    exp_anchor_counts: Dict[Tuple[str, ...], int],
    cur_anchor_counts: Dict[Tuple[str, ...], int],
    cur_anchor_map: Dict[Tuple[str, ...], List[ca.Item]],
    cur_by_path: Dict[str, ca.Item],
) -> Dict[str, Any]:
    if chain_info.get("status") in ("not_provided", "lookup_failed"):
        return {
            "requested_node_id": chain_info.get("requested_node_id"),
            "status": chain_info.get("status"),
            "error_code": chain_info.get("error_code"),
            "error_message": chain_info.get("error_message"),
            "nodes": [],
            "overall_chain_verdict": "unverified",
        }

    chain_items: List[ca.Item] = chain_info.get("chain_items", [])
    node_reports: List[Dict[str, Any]] = []
    any_changed = False
    any_missing_or_ambiguous = False

    for idx, exp_it in enumerate(chain_items):
        role = "requested_node" if idx == 0 else (
            "resolved_clickable_ancestor"
            if exp_it.path == chain_info.get("resolved_clickable_target_path")
            else f"intermediate_ancestor_{idx}"
        )
        akey = _anchor_key(exp_it)
        same_path_cur = cur_by_path.get(exp_it.path)

        match_status = "unconfirmed"
        match_basis = "none"
        matched_cur: Optional[ca.Item] = None
        attr_diffs: Dict[str, Any] = {}
        candidate_evidence: Optional[Dict[str, Any]] = None

        if exp_it.el.tag != "node":
            match_status = "unconfirmed"
            match_basis = f"unsupported_expected_xml_tag({exp_it.el.tag})"
            any_missing_or_ambiguous = True
        elif akey is not None:
            # Node had a semantic anchor (resource-id, text, or content-desc)
            exp_count = exp_anchor_counts.get(akey, 0)
            cur_candidates = cur_anchor_map.get(akey, [])
            if exp_count > 1 or len(cur_candidates) > 1:
                match_status = "ambiguous"
                match_basis = f"duplicate_semantic_anchor(expected={exp_count}, current={len(cur_candidates)})"
                any_missing_or_ambiguous = True
                if cur_candidates:
                    candidate_evidence = {
                        "reason": "multiple_nodes_share_semantic_anchor",
                        "candidate_paths": [c.path for c in cur_candidates],
                    }
            elif len(cur_candidates) == 1:
                matched_cur = cur_candidates[0]
                attr_diffs = _diff_node_attributes(exp_it, matched_cur)
                if matched_cur.path == exp_it.path and not attr_diffs:
                    match_status = "unchanged"
                    match_basis = "unique_semantic_anchor_and_exact_path"
                elif matched_cur.path == exp_it.path:
                    match_status = "changed"
                    match_basis = "unique_semantic_anchor_same_path_attr_changed"
                    any_changed = True
                else:
                    match_status = "changed"
                    match_basis = f"unique_semantic_anchor_path_shifted({exp_it.path}->{matched_cur.path})"
                    any_changed = True
            else:
                # Original semantic anchor disappeared!
                # Check if exp_it had a unique non-empty resource-id that still exists uniquely at the same path/class/package
                exp_res_id = (exp_it.el.get("resource-id") or "").strip()
                same_res_candidates = [
                    it for it in cur_obs.items
                    if it.path != "R"
                    and it.el.tag == "node"
                    and (it.el.get("resource-id") or "").strip() == exp_res_id
                    and (it.el.get("class") or "").strip() == (exp_it.el.get("class") or "").strip()
                    and (it.el.get("package") or "").strip() == (exp_it.el.get("package") or "").strip()
                ] if exp_res_id else []

                if exp_res_id and len(same_res_candidates) == 1 and same_res_candidates[0].path == exp_it.path:
                    # Resource-id is non-empty, unique, and preserved at the exact path, while text/desc changed
                    matched_cur = same_res_candidates[0]
                    attr_diffs = _diff_node_attributes(exp_it, matched_cur)
                    match_status = "changed"
                    match_basis = "unique_resource_id_preserved_same_path_text_changed"
                    any_changed = True
                else:
                    # Semantic anchor disappeared and cannot be confirmed from path/class/package alone!
                    match_status = "unconfirmed"
                    any_missing_or_ambiguous = True
                    if same_path_cur is not None:
                        same_cls_pkg = (
                            same_path_cur.el.tag == "node"
                            and same_path_cur.el.get("class") == exp_it.el.get("class")
                            and same_path_cur.el.get("package") == exp_it.el.get("package")
                        )
                        match_basis = (
                            "semantic_anchor_missing_cannot_confirm_from_path_and_class"
                            if same_cls_pkg
                            else "semantic_anchor_missing_and_path_incompatible"
                        )
                        candidate_evidence = {
                            "candidate_basis": (
                                "same_path_and_class_only_anchor_missing"
                                if same_cls_pkg
                                else "same_path_different_tag_class_or_package"
                            ),
                            "identity_confirmed": False,
                            "same_path_candidate": _node_summary(cur_obs, same_path_cur),
                            "candidate_attribute_diffs": _diff_node_attributes(exp_it, same_path_cur),
                        }
                    else:
                        match_basis = "semantic_anchor_missing_and_path_absent"
        else:
            # Case 2: Anonymous container (no resource-id, text, or content-desc on the container itself)
            if (
                same_path_cur is not None
                and same_path_cur.el.tag == "node"
                and same_path_cur.el.get("class") == exp_it.el.get("class")
                and same_path_cur.el.get("package") == exp_it.el.get("package")
            ):
                if _subtree_has_unique_preserved_anchor(
                    exp_obs, cur_obs, exp_it, same_path_cur, exp_anchor_counts, cur_anchor_counts
                ):
                    matched_cur = same_path_cur
                    attr_diffs = _diff_node_attributes(exp_it, matched_cur)
                    if not attr_diffs:
                        match_status = "unchanged"
                        match_basis = "exact_path_class_bounds_and_preserved_unique_subtree_anchor"
                    else:
                        match_status = "changed"
                        match_basis = "exact_path_and_preserved_unique_subtree_anchor_with_attr_diff"
                        any_changed = True
                else:
                    match_status = "unconfirmed"
                    match_basis = "anonymous_container_subtree_anchor_changed_or_absent"
                    any_missing_or_ambiguous = True
                    candidate_evidence = {
                        "candidate_basis": "same_path_and_class_anonymous_container_without_preserved_subtree_anchor",
                        "identity_confirmed": False,
                        "same_path_candidate": _node_summary(cur_obs, same_path_cur),
                        "candidate_attribute_diffs": _diff_node_attributes(exp_it, same_path_cur),
                    }
            else:
                match_status = "unconfirmed"
                match_basis = "anonymous_container_path_or_class_changed"
                any_missing_or_ambiguous = True
                if same_path_cur is not None:
                    candidate_evidence = {
                        "candidate_basis": "same_path_different_tag_class_or_package",
                        "identity_confirmed": False,
                        "same_path_candidate": _node_summary(cur_obs, same_path_cur),
                    }

        node_reports.append({
            "role": role,
            "expected": _node_summary(exp_obs, exp_it),
            "current": _node_summary(cur_obs, matched_cur) if matched_cur is not None else None,
            "status": match_status,
            "match_basis": match_basis,
            "attribute_diffs": attr_diffs,
            "candidate_evidence": candidate_evidence,
        })

    if any_missing_or_ambiguous:
        overall = "target_or_ancestor_ambiguous_or_missing"
    elif any_changed:
        overall = "target_or_ancestor_changed"
    else:
        overall = "target_and_ancestor_chain_unchanged"

    return {
        "requested_node_id": chain_info.get("requested_node_id"),
        "resolution_status_in_expected": chain_info.get("status"),
        "resolved_clickable_target_node_id": chain_info.get("resolved_clickable_target_node_id"),
        "overall_chain_verdict": overall,
        "nodes": node_reports,
    }


def compare_xml_files(
    expected_xml_path: Path,
    current_xml_path: Path,
    expected_hash: Optional[str] = None,
    current_hash: Optional[str] = None,
    requested_node_id: Optional[str] = None,
    binding_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Compare two raw UI hierarchy XML files and return a structured forensic report.

    Both expected_hash and current_hash must be supplied as valid 64-character
    lowercase hex SHA-256 strings and must match the actual file bytes before
    hash_verified can be True.
    """
    try:
        exp_path = Path(expected_xml_path).resolve()
        cur_path = Path(current_xml_path).resolve()
    except Exception as exc:
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": False,
            "hash_verified": False,
            "error_code": "invalid_xml_path",
            "error_message": str(exc),
        }

    if not exp_path.is_file():
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": False,
            "hash_verified": False,
            "error_code": "expected_xml_missing",
            "error_message": f"Expected XML file not found: {exp_path}",
        }
    if not cur_path.is_file():
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": False,
            "hash_verified": False,
            "error_code": "current_xml_missing",
            "error_message": f"Current XML file not found: {cur_path}",
        }

    exp_bytes = exp_path.read_bytes()
    cur_bytes = cur_path.read_bytes()
    actual_exp_hash = hashlib.sha256(exp_bytes).hexdigest()
    actual_cur_hash = hashlib.sha256(cur_bytes).hexdigest()

    declared_errors: List[str] = []
    if not isinstance(expected_hash, str) or not SHA256_RE.fullmatch(expected_hash):
        declared_errors.append(
            f"expected_hash must be a 64-char lowercase hex SHA-256 (got {expected_hash!r})"
        )
    if not isinstance(current_hash, str) or not SHA256_RE.fullmatch(current_hash):
        declared_errors.append(
            f"current_hash must be a 64-char lowercase hex SHA-256 (got {current_hash!r})"
        )

    if declared_errors:
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": True,
            "hash_verified": False,
            "error_code": "missing_or_invalid_declared_hash",
            "error_message": "; ".join(declared_errors),
            "expected_xml": {
                "path": str(exp_path),
                "declared_sha256": expected_hash,
                "actual_sha256": actual_exp_hash,
                "bytes": len(exp_bytes),
            },
            "current_xml": {
                "path": str(cur_path),
                "declared_sha256": current_hash,
                "actual_sha256": actual_cur_hash,
                "bytes": len(cur_bytes),
            },
        }

    hash_errors: List[str] = []
    if actual_exp_hash != expected_hash:
        hash_errors.append(
            f"expected_xml hash mismatch: declared {expected_hash}, actual file bytes {actual_exp_hash}"
        )
    if actual_cur_hash != current_hash:
        hash_errors.append(
            f"current_xml hash mismatch: declared {current_hash}, actual file bytes {actual_cur_hash}"
        )

    if hash_errors:
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": True,
            "hash_verified": False,
            "error_code": "hash_verification_failed",
            "error_message": "; ".join(hash_errors),
            "expected_xml": {
                "path": str(exp_path),
                "declared_sha256": expected_hash,
                "actual_sha256": actual_exp_hash,
                "bytes": len(exp_bytes),
            },
            "current_xml": {
                "path": str(cur_path),
                "declared_sha256": current_hash,
                "actual_sha256": actual_cur_hash,
                "bytes": len(cur_bytes),
            },
        }

    try:
        exp_obs = ca.Observation(exp_bytes, source_name=exp_path.name)
        cur_obs = ca.Observation(cur_bytes, source_name=cur_path.name)
    except Exception as exc:
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": True,
            "hash_verified": True,
            "error_code": "xml_parse_failed",
            "error_message": str(exc),
        }

    bytes_identical = (exp_bytes == cur_bytes)
    unsupported_xml_elements = (
        _collect_unsupported_xml_elements(exp_obs, "expected")
        + _collect_unsupported_xml_elements(cur_obs, "current")
    )

    exp_by_path = {it.path: it for it in exp_obs.items if it.path != "R"}
    cur_by_path = {it.path: it for it in cur_obs.items if it.path != "R"}

    exp_anchor_map: Dict[Tuple[str, ...], List[ca.Item]] = {}
    cur_anchor_map: Dict[Tuple[str, ...], List[ca.Item]] = {}
    for it in exp_obs.items:
        if it.path == "R":
            continue
        k = _anchor_key(it)
        if k is not None:
            exp_anchor_map.setdefault(k, []).append(it)
    for it in cur_obs.items:
        if it.path == "R":
            continue
        k = _anchor_key(it)
        if k is not None:
            cur_anchor_map.setdefault(k, []).append(it)

    exp_anchor_counts = {k: len(v) for k, v in exp_anchor_map.items()}
    cur_anchor_counts = {k: len(v) for k, v in cur_anchor_map.items()}

    matched_exp_paths = set()
    matched_cur_paths = set()
    attribute_or_text_diffs: List[Dict[str, Any]] = []
    order_or_index_changes: List[Dict[str, Any]] = []
    ambiguous_anchors: List[Dict[str, Any]] = []
    unconfirmed_replacements: List[Dict[str, Any]] = []
    added_nodes: List[Dict[str, Any]] = []
    removed_nodes: List[Dict[str, Any]] = []

    # 1. Detect duplicate / ambiguous semantic anchors across either tree
    all_anchor_keys = sorted(set(exp_anchor_map.keys()) | set(cur_anchor_map.keys()))
    for akey in all_anchor_keys:
        e_list = exp_anchor_map.get(akey, [])
        c_list = cur_anchor_map.get(akey, [])
        if len(e_list) > 1 or len(c_list) > 1:
            e_paths = {x.path: x for x in e_list}
            c_paths = {x.path: x for x in c_list}
            if set(e_paths.keys()) == set(c_paths.keys()) and all(
                not _diff_node_attributes(e_paths[p], c_paths[p]) for p in e_paths
            ):
                for p in e_paths:
                    matched_exp_paths.add(p)
                    matched_cur_paths.add(p)
            else:
                ambiguous_anchors.append({
                    "anchor": {
                        "tag": akey[0],
                        "package": akey[1],
                        "class": akey[2],
                        "resource_id": akey[3],
                        "text": akey[4],
                        "content_desc": akey[5],
                    },
                    "expected_count": len(e_list),
                    "current_count": len(c_list),
                    "expected_paths": [x.path for x in e_list],
                    "current_paths": [x.path for x in c_list],
                    "reason": "duplicate_semantic_anchor_with_structural_or_attribute_shift",
                })
                for x in e_list:
                    matched_exp_paths.add(x.path)
                for x in c_list:
                    matched_cur_paths.add(x.path)

    # 2. Match unique semantic anchors
    for akey in all_anchor_keys:
        e_list = exp_anchor_map.get(akey, [])
        c_list = cur_anchor_map.get(akey, [])
        if len(e_list) == 1 and len(c_list) == 1:
            e_it = e_list[0]
            c_it = c_list[0]
            matched_exp_paths.add(e_it.path)
            matched_cur_paths.add(c_it.path)
            adiffs = _diff_node_attributes(e_it, c_it)
            if e_it.path != c_it.path:
                order_or_index_changes.append({
                    "match_basis": "unique_semantic_anchor",
                    "identity_confirmed": True,
                    "expected_path": e_it.path,
                    "current_path": c_it.path,
                    "expected_node": _node_summary(exp_obs, e_it),
                    "current_node": _node_summary(cur_obs, c_it),
                    "attribute_diffs": adiffs,
                })
            elif adiffs:
                attribute_or_text_diffs.append({
                    "path": e_it.path,
                    "match_basis": "unique_semantic_anchor_same_path",
                    "identity_confirmed": True,
                    "expected_node": _node_summary(exp_obs, e_it),
                    "current_node": _node_summary(cur_obs, c_it),
                    "attribute_diffs": adiffs,
                })

    # 3. Inspect remaining unmatched common paths conservatively without blindly claiming same identity
    common_unmatched_paths = sorted(
        (set(exp_by_path.keys()) - matched_exp_paths) & (set(cur_by_path.keys()) - matched_cur_paths)
    )
    for path in common_unmatched_paths:
        e_it = exp_by_path[path]
        c_it = cur_by_path[path]
        if (
            e_it.el.tag != "node"
            or c_it.el.tag != "node"
            or e_it.el.get("class") != c_it.el.get("class")
            or e_it.el.get("package") != c_it.el.get("package")
        ):
            unconfirmed_replacements.append({
                "path": path,
                "reason": "tag_class_or_package_changed_at_same_path",
                "identity_confirmed": False,
                "expected_node": _node_summary(exp_obs, e_it),
                "current_node": _node_summary(cur_obs, c_it),
                "attribute_diffs": _diff_node_attributes(e_it, c_it),
            })
            continue

        e_akey = _anchor_key(e_it)
        c_akey = _anchor_key(c_it)
        e_res = (e_it.el.get("resource-id") or "").strip()
        c_res = (c_it.el.get("resource-id") or "").strip()
        adiffs = _diff_node_attributes(e_it, c_it)

        # 3a. Both are anonymous containers (no resource-id, text, or content-desc)
        if e_akey is None and c_akey is None:
            if not adiffs:
                matched_exp_paths.add(path)
                matched_cur_paths.add(path)
            elif _subtree_has_unique_preserved_anchor(
                exp_obs, cur_obs, e_it, c_it, exp_anchor_counts, cur_anchor_counts
            ):
                matched_exp_paths.add(path)
                matched_cur_paths.add(path)
                attribute_or_text_diffs.append({
                    "path": path,
                    "match_basis": "anonymous_container_with_preserved_unique_subtree_anchor",
                    "identity_confirmed": True,
                    "expected_node": _node_summary(exp_obs, e_it),
                    "current_node": _node_summary(cur_obs, c_it),
                    "attribute_diffs": adiffs,
                })
            else:
                unconfirmed_replacements.append({
                    "path": path,
                    "reason": "anonymous_container_modified_without_preserved_subtree_anchor",
                    "identity_confirmed": False,
                    "expected_node": _node_summary(exp_obs, e_it),
                    "current_node": _node_summary(cur_obs, c_it),
                    "attribute_diffs": adiffs,
                })
            continue

        # 3b. Resource-id differs -> distinct replacement, never same-node attribute diff
        if e_res != c_res:
            unconfirmed_replacements.append({
                "path": path,
                "reason": "resource_id_changed_or_lost_at_same_path",
                "identity_confirmed": False,
                "expected_node": _node_summary(exp_obs, e_it),
                "current_node": _node_summary(cur_obs, c_it),
                "attribute_diffs": adiffs,
            })
            continue

        # 3c. Same non-empty resource-id at the same path/class/package
        if e_res and e_res == c_res:
            matched_exp_paths.add(path)
            matched_cur_paths.add(path)
            if adiffs:
                attribute_or_text_diffs.append({
                    "path": path,
                    "match_basis": "same_resource_id_and_path_text_or_attr_changed",
                    "identity_confirmed": True,
                    "expected_node": _node_summary(exp_obs, e_it),
                    "current_node": _node_summary(cur_obs, c_it),
                    "attribute_diffs": adiffs,
                })
            continue

        # 3d. Both have empty resource-id, same path/class/package, but text/content-desc anchor changed.
        # Only pair as an unconfirmed slot text change if bounds and clickable state are identical;
        # always mark identity_confirmed=False and also record in unconfirmed_replacements.
        if (
            e_it.el.get("bounds") == c_it.el.get("bounds")
            and e_it.el.get("clickable") == c_it.el.get("clickable")
        ):
            matched_exp_paths.add(path)
            matched_cur_paths.add(path)
            attribute_or_text_diffs.append({
                "path": path,
                "match_basis": "same_path_class_and_bounds_slot_text_changed_identity_unconfirmed",
                "identity_confirmed": False,
                "expected_node": _node_summary(exp_obs, e_it),
                "current_node": _node_summary(cur_obs, c_it),
                "attribute_diffs": adiffs,
            })
            unconfirmed_replacements.append({
                "path": path,
                "reason": "semantic_text_anchor_changed_without_resource_id",
                "identity_confirmed": False,
                "expected_node": _node_summary(exp_obs, e_it),
                "current_node": _node_summary(cur_obs, c_it),
                "attribute_diffs": adiffs,
            })
        else:
            unconfirmed_replacements.append({
                "path": path,
                "reason": "semantic_anchor_and_bounds_or_clickability_changed_without_resource_id",
                "identity_confirmed": False,
                "expected_node": _node_summary(exp_obs, e_it),
                "current_node": _node_summary(cur_obs, c_it),
                "attribute_diffs": adiffs,
            })

    # 4. Remaining unmatched nodes in expected -> removed; in current -> added
    for path in sorted(set(exp_by_path.keys()) - matched_exp_paths):
        removed_nodes.append(_node_summary(exp_obs, exp_by_path[path]))
    for path in sorted(set(cur_by_path.keys()) - matched_cur_paths):
        added_nodes.append(_node_summary(cur_obs, cur_by_path[path]))

    root_attr_diffs = _diff_node_attributes(exp_obs.items[0], cur_obs.items[0])
    structural_or_attr_diff_present = bool(
        root_attr_diffs
        or unsupported_xml_elements
        or attribute_or_text_diffs
        or order_or_index_changes
        or ambiguous_anchors
        or unconfirmed_replacements
        or added_nodes
        or removed_nodes
    )
    raw_byte_only_diff = (not bytes_identical) and (not structural_or_attr_diff_present)

    if bytes_identical:
        classification = "identical_bytes"
    elif raw_byte_only_diff:
        classification = "raw_byte_only_diff"
    else:
        classification = "structural_or_attribute_diff"

    chain_info = _resolve_expected_target_chain(exp_obs, requested_node_id)
    target_chain_report = _evaluate_chain_in_current(
        exp_obs,
        cur_obs,
        chain_info,
        exp_anchor_counts,
        cur_anchor_counts,
        cur_anchor_map,
        cur_by_path,
    )

    return {
        "ok": True,
        "tool_version": TOOL_VERSION,
        "diagnostic_only": True,
        "diagnostic_caveat": (
            "Forensic comparison only. Never use this report to accept a stale observation, "
            "ignore dynamic screen regions, or automatically retry an input action."
        ),
        "hash_computed": True,
        "hash_verified": True,
        "classification": classification,
        "bytes_identical": bytes_identical,
        "raw_byte_only_diff": raw_byte_only_diff,
        "binding_metadata": binding_meta,
        "expected_xml": {
            "path": str(exp_path),
            "declared_sha256": expected_hash,
            "sha256": actual_exp_hash,
            "bytes": len(exp_bytes),
            "node_count": len(exp_obs.items) - 1,
        },
        "current_xml": {
            "path": str(cur_path),
            "declared_sha256": current_hash,
            "sha256": actual_cur_hash,
            "bytes": len(cur_bytes),
            "node_count": len(cur_obs.items) - 1,
        },
        "target_and_ancestor_report": target_chain_report,
        "summary_counts": {
            "root_attribute_diffs": len(root_attr_diffs),
            "unsupported_xml_elements": len(unsupported_xml_elements),
            "attribute_or_text_diffs": len(attribute_or_text_diffs),
            "order_or_index_changes": len(order_or_index_changes),
            "added_nodes": len(added_nodes),
            "removed_nodes": len(removed_nodes),
            "ambiguous_anchors": len(ambiguous_anchors),
            "unconfirmed_replacements": len(unconfirmed_replacements),
        },
        "root_attribute_diffs": root_attr_diffs,
        "unsupported_xml_elements": unsupported_xml_elements,
        "attribute_or_text_diffs": attribute_or_text_diffs,
        "order_or_index_changes": order_or_index_changes,
        "added_nodes": added_nodes,
        "removed_nodes": removed_nodes,
        "ambiguous_anchors": ambiguous_anchors,
        "unconfirmed_replacements": unconfirmed_replacements,
        "truncated": False,
    }


def render_markdown_report(report: Dict[str, Any]) -> str:
    """Render a concise human-readable Markdown summary from a compare_xml_files report."""
    lines = ["# Pre-Input XML Forensic Diff Report", ""]
    if not report.get("ok"):
        lines.append(f"- **Status**: FAILED (`{report.get('error_code')}`)")
        lines.append(f"- **Hash Verified**: `{report.get('hash_verified')}`")
        lines.append(f"- **Message**: {report.get('error_message')}")
        return "\n".join(lines) + "\n"

    exp = report["expected_xml"]
    cur = report["current_xml"]
    counts = report["summary_counts"]
    t_rep = report["target_and_ancestor_report"]

    lines.extend([
        f"- **Classification**: `{report['classification']}`",
        f"- **Hash Verified**: `{report['hash_verified']}`",
        f"- **Bytes Identical**: `{report['bytes_identical']}`",
        f"- **Raw Byte-Only Diff**: `{report['raw_byte_only_diff']}`",
        f"- **Expected XML**: `{exp['sha256']}` ({exp['bytes']} bytes, {exp['node_count']} nodes)",
        f"- **Current XML**: `{cur['sha256']}` ({cur['bytes']} bytes, {cur['node_count']} nodes)",
        "",
        "## Target & Clickable Ancestor Chain",
        f"- **Requested Node ID**: `{t_rep.get('requested_node_id')}`",
        f"- **Resolved Clickable Target**: `{t_rep.get('resolved_clickable_target_node_id')}`",
        f"- **Overall Chain Verdict**: `{t_rep.get('overall_chain_verdict')}`",
    ])
    for n in t_rep.get("nodes", []):
        exp_n = n["expected"]
        lines.append(
            f"  - `{n['role']}` (`{exp_n['path']}`, class=`{exp_n['class']}`, "
            f"text={exp_n['text']!r}): **{n['status']}** via `{n['match_basis']}`"
        )
        if n.get("attribute_diffs"):
            lines.append(f"    - diffs: `{json.dumps(n['attribute_diffs'], ensure_ascii=False)}`")
        if n.get("candidate_evidence"):
            lines.append(f"    - candidate_evidence: `{json.dumps(n['candidate_evidence'], ensure_ascii=False)}`")

    lines.extend([
        "",
        "## Whole-Tree Diff Counts",
        f"- Attribute/Text Diffs: `{counts['attribute_or_text_diffs']}`",
        f"- Order/Index Changes: `{counts['order_or_index_changes']}`",
        f"- Added Nodes: `{counts['added_nodes']}`",
        f"- Removed Nodes: `{counts['removed_nodes']}`",
        f"- Ambiguous Anchors: `{counts['ambiguous_anchors']}`",
        f"- Unconfirmed Replacements: `{counts['unconfirmed_replacements']}`",
        f"- Unsupported XML Elements: `{counts['unsupported_xml_elements']}`",
        "",
        f"> Note: {report['diagnostic_caveat']}",
    ])
    return "\n".join(lines) + "\n"


def compare_from_evidence_meta(meta_json_path: Path) -> Dict[str, Any]:
    """Load a pre-input-mismatch-*.json file and run compare_xml_files against its bound XMLs.

    Returns a structured failure dictionary if the metadata file is missing,
    unreadable, malformed JSON, or missing required binding keys.
    """
    try:
        meta_path = Path(meta_json_path).resolve()
    except Exception as exc:
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": False,
            "hash_verified": False,
            "error_code": "invalid_evidence_meta",
            "error_message": f"Invalid evidence metadata path: {exc}",
        }

    if not meta_path.is_file():
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": False,
            "hash_verified": False,
            "error_code": "invalid_evidence_meta",
            "error_message": f"Evidence metadata file not found: {meta_path}",
        }

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": False,
            "hash_verified": False,
            "error_code": "invalid_evidence_meta",
            "error_message": f"Failed to parse evidence metadata JSON: {exc}",
        }

    if not isinstance(meta, dict):
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": False,
            "hash_verified": False,
            "error_code": "invalid_evidence_meta",
            "error_message": "Evidence metadata JSON must be an object",
        }

    required_keys = ("expected_xml_path", "current_xml_path", "expected_observation_id", "current_hash")
    missing = [k for k in required_keys if not isinstance(meta.get(k), str) or not meta.get(k)]
    if missing:
        return {
            "ok": False,
            "tool_version": TOOL_VERSION,
            "diagnostic_only": True,
            "hash_computed": False,
            "hash_verified": False,
            "error_code": "invalid_evidence_meta",
            "error_message": f"Evidence metadata missing required string fields: {missing}",
        }

    return compare_xml_files(
        expected_xml_path=Path(meta["expected_xml_path"]),
        current_xml_path=Path(meta["current_xml_path"]),
        expected_hash=meta["expected_observation_id"],
        current_hash=meta["current_hash"],
        requested_node_id=meta.get("requested_node_id"),
        binding_meta=meta,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--evidence-meta", type=Path, default=None, help="Path to pre-input-mismatch-*.json")
    p.add_argument("--expected-xml", type=Path, default=None)
    p.add_argument("--current-xml", type=Path, default=None)
    p.add_argument("--expected-hash", default=None)
    p.add_argument("--current-hash", default=None)
    p.add_argument("--node-id", default=None)
    p.add_argument("--json-out", type=Path, default=None)
    p.add_argument("--md-out", type=Path, default=None)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.evidence_meta is not None:
        report = compare_from_evidence_meta(args.evidence_meta)
    else:
        if not args.expected_xml or not args.current_xml:
            sys.stderr.write("Either --evidence-meta or both --expected-xml and --current-xml must be provided\n")
            return 2
        report = compare_xml_files(
            expected_xml_path=args.expected_xml,
            current_xml_path=args.current_xml,
            expected_hash=args.expected_hash,
            current_hash=args.current_hash,
            requested_node_id=args.node_id,
        )

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.md_out is not None:
        args.md_out.parent.mkdir(parents=True, exist_ok=True)
        args.md_out.write_text(render_markdown_report(report), encoding="utf-8")

    sys.stdout.write(json.dumps(report, ensure_ascii=False) + "\n")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
