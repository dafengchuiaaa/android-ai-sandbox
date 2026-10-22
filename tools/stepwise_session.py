"""One bounded session; external agent chooses each formal CLI action.

No automatic UI action sequence. A worker owns cleanup even while the agent
is between turns. Runtime requests are data, never shell commands.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid

import stepwise_support as support

TOTAL_S = 360
CLEANUP_S = 40
MAX_CALLS = 16
PROFILE = 'api35-google-x86_64'
PACKAGE = 'org.fdroid.basic'
HASH = re.compile(r'[0-9a-f]{64}\Z')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def observation(payload):
    data = payload.get('data')
    if not isinstance(data, dict):
        return None
    obs = data.get('after_observation', data)
    return obs if isinstance(obs, dict) and isinstance(obs.get('nodes'), list) else None


PREVIEW_KEYS = ('node_id', 'path', 'package', 'class', 'resource_id', 'text', 'content_desc',
                'bounds', 'bounds_raw', 'bounds_valid', 'enabled', 'clickable', 'focused', 'password')
NODE_KEYS = ('node_id', 'parent_id', 'path', 'package', 'class', 'resource_id', 'text', 'content_desc',
             'bounds', 'bounds_raw', 'bounds_valid', 'enabled', 'clickable', 'focused', 'password')


def node_preview(node, keys=NODE_KEYS):
    row = {k: node.get(k) for k in keys}
    if node.get('password') is True:
        # Accessibility dumps carry password-field text in clear text. Never repeat it
        # into a summary that exists to be read outside the device sandbox.
        row['text'] = None
        row['content_desc'] = None
    return row


def summary(payload):
    obs = observation(payload)
    result = {'ok': payload.get('ok'), 'action': payload.get('action'), 'error': payload.get('error')}
    if obs is None:
        result['data'] = payload.get('data')
        return result
    raw = obs.get('nodes') if isinstance(obs.get('nodes'), list) else []
    raw_nodes = [n for n in raw if isinstance(n, dict)]
    observed_packages = sorted({n.get('package') for n in raw_nodes if n.get('package')})
    ready, readiness_reason = support.is_fdroid_ready(obs)
    inferred_pkg = observed_packages[0] if len(observed_packages) == 1 else None
    # Parsed observations include the metadata-free <hierarchy> wrapper at R. Prefer the
    # first node with an attributed package, but a window may legitimately parse with
    # package=null on every node, so fall back to the first node carrying any metadata
    # and finally to the bare wrapper instead of hiding what was actually observed.
    r0 = next((n for n in raw_nodes if n.get('package')), None)
    if r0 is None:
        r0 = next((n for n in raw_nodes if any(n.get(k) not in (None, '', [], {}) for k in
                  ('class', 'resource_id', 'text', 'content_desc', 'bounds_raw', 'bounds'))), None)
    if r0 is None and raw_nodes:
        r0 = raw_nodes[0]
    root_preview = None if r0 is None else node_preview(r0, PREVIEW_KEYS)
    nodes = [n for n in raw_nodes if n.get('enabled') is True and
             (n.get('text') or n.get('content_desc') or n.get('class') == 'android.widget.EditText')]
    result.update(observation_id=obs.get('observation_id'), package=inferred_pkg,
                  observed_packages=observed_packages,
                  target_package=PACKAGE,
                  target_package_ready=ready,
                  target_readiness_reason=readiness_reason,
                  root_node_preview=root_preview,
                  malformed_nodes=len(raw) - len(raw_nodes),
                  nodes=[node_preview(n) for n in nodes[:80]],
                  summary_truncated=len(nodes) > 80, total_nodes=len(obs['nodes']))
    return result


def is_tight_wrapper(node, target_node, obs):
    if node.get('clickable') is not True:
        return False
    target_path = node.get('path', '')
    descendants = [n for n in obs.get('nodes', []) if n.get('path', '').startswith(target_path + '.')]
    # Labeled descendants other than target_node
    other_labeled = [
        d for d in descendants
        if (d.get('text') or d.get('content_desc')) and d.get('node_id') != target_node.get('node_id')
    ]
    if other_labeled:
        return False
    n_depth = target_path.count('.')
    t_depth = target_node.get('path', '').count('.')
    if t_depth - n_depth > 2:
        return False
    # Geometric containment check
    nb = node.get('bounds')
    tb = target_node.get('bounds')
    if nb and tb and len(nb) == 4 and len(tb) == 4:
        if not (nb[0] <= tb[0] and nb[1] <= tb[1] and nb[2] >= tb[2] and nb[3] >= tb[3]):
            return False
    return True


OFFICIAL_REPOSITORY_FINGERPRINT = '43238d512c1e5eb2d6569f4a3afbf5523418b82e0a3ed1552770abb9a9c9ccab'
OFFICIAL_REPOSITORY_DESCRIPTION = 'The official F-Droid Free Software repository. Everything in this repository is always built from the source code.'


def official_repository_visible(obs):
    nodes = [n for n in obs.get('nodes', []) if isinstance(n, dict) and n.get('package') == PACKAGE]
    if not any(n.get('text') == 'F-Droid' for n in nodes):
        return False
    if any(n.get('text') == 'https://f-droid.org/repo' for n in nodes):
        return True
    # Basic 2.0.2 deliberately shortens every displayed URL. This is an
    # explicit alternative identity proof, not a claim that a URL was observed.
    # The archive shares this key; title, domain AND description remain required.
    if any(n.get('text') == 'F-Droid Archive' for n in nodes):
        return False
    context = []
    for text in ('F-Droid', 'f-droid.org', OFFICIAL_REPOSITORY_DESCRIPTION, 'Signing key fingerprint (SHA-256)'):
        matches = [n for n in nodes if n.get('text') == text and n.get('enabled') is True and
                   n.get('bounds_valid') is True and n.get('password') is not True]
        if len(matches) != 1:
            return False
        context.append(matches[0])
    content = context[0].get('parent_id')
    if not content or any(n.get('parent_id') != content for n in context[1:3]):
        return False
    headers = [n for n in nodes if n.get('node_id') == context[3].get('parent_id') and n.get('parent_id') == content]
    if len(headers) != 1 or any('https://' in (n.get('text') or '') or 'http://' in (n.get('text') or '') for n in nodes):
        return False
    fingerprints = [n for n in nodes if n.get('class') == 'android.widget.TextView' and
                    n.get('enabled') is True and n.get('bounds_valid') is True and
                    n.get('password') is not True and n.get('checkable') is not True and
                    re.fullmatch(r'[0-9A-Fa-f\s:]+', n.get('text') or '') and
                    re.sub(r'[\s:]', '', n['text']).lower() == OFFICIAL_REPOSITORY_FINGERPRINT]
    if len(fingerprints) != 1:
        return False
    pin, header = fingerprints[0], headers[0]
    content_path = context[0]['path'].rsplit('.', 1)[0]
    if not pin.get('path', '').startswith(content_path + '.'):
        return False
    if pin['path'].startswith(header['path'] + '.'):
        return True
    try:
        adjacent = content_path + '.' + str(int(header['path'].rsplit('.', 1)[1]) + 1)
        if pin.get('parent_id') == content and pin['path'] == adjacent:
            return True
        wrappers = [n for n in nodes if n.get('path') == adjacent and n.get('parent_id') == content]
        if len(wrappers) != 1:
            return False
        wrapper = wrappers[0]
        if (wrapper.get('class') != 'android.view.View' or wrapper.get('enabled') is not True or
                wrapper.get('bounds_valid') is not True or wrapper.get('clickable') is not False or
                wrapper.get('checkable') is not False or wrapper.get('password') is not False or
                wrapper.get('actions') != [] or pin.get('parent_id') != wrapper.get('node_id') or
                pin.get('path') != adjacent + '.0'):
            return False
        descendants = [n for n in obs.get('nodes', []) if n.get('path', '').startswith(adjacent + '.')]
        if len(descendants) != 1 or descendants[0] != pin:
            return False
        outer, inner = wrapper.get('bounds'), pin.get('bounds')
        return (isinstance(outer, list) and len(outer) == 4 and isinstance(inner, list) and len(inner) == 4 and
                outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3])
    except (ValueError, KeyError):
        return False


def repository_fingerprint_header(node, obs):
    """Only the collapsed section header, never a mirror row or generic Expand."""
    nodes = [n for n in obs.get('nodes', []) if isinstance(n, dict) and n.get('package') == PACKAGE]
    labels = [n for n in nodes if n.get('text') == 'Signing key fingerprint (SHA-256)']
    if (len(labels) != 1 or official_repository_visible(obs) or
            not any(n.get('text') == 'F-Droid' for n in nodes) or
            not any(n.get('text') == 'f-droid.org' for n in nodes) or
            not any(n.get('content_desc') == 'Check for updates' for n in nodes)):
        return None
    label = labels[0]
    # The observed 2.0.2 header has a label and an Expand arrow in the same row.
    if (node.get('node_id') != label.get('parent_id') or node.get('path') != label.get('path', '').rsplit('.', 1)[0] or
            node.get('class') != 'android.view.View' or node.get('clickable') is not True or
            node.get('checkable') is True or node.get('password') is True):
        return None
    descendants = [n for n in obs.get('nodes', []) if isinstance(n, dict) and n.get('path', '').startswith(node['path'] + '.')]
    if (not descendants or any(n.get('package') != PACKAGE or n.get('checkable') is True or
                              'Switch' in (n.get('class') or '') for n in descendants)):
        return None
    labeled = [n for n in descendants if n.get('text') or n.get('content_desc')]
    arrows = [n for n in labeled if n.get('content_desc') == 'Expand']
    if len(labeled) != 2 or len(arrows) != 1 or label not in labeled:
        return None
    bounds = node.get('bounds')
    if not isinstance(bounds, list) or len(bounds) != 4:
        return None
    for child in (label, arrows[0]):
        b = child.get('bounds')
        if (child.get('enabled') is not True or child.get('bounds_valid') is not True or
                not isinstance(b, list) or len(b) != 4 or
                not (bounds[0] <= b[0] < b[2] <= bounds[2] and bounds[1] <= b[1] < b[3] <= bounds[3])):
            return None
    return label


def is_permitted_refresh_click_target(node, obs):
    if node.get('package') != PACKAGE or node.get('enabled') is not True or node.get('bounds_valid') is not True:
        return False, 'target outside permitted package or not enabled/bounds_valid', None
    desc = node.get('content_desc') or ''
    text = node.get('text') or ''
    header = repository_fingerprint_header(node, obs)
    if header is not None:
        return True, 'repository_fingerprint_header', header

    # 1. Repositories tab
    if desc == 'Repositories' or text == 'Repositories':
        return True, 'repositories_tab', node
    for other in obs.get('nodes', []):
        if (other.get('content_desc') == 'Repositories' or other.get('text') == 'Repositories') and is_tight_wrapper(node, other, obs):
            return True, 'repositories_tab_container', other

    # 2. Got it dialog button
    if text == 'Got it':
        return True, 'dialog_got_it', node
    for other in obs.get('nodes', []):
        if other.get('text') == 'Got it' and is_tight_wrapper(node, other, obs):
            return True, 'dialog_got_it_container', other

    # 3. F-Droid repo item
    if text == 'F-Droid':
        return True, 'fdroid_repo_item', node
    for other in obs.get('nodes', []):
        if other.get('text') == 'F-Droid' and is_tight_wrapper(node, other, obs):
            return True, 'fdroid_repo_item_container', other

    # 4. Check for updates button
    if desc == 'Check for updates':
        return True, 'check_for_updates', node
    for other in obs.get('nodes', []):
        if other.get('content_desc') == 'Check for updates' and is_tight_wrapper(node, other, obs):
            return True, 'check_for_updates_container', other

    return False, f'node {node.get("node_id")} is outside permitted application_refresh navigation surface', None


class Policy:
    def __init__(self, mode='stepwise_input', session_id=None, expected_device_serial=None,
                 controlled_evidence_dir=None):
        self.mode = mode
        self.session_id = session_id
        self.expected_device_serial = expected_device_serial
        self.controlled_evidence_dir = controlled_evidence_dir
        self.calls = 0
        self.used = {'launch': 0, 'click': 0, 'set-text': 0, 'observe': 0}
        self.latest = None
        self.navigation_recoveries = 0
        self.observation_required = False
        self.fingerprint_header_claimed = False
        self.target = None
        self.text_succeeded = False
        self.independent_readback = False
        self.max_clicks = 5 if mode == 'application_refresh' else 1
        self.max_set_text = 0 if mode == 'application_refresh' else 1
        self.max_observes = 3 if mode == 'application_refresh' else 16
        self.refresh_observes_used = 0
        self.refresh_triggered = False
        self.refresh_clicked = False
        self.requested_target_identity = None
        self.semantic_button_identity = None
        self.refresh_button_identity = None
        self.refresh_action_seq = None
        self.refresh_in_progress = False
        self.refresh_ui_completed = False
        self.network_verified = False
        self.goal_verified = False
        self.require_raw_log_file = (mode == 'application_refresh')

    @classmethod
    def from_config(cls, cfg):
        return cls(
            mode=cfg.get('mode', 'stepwise_input'),
            session_id=cfg.get('session_id'),
            controlled_evidence_dir=cfg.get('controlled_evidence_dir')
        )

    def validate_network_check(self, net):
        if not isinstance(net, dict):
            return False
        if self.session_id and net.get('session_id') != self.session_id:
            return False
        if self.refresh_action_seq is not None:
            if net.get('refresh_seq') != self.refresh_action_seq:
                return False
        device_serial = net.get('device_serial')
        if not isinstance(device_serial, str) or not device_serial:
            return False
        if self.expected_device_serial and device_serial != self.expected_device_serial:
            return False
        raw_hash = net.get('raw_log_sha256')
        if not isinstance(raw_hash, str) or not re.fullmatch(r'^[0-9a-fA-F]{64}$', raw_hash):
            return False

        is_refresh = (self.mode == 'application_refresh' or getattr(self, 'require_raw_log_file', False))
        raw_path = net.get('raw_log_path')
        if is_refresh:
            if not isinstance(raw_path, str) or not raw_path:
                return False
            if not os.path.isfile(raw_path):
                return False
            if self.controlled_evidence_dir:
                ctrl_real = os.path.realpath(str(self.controlled_evidence_dir))
                raw_real = os.path.realpath(raw_path)
                if not (raw_real == ctrl_real or raw_real.startswith(ctrl_real + os.sep)):
                    return False
            try:
                with open(raw_path, 'rb') as f:
                    file_hash = hashlib.sha256(f.read()).hexdigest()
                if file_hash.lower() != raw_hash.lower():
                    return False
            except Exception:
                return False
        else:
            if raw_path is not None:
                if not isinstance(raw_path, str) or not os.path.isfile(raw_path):
                    return False
                try:
                    with open(raw_path, 'rb') as f:
                        file_hash = hashlib.sha256(f.read()).hexdigest()
                    if file_hash.lower() != raw_hash.lower():
                        return False
                except Exception:
                    return False
        worker_tag = net.get('worker_tag')
        if not isinstance(worker_tag, str) or not worker_tag:
            return False
        required_keys = [
            'network_verified', 'entry_jar_requested', 'downloader_progress',
            'positive_bytes_transferred', 'workmanager_success',
            'pid_matched', 'action_window_matched', 'same_chain_matched'
        ]
        if not all(net.get(k) is True for k in required_keys):
            return False
        if net.get('bytes_transferred', 0) <= 0:
            return False
        if net.get('repo_update_result') not in ('IndexUpdateResult$Unchanged', 'IndexUpdateResult$Updated'):
            return False
        if net.get('error') is not None:
            return False
        return True

    def prepare(self, req):
        action = req.get('action')
        allowed_actions = {'status', 'launch', 'observe', 'click', 'finish'} if self.mode == 'application_refresh' else {'status', 'launch', 'observe', 'click', 'set-text', 'finish'}
        if action not in allowed_actions:
            raise ValueError('action not allowed')
        if not isinstance(req.get('reason'), str) or not 8 <= len(req['reason']) <= 2000:
            raise ValueError('a concrete decision reason is required')
        if self.observation_required and action not in ('observe', 'finish'):
            raise ValueError('navigation rejected before input; obtain a new observation or finish')
        if action == 'finish':
            if self.mode == 'application_refresh':
                net = req.get('network_check')
                net_ok = self.validate_network_check(net)
                if self.refresh_ui_completed and net_ok:
                    self.network_verified = True
                    self.goal_verified = True
                else:
                    self.network_verified = False
                    self.goal_verified = False
            return []
        if self.calls >= MAX_CALLS:
            raise ValueError('CLI call limit exhausted')
        args = []
        if action == 'launch':
            if self.used[action] or self.calls != 0:
                raise ValueError('one initial launch only')
            args = ['--package', PACKAGE]
        elif action == 'observe':
            if self.mode == 'application_refresh':
                if not self.refresh_triggered:
                    if self.used['observe'] >= 1:
                        raise ValueError('launch readiness observe limit exhausted (max 1)')
                else:
                    if self.refresh_observes_used >= 2:
                        raise ValueError('refresh observe limit exhausted (max 2)')
                if self.used['observe'] >= self.max_observes:
                    raise ValueError('observe call limit exhausted (max 3)')
        elif action in {'click', 'set-text'}:
            if action == 'set-text' and self.max_set_text == 0:
                raise ValueError('set-text is not permitted in application_refresh mode')
            if action == 'click' and self.used['click'] >= self.max_clicks:
                raise ValueError('click call limit exhausted')
            if action == 'click' and self.mode == 'application_refresh' and self.refresh_triggered:
                raise ValueError('refresh already triggered; await completion via observe')
            if self.mode == 'stepwise_input' and self.used[action]:
                raise ValueError('input may not be replayed')
            obs = self.latest
            oid, nid = req.get('observation_id'), req.get('node_id')
            if not isinstance(oid, str) or not HASH.fullmatch(oid) or not obs or oid != obs.get('observation_id'):
                raise ValueError('input must reference the latest full observation hash')
            matches = [n for n in obs['nodes'] if n.get('node_id') == nid]
            if len(matches) != 1:
                raise ValueError('node not unique in latest observation')
            node = matches[0]
            if node.get('package') != PACKAGE or node.get('enabled') is not True or node.get('bounds_valid') is not True or node.get('password') is True:
                raise ValueError('target is outside the permitted surface')
            if self.mode == 'application_refresh' and action == 'click':
                permitted, reason, semantic_node = is_permitted_refresh_click_target(node, obs)
                if not permitted:
                    raise ValueError('click rejected: ' + reason)
                if reason == 'repository_fingerprint_header':
                    if self.fingerprint_header_claimed:
                        raise ValueError('repository fingerprint header may be opened only once')
                    self.fingerprint_header_claimed = True
                if reason.startswith('check_for_updates'):
                    # 1. Official F-Droid repository detail page verification (title + repo address)
                    has_fdroid_title = any(
                        n.get('package') == PACKAGE and n.get('text') == 'F-Droid'
                        for n in obs.get('nodes', [])
                    )
                    has_fdroid_address = official_repository_visible(obs)
                    if not (has_fdroid_title and has_fdroid_address):
                        raise ValueError('click rejected: not on official F-Droid repository page (requires exact full URL or pinned signing identity in the repository content)')
                    # 2. Strict uniqueness check for Check for updates button (all instances regardless of enabled)
                    all_update_btns = [
                        n for n in obs.get('nodes', [])
                        if n.get('package') == PACKAGE and n.get('content_desc') == 'Check for updates'
                    ]
                    if len(all_update_btns) != 1:
                        raise ValueError(f'click rejected: expected exactly 1 Check for updates button, found {len(all_update_btns)}')
                    sem_btn = all_update_btns[0]
                    if sem_btn.get('enabled') is not True or sem_btn.get('bounds_valid') is not True:
                        raise ValueError('click rejected: Check for updates button is not enabled or has invalid bounds')
                    if semantic_node.get('node_id') != sem_btn.get('node_id'):
                        raise ValueError('click rejected: semantic target does not match unique Check for updates button')
                    # 3. Pre-existing in-progress task check (no ProgressBar allowed before click)
                    has_pre_progress = any(
                        n.get('package') == PACKAGE and (n.get('class') == 'android.widget.ProgressBar' or n.get('class_name') == 'android.widget.ProgressBar') and n.get('bounds_valid') is True
                        for n in obs.get('nodes', [])
                    )
                    if has_pre_progress:
                        raise ValueError('click rejected: pre-existing in-progress task detected before refresh click')
                    self.requested_target_identity = {
                        'node_id': node.get('node_id'),
                        'path': node.get('path'),
                        'package': node.get('package'),
                        'class': node.get('class') or node.get('class_name'),
                        'resource_id': node.get('resource_id'),
                        'bounds': list(node.get('bounds') or []),
                        'content_desc': node.get('content_desc')
                    }
                    self.semantic_button_identity = {
                        'node_id': sem_btn.get('node_id'),
                        'path': sem_btn.get('path'),
                        'package': sem_btn.get('package'),
                        'class': sem_btn.get('class') or sem_btn.get('class_name'),
                        'resource_id': sem_btn.get('resource_id'),
                        'bounds': list(sem_btn.get('bounds') or []),
                        'content_desc': 'Check for updates'
                    }
                    self.refresh_button_identity = dict(self.semantic_button_identity)
                    self.refresh_triggered = True
                    self.refresh_action_seq = self.calls + 1
            args = ['--observation-id', oid, '--node-id', nid]
            if self.mode != 'application_refresh' and action == 'set-text':
                edits = [n for n in obs['nodes'] if n.get('package') == PACKAGE and n.get('class') == 'android.widget.EditText'
                         and n.get('enabled') is True and n.get('bounds_valid') is True]
                if len(edits) != 1 or edits[0] != node or node.get('text') not in (None, ''):
                    raise ValueError('set-text requires the unique initially empty EditText')
                if req.get('value') != 'Aurora':
                    raise ValueError('this bounded demonstration permits Aurora only')
                self.target = dict(node)
                args += ['--value', 'Aurora']
        self.calls += 1
        if action in self.used:
            self.used[action] += 1  # Claim before any subprocess; failures cannot release this claim.
        return args

    def accept(self, action, payload):
        if payload.get('ok') is not True:
            raise ValueError('CLI failure ends session')
        obs = observation(payload)
        if self.observation_required and (action != 'observe' or obs is None):
            raise ValueError('recovery requires a new formal observation')
        if obs is not None:
            self.latest = obs
            if action == 'observe':
                self.observation_required = False
        if self.mode == 'application_refresh':
            if action == 'click' and self.refresh_triggered and not self.refresh_clicked:
                self.refresh_clicked = True
                raw_nodes = obs.get('nodes', []) if obs else []
                has_progress = any(
                    n.get('package') == PACKAGE and (n.get('class') == 'android.widget.ProgressBar' or n.get('class_name') == 'android.widget.ProgressBar') and n.get('bounds_valid') is True
                    for n in raw_nodes if isinstance(n, dict)
                )
                btn_disabled = False
                if self.semantic_button_identity:
                    btn_disabled = any(
                        n.get('package') == PACKAGE and n.get('path') == self.semantic_button_identity.get('path')
                        and n.get('content_desc') == 'Check for updates' and n.get('enabled') is False
                        for n in raw_nodes if isinstance(n, dict)
                    )
                if has_progress or btn_disabled:
                    self.refresh_in_progress = True
            elif action == 'observe' and self.refresh_triggered:
                self.refresh_observes_used += 1
                raw_nodes = obs.get('nodes', []) if obs else []
                has_progress = any(
                    n.get('package') == PACKAGE and (n.get('class') == 'android.widget.ProgressBar' or n.get('class_name') == 'android.widget.ProgressBar') and n.get('bounds_valid') is True
                    for n in raw_nodes if isinstance(n, dict)
                )
                matching_btns = [
                    n for n in raw_nodes
                    if isinstance(n, dict) and n.get('package') == PACKAGE and n.get('content_desc') == 'Check for updates'
                ]
                btn_disabled = False
                btn_enabled = False
                btn_identity_matches = False

                if len(matching_btns) == 1 and self.semantic_button_identity:
                    btn = matching_btns[0]
                    sem = self.semantic_button_identity
                    exact_match = (
                        btn.get('package') == sem['package'] and
                        btn.get('path') == sem['path'] and
                        (btn.get('class') or btn.get('class_name')) == sem['class'] and
                        btn.get('resource_id') == sem['resource_id'] and
                        btn.get('bounds') == sem['bounds'] and
                        btn.get('bounds_valid') is True
                    )
                    if exact_match:
                        if btn.get('enabled') is False:
                            btn_disabled = True
                        elif btn.get('enabled') is True:
                            btn_enabled = True
                            btn_identity_matches = True
                            if self.requested_target_identity and self.requested_target_identity['node_id'] != sem['node_id']:
                                req_wrappers = [
                                    n for n in raw_nodes
                                    if isinstance(n, dict) and n.get('path') == self.requested_target_identity['path']
                                    and is_tight_wrapper(n, btn, obs)
                                ]
                                if not req_wrappers:
                                    btn_identity_matches = False

                if has_progress or btn_disabled:
                    self.refresh_in_progress = True

                on_fdroid_page = official_repository_visible(obs or {})
                if not has_progress and btn_enabled and btn_identity_matches and on_fdroid_page:
                    if self.refresh_in_progress:
                        self.refresh_ui_completed = True
        else:
            if action == 'set-text':
                data = payload['data']
                if data.get('verified') is not True or data.get('readback_text') != 'Aurora':
                    raise ValueError('backend text verification missing')
                self.text_succeeded = True
            if action == 'observe' and self.text_succeeded:
                target = self.target
                matches = [n for n in obs['nodes'] if all(n.get(k) == target.get(k) for k in
                           ['path', 'package', 'class', 'resource_id'])]
                if len(matches) != 1:
                    raise ValueError('independent readback target not unique')
                node = matches[0]
                before, after = target['bounds'], node.get('bounds')
                if (node.get('text') != 'Aurora' or node.get('focused') is not True or
                    node.get('enabled') is not True or node.get('bounds_valid') is not True or
                    not isinstance(after, list) or len(after) != 4 or
                    after[2] <= after[0] or after[3] <= after[1] or
                    [after[0], after[1], after[3]] != [before[0], before[1], before[3]]):
                    raise ValueError('independent text/focus/anchor verification failed')
                self.independent_readback = True
                self.goal_verified = True


def verify_files(project, manifests):
    files = {}
    for manifest in manifests:
        for rel, expected in load(manifest)['files'].items():
            target = (project / rel).resolve()
            if not target.is_relative_to(project) or not HASH.fullmatch(expected):
                raise ValueError('manifest entry escapes project or has invalid hash')
            if str(target) in files and files[str(target)] != expected:
                raise ValueError('conflicting manifest entries')
            files[str(target)] = expected
    if not files:
        raise ValueError('empty source manifest')
    differences = [p for p, expected in files.items() if not Path(p).is_file() or digest(p) != expected]
    if differences:
        raise ValueError('source drift: ' + repr(differences[:3]))
    return len(files)


class Session(support.SingleCaseRunner):
    def formal_action(self, label, req, args, policy, validator, serial):
        # Only this session path defers business-failure handling until after the
        # response contract and the no-input proof. Other CLI callers are unchanged.
        payload = self.call_cli(label, req['action'], *args, check=False)
        cmd = self.commands[-1]
        checked = validator.validate(payload, cmd['exit_code'], req['action'],
                                     expected_serial=serial, expected_profile=PROFILE)
        save(self.run_dir / f'validation-{req["seq"]:04d}.json', checked)
        recovery = None
        if checked.get('protocol_valid') is True and payload.get('ok') is False:
            if not self.unknown_input_state and can_recover_navigation(payload, cmd, req, policy, self.session_dir):
                policy.navigation_recoveries += 1
                policy.observation_required = True
                policy.latest = None
                recovery = {'kind': 'navigation_observation_required_v1',
                            'input_sent': False, 'allowed_actions': ['observe', 'finish']}
        if checked.get('protocol_valid') is not True or (payload.get('ok') is not True and recovery is None):
            if req['action'] in ('click', 'set-text'):
                self.unknown_input_state = True
                self.verdict['unknown_input_state'] = True
            err = payload.get('error') or {}
            raise support.RunnerError(err.get('code', 'invalid_cli_response'),
                                      'formal CLI response rejected', payload)
        if recovery is None:
            policy.accept(req['action'], payload)
        return payload, checked, recovery

    def verify_manifest(self, stage_label):
        count = verify_files(self.project_root, self.manifests)
        for path, expected in self.tool_hashes.items():
            if digest(path) != expected:
                raise ValueError('session tool changed during execution')
        save(self.run_dir / (stage_label + '.json'), {'verified': True, 'file_count': count, 'tool_hashes': self.tool_hashes})


def is_navigation_recovery_response(response):
    """Project a committed worker recovery into a read-only briefing state."""
    if not isinstance(response, dict):
        return False
    marker = response.get('recovery')
    payload = response.get('payload') or {}
    detail = (payload.get('error') or {}).get('detail') or {}
    stage = detail.get('click_stage') or {}
    return (marker == {'kind': 'navigation_observation_required_v1', 'input_sent': False,
                       'allowed_actions': ['observe', 'finish']} and
            response.get('validation', {}).get('protocol_valid') is True and
            payload.get('ok') is False and payload.get('action') == 'click' and
            (payload.get('error') or {}).get('code') == 'stale_observation' and
            stage.get('stage') == 'pre_input_read' and stage.get('input_attempted') is False and
            stage.get('input_phase') == 'before_input' and
            (stage.get('input_send') or {}).get('requested') is False)


def can_recover_navigation(payload, cmd, req, policy, evidence_dir):
    """One read-only recovery of a proven pre-input navigation rejection.

    Diagnostic XML is audited but never becomes an actionable observation.
    Missing/contradictory evidence falls through to the existing terminal path.
    """
    try:
        if (policy.mode != 'application_refresh' or policy.refresh_triggered or
                policy.navigation_recoveries != 0 or policy.used['observe'] != 0 or
                req['action'] != 'click' or payload.get('action') != 'click' or
                payload.get('ok') is not False or type(cmd.get('exit_code')) is not int or cmd['exit_code'] != 1 or
                cmd.get('timed_out') is not False or not policy.latest):
            return False
        oid, nid = req['observation_id'], req['node_id']
        if not HASH.fullmatch(oid) or oid != policy.latest['observation_id']:
            return False
        matches = [n for n in policy.latest['nodes'] if n.get('node_id') == nid]
        if len(matches) != 1:
            return False
        permitted, reason, _ = is_permitted_refresh_click_target(matches[0], policy.latest)
        if not permitted or reason.startswith('check_for_updates'):
            return False
        error = payload['error']
        if error['code'] != 'stale_observation' or payload.get('data') is not None:
            return False
        detail = error['detail']
        stage = detail['click_stage']
        send = stage['input_send']
        proof = detail['pre_input_mismatch_evidence']
        if (detail['stage'] != 'pre_input_read' or stage['stage'] != 'pre_input_read' or
                stage['stages_entered'] != ['verify_live_device', 'load_observation', 'pre_input_read'] or
                stage['input_attempted'] is not False or stage['input_phase'] != 'before_input' or
                stage['app_state_after_input'] != 'not_attempted' or
                send.get('requested') is not False or send.get('command_returned') is not False or
                send != {'requested': False, 'coordinates': None, 'command_returned': False,
                         'returned_ok': None, 'error_kind': None, 'error_message': None} or
                proof['schema'] != 'pre_input_mismatch_evidence_v1' or
                proof['stage'] != 'pre_input_read' or proof['input_attempted'] is not False or
                proof['input_phase'] != 'before_input' or proof['input_send_requested'] is not False or
                proof['requested_node_id'] != nid or proof['read_backend'] != 'stable-direct'):
            return False
        for row in (stage, proof):
            if row['profile_id'] != PROFILE or row['device_serial'] != policy.expected_device_serial:
                return False
        new_hash = detail['current_hash']
        if (detail['expected_observation_id'] != oid or proof['expected_observation_id'] != oid or
                not HASH.fullmatch(new_hash) or new_hash == oid or proof['current_hash'] != new_hash):
            return False
        root = Path(evidence_dir).resolve()
        def owned_file(value):
            path = Path(value).resolve()
            if not path.is_relative_to(root) or not path.is_file():
                raise ValueError('evidence outside owned runtime')
            return path
        old_xml = owned_file(proof['expected_xml_path'])
        new_xml = owned_file(proof['current_xml_path'])
        trace_path = owned_file(proof['read_evidence'])
        meta_path = owned_file(proof['evidence_meta_path'])
        if (old_xml.name != proof['expected_xml_filename'] or new_xml.name != proof['current_xml_filename'] or
                meta_path.name != proof['evidence_meta_filename'] or load(meta_path) != proof or
                digest(old_xml) != oid or digest(new_xml) != new_hash or
                new_xml.stat().st_size != proof['current_bytes_len']):
            return False
        trace = load(trace_path)
        valid, audited = support.validate_stable_direct_trace(trace)
        return (valid and trace.get('ok') is True and trace.get('is_launch') is False and
                audited['verified_sha256'] == new_hash and audited['package'] == PACKAGE)
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        return False


STARTUP_LOGCAT_TAIL_LIMIT = 4000
MAX_DIAGNOSTIC_RAW_BYTES = 10 * 1024 * 1024
TIMESTAMP_PATTERN = re.compile(r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\b")
DIAGNOSTIC_TAGS = {
    'ActivityTaskManager', 'WindowManager', 'PermissionController',
    'PackageManager', 'ActivityManager', 'UiAutomation'
}
DIAG_LINE_PATTERN = re.compile(
    r"^(\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d{3})\s+(\d+)\s+(\d+)\s+([VDIWEF])\s+([^:]+):\s+(.*)$"
)


def collect_startup_logcat_diagnostic(runner, state, out, active_request=None, action=None, opt_in=False):
    """Bounded, failure-path startup logcat diagnostic run by the worker owning the device.

    Strict invariants:
    - Never runs after worker is in 'stopping' or after Stop.
    - Explicit opt-in required via configuration (enable_startup_logcat_diagnostic: True).
    - Only runs during admitted launch/startup failure stage (active_request in (None, 1) and action in (None, 'launch')).
    - Only runs if worker has successfully completed owned-status verification for this instance.
    - Local state-file verification: state file path, serial, PID, and creation identity must match verified session.
    - Missing, corrupted, mismatched, or altered state content skips with zero device commands.
    - Bounded to at most 8.0s timeout and bounded by operational deadline.
    - Strictly reserves CLEANUP_S (40s); skips if remaining_s(reserve_cleanup=True) < 8.0s.
    - Real SingleCaseRunner command inspection: reads actual stdout/stderr file paths and raw_stdout/raw_stderr byte files.
    - Computes raw_log_sha256 from actual bytes; for oversized logs (>10MB post-processing threshold), binds existing raw file and skips hash computation to strictly preserve 40s cleanup budget, marking evidence_incomplete.
    - If raw evidence file is missing, unreadable, or write fails, records status='failed', reason='evidence_incomplete'.
    - Missing command record, missing/None/non-int exit code, or timeout recorded as failed, never collected.
    - Only exit code 0 with intact raw evidence is recorded as collected.
    - Keeps host monotonic times and device logcat timestamps separate (no forced cross-clock stitching).
    - Decoupled from network refresh verification credentials.
    """
    def _save_diag(res):
        try:
            save(runner.run_dir / 'startup-logcat-diagnostic.json', res)
            save(out / 'startup-logcat-diagnostic.json', res)
        except Exception:
            pass

    if not opt_in:
        res = {'status': 'skipped', 'reason': 'diagnostic_not_opted_in', 'detail': 'Task 99 startup logcat diagnostic requires explicit opt-in in session config'}
        _save_diag(res)
        return res

    is_startup_stage = (active_request in (None, 1)) and (action in (None, 'launch')) and getattr(runner, 'startup_stage', True)
    if not is_startup_stage:
        res = {'status': 'skipped', 'reason': 'non_startup_stage', 'detail': f'Diagnostic only admitted for launch startup failure stage, got active_request={active_request}, action={action}'}
        _save_diag(res)
        return res

    if not getattr(runner, 'owned_status_verified', False):
        res = {'status': 'skipped', 'reason': 'owned_status_not_verified', 'detail': 'Worker has not successfully completed owned-status verification for this instance'}
        _save_diag(res)
        return res

    if not getattr(runner, 'boot_attempted', False) or not hasattr(runner, 'state_file') or not runner.state_file.is_file():
        res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': 'boot not attempted or state file missing'}
        _save_diag(res)
        return res

    # Verify frozen state file path if set
    if hasattr(runner, 'verified_state_file'):
        try:
            if runner.state_file.resolve() != runner.verified_state_file:
                res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': f'state file path mismatch: expected {runner.verified_state_file}, got {runner.state_file.resolve()}'}
                _save_diag(res)
                return res
        except Exception as exc:
            res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': f'state file path resolution failure: {exc}'}
            _save_diag(res)
            return res

    # Read and verify current state file content on disk
    try:
        cur_state = load(runner.state_file)
    except Exception as exc:
        res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': f'failed to read current state file: {exc}'}
        _save_diag(res)
        return res

    if not isinstance(cur_state, dict):
        res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': 'state file content is not a dictionary'}
        _save_diag(res)
        return res

    cur_serial = cur_state.get('serial')
    if not cur_serial or not support.valid_emulator_serial(cur_serial):
        res = {'status': 'skipped', 'reason': 'invalid_emulator_serial', 'detail': f'serial invalid in state file: {cur_serial!r}'}
        _save_diag(res)
        return res

    # Check serial match
    if hasattr(runner, 'verified_serial') and runner.verified_serial != cur_serial:
        res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': f'serial mismatch with verified session: expected {runner.verified_serial}, got {cur_serial}'}
        _save_diag(res)
        return res

    # Check PID match
    cur_pid = cur_state.get('pid')
    if hasattr(runner, 'verified_pid') and runner.verified_pid != cur_pid:
        res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': f'PID mismatch with verified session: expected {runner.verified_pid}, got {cur_pid}'}
        _save_diag(res)
        return res

    # Check creation identity match
    cur_created = cur_state.get('pid_created_utc') or cur_state.get('started_utc')
    if hasattr(runner, 'verified_pid_created_utc') and runner.verified_pid_created_utc:
        if cur_created != runner.verified_pid_created_utc:
            res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': f'creation identity mismatch with verified session: expected {runner.verified_pid_created_utc}, got {cur_created}'}
            _save_diag(res)
            return res

    # Check in-memory state dictionary consistency if provided
    if isinstance(state, dict):
        if state.get('serial') != cur_serial or state.get('pid') != cur_pid:
            res = {'status': 'skipped', 'reason': 'instance_identity_unverified', 'detail': 'in-memory state inconsistent with state file on disk'}
            _save_diag(res)
            return res

    serial = cur_serial

    rem_op = runner.remaining_s(reserve_cleanup=True)
    if rem_op < 8.0:
        res = {
            'status': 'skipped',
            'reason': 'insufficient_operational_budget',
            'remaining_operational_s': round(rem_op, 3),
            'required_operational_s': 8.0,
            'detail': 'Zero device commands dispatched to preserve 40s cleanup budget'
        }
        _save_diag(res)
        return res

    cmd_timeout = min(8.0, rem_op)
    t_start_mono = time.monotonic()
    t_start_utc = support.utc_now()
    logcat_cmd = [
        str(runner.adb_exe), '-s', serial, 'logcat', '-d', '-v', 'threadtime', '-t', str(STARTUP_LOGCAT_TAIL_LIMIT)
    ]
    cmd_count_before = len(getattr(runner, 'commands', []))
    raw_stdout_text = ""
    cmd_error = None
    try:
        raw_stdout_text = runner.execute_cmd(
            'startup-logcat-diagnostic', logcat_cmd, timeout=cmd_timeout, check=False, cleanup=False
        )
    except Exception as exc:
        cmd_error = exc

    t_end_mono = time.monotonic()
    t_end_utc = support.utc_now()
    elapsed_s = round(t_end_mono - t_start_mono, 3)

    # Locate the command record newly appended by SingleCaseRunner
    cmd_record = None
    if hasattr(runner, 'commands') and len(runner.commands) > cmd_count_before:
        cand = runner.commands[-1]
        if cand.get('label') == 'startup-logcat-diagnostic' and cand.get('argv') == [str(a) for a in logcat_cmd]:
            cmd_record = cand

    if cmd_record is None:
        res = {
            'status': 'failed',
            'reason': 'missing_command_record',
            'error': f'Command execution did not produce a matching record in runner.commands (cmd_error: {cmd_error!r})',
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    rc = cmd_record.get('exit_code')
    timed_out = cmd_record.get('timed_out', False)

    # Read stderr file from path in cmd_record
    stderr_path = cmd_record.get('stderr')
    raw_stderr_path = cmd_record.get('raw_stderr')
    stderr_text = ""
    if stderr_path and Path(stderr_path).is_file():
        try:
            stderr_text = Path(stderr_path).read_text(encoding='utf-8-sig', errors='replace')
        except Exception as se_err:
            stderr_text = f"<failed to read stderr file {stderr_path}: {se_err}>"
    elif raw_stderr_path and Path(raw_stderr_path).is_file():
        try:
            stderr_text = Path(raw_stderr_path).read_text(encoding='utf-8-sig', errors='replace')
        except Exception:
            pass

    is_timeout = timed_out or (cmd_error is not None and ('timeout' in type(cmd_error).__name__.lower() or 'timeout' in str(cmd_error).lower()))

    # Read raw stdout bytes from cmd_record raw_stdout or stdout file path
    raw_bytes_file = None
    for cand_field in ('raw_stdout', 'stdout'):
        cand_p = cmd_record.get(cand_field)
        if cand_p and Path(cand_p).is_file():
            raw_bytes_file = Path(cand_p)
            break

    if raw_bytes_file is None:
        res = {
            'status': 'failed',
            'reason': 'evidence_incomplete',
            'detail': 'missing raw stdout evidence file in command record',
            'exit_code': rc,
            'timed_out': is_timeout,
            'stderr': stderr_text[:1000],
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    try:
        raw_file_size = raw_bytes_file.stat().st_size
    except Exception as st_err:
        res = {
            'status': 'failed',
            'reason': 'evidence_incomplete',
            'detail': f'failed to stat raw output file {raw_bytes_file}: {st_err}',
            'exit_code': rc,
            'timed_out': is_timeout,
            'stderr': stderr_text[:1000],
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    # R2 Minimal Defense: 10MB is a post-processing threshold preventing memory/decode exhaustion.
    # Check size BEFORE in-memory reading. For oversized output (>10MB), bind existing raw file
    # and file size, skip in-memory decoding and hash computation to strictly preserve the 40s
    # cleanup budget, and explicitly mark evidence_incomplete.
    if raw_file_size > MAX_DIAGNOSTIC_RAW_BYTES:
        res = {
            'status': 'failed',
            'reason': 'output_size_exceeded',
            'detail': f'raw logcat output size ({raw_file_size} bytes) exceeded post-processing limit of {MAX_DIAGNOSTIC_RAW_BYTES} bytes; skipped full in-memory decode and parsing',
            'exit_code': rc,
            'timed_out': is_timeout,
            'stderr': stderr_text[:1000],
            'raw_log_file': str(raw_bytes_file),
            'raw_log_sha256': None,
            'raw_bytes_count': raw_file_size,
            'max_allowed_bytes': MAX_DIAGNOSTIC_RAW_BYTES,
            'evidence_incomplete': True,
            'hash_note': 'Hash computation skipped for oversized output to preserve 40s cleanup budget',
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    raw_bytes = b""
    try:
        raw_bytes = raw_bytes_file.read_bytes()
    except Exception as rb_err:
        res = {
            'status': 'failed',
            'reason': 'evidence_incomplete',
            'detail': f'failed to read raw output file {raw_bytes_file}: {rb_err}',
            'exit_code': rc,
            'timed_out': is_timeout,
            'stderr': stderr_text[:1000],
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    raw_log_file = runner.run_dir / 'startup-logcat-raw.log'
    try:
        raw_log_file.write_bytes(raw_bytes)
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    except Exception as w_err:
        res = {
            'status': 'failed',
            'reason': 'evidence_incomplete',
            'detail': f'failed to write raw log file {raw_log_file}: {w_err}',
            'exit_code': rc,
            'stderr': stderr_text[:1000],
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    if is_timeout:
        res = {
            'status': 'failed',
            'reason': 'command_timeout',
            'exit_code': rc,
            'stderr': stderr_text[:1000] if stderr_text else (str(cmd_error) if cmd_error else 'timed out'),
            'error': repr(cmd_error) if cmd_error else 'Command timed out',
            'raw_log_file': str(raw_log_file),
            'raw_log_sha256': raw_hash,
            'partial_bytes_count': len(raw_bytes),
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    if cmd_error is not None:
        res = {
            'status': 'failed',
            'reason': 'command_execution_error',
            'exit_code': rc,
            'stderr': stderr_text[:1000] or str(cmd_error),
            'error': repr(cmd_error),
            'raw_log_file': str(raw_log_file),
            'raw_log_sha256': raw_hash,
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    if rc is None or not isinstance(rc, int):
        res = {
            'status': 'failed',
            'reason': 'invalid_exit_code',
            'detail': f'command exit code is not an integer: {rc!r}',
            'raw_log_file': str(raw_log_file),
            'raw_log_sha256': raw_hash,
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    if rc != 0:
        res = {
            'status': 'failed',
            'reason': 'command_exit_non_zero',
            'exit_code': rc,
            'stderr': stderr_text[:1000],
            'stderr_file': str(stderr_path) if stderr_path else None,
            'raw_log_file': str(raw_log_file),
            'raw_log_sha256': raw_hash,
            'raw_bytes_count': len(raw_bytes),
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
        }
        _save_diag(res)
        return res

    # Successful collection: decode raw_bytes
    raw_stdout = raw_bytes.decode('utf-8-sig', errors='replace')
    raw_lines = raw_stdout.splitlines()

    if not raw_stdout.strip():
        res = {
            'status': 'failed',
            'reason': 'empty_logcat_output',
            'detail': 'command succeeded with exit code 0 but logcat output was empty',
            'exit_code': 0,
            'stderr': stderr_text[:500],
            'stderr_file': str(stderr_path) if stderr_path else None,
            'raw_log_file': str(raw_log_file),
            'raw_log_sha256': raw_hash,
            'raw_bytes_count': len(raw_bytes),
            'tail_limit': STARTUP_LOGCAT_TAIL_LIMIT,
            'host_start_utc': t_start_utc,
            'host_end_utc': t_end_utc,
            'host_start_monotonic': t_start_mono,
            'host_end_monotonic': t_end_mono,
            'host_elapsed_s': elapsed_s,
            'device_serial': serial,
            'raw_lines_count': 0,
            'filtered_events_count': 0,
            'filtered_events': [],
            'device_start_time': None,
            'device_end_time': None,
            'device_time_span_s': None,
            'absence_note': 'Absence of specific tags or events does not prove non-occurrence (log buffers may roll over, release builds may omit debug tags). Device timestamps and host monotonic times are strictly separate; no cross-clock stitching assumed.',
        }
        _save_diag(res)
        return res

    # Extract device time span across raw log lines
    dev_start_time = None
    dev_end_time = None
    for line in raw_lines:
        line_s = line.strip()
        if not line_s:
            continue
        m_ts = TIMESTAMP_PATTERN.match(line_s)
        if m_ts:
            ts = m_ts.group(1)
            if dev_start_time is None:
                dev_start_time = ts
            dev_end_time = ts

    device_time_span_s = None
    if dev_start_time and dev_end_time:
        try:
            t0 = datetime.strptime("2026-" + dev_start_time.strip(), "%Y-%m-%d %H:%M:%S.%f")
            t1 = datetime.strptime("2026-" + dev_end_time.strip(), "%Y-%m-%d %H:%M:%S.%f")
            device_time_span_s = round((t1 - t0).total_seconds(), 3)
        except Exception:
            pass

    filtered_events = []
    for line in raw_lines:
        line = line.strip()
        if not line:
            continue
        m = DIAG_LINE_PATTERN.match(line)
        if m:
            dev_time, pid, tid, level, tag, msg = m.groups()
            tag_clean = tag.strip()
            if tag_clean in DIAGNOSTIC_TAGS or 'fdroid' in tag_clean.lower() or 'permission' in tag_clean.lower() or 'fdroid' in msg.lower():
                filtered_events.append({
                    'device_time': dev_time,
                    'pid': int(pid),
                    'tid': int(tid),
                    'level': level,
                    'tag': tag_clean,
                    'message': msg[:300]
                })
        elif 'fdroid' in line.lower() or 'permissioncontroller' in line.lower():
            filtered_events.append({
                'raw_line': line[:300]
            })

    truncated = len(filtered_events) > 150
    if len(filtered_events) == 0:
        diag_status = 'partial'
        status_reason = 'zero_target_events_matched'
    elif truncated:
        diag_status = 'partial'
        status_reason = 'filtered_events_truncated'
    else:
        diag_status = 'collected'
        status_reason = None

    res = {
        'status': diag_status,
        'exit_code': 0,
        'stderr': stderr_text[:500],
        'stderr_file': str(stderr_path) if stderr_path else None,
        'raw_log_file': str(raw_log_file),
        'raw_log_sha256': raw_hash,
        'raw_bytes_count': len(raw_bytes),
        'tail_limit': STARTUP_LOGCAT_TAIL_LIMIT,
        'selected_tags': sorted(list(DIAGNOSTIC_TAGS)),
        'host_start_utc': t_start_utc,
        'host_end_utc': t_end_utc,
        'host_start_monotonic': t_start_mono,
        'host_end_monotonic': t_end_mono,
        'host_elapsed_s': elapsed_s,
        'device_serial': serial,
        'device_start_time': dev_start_time,
        'device_end_time': dev_end_time,
        'device_time_span_s': device_time_span_s,
        'raw_lines_count': len(raw_lines),
        'filtered_events_count': len(filtered_events),
        'filtered_events': filtered_events[:150],
        'truncated': truncated,
        'absence_note': 'Absence of specific tags or events does not prove non-occurrence (log buffers may roll over, release builds may omit debug tags). Device timestamps and host monotonic times are strictly separate; no cross-clock stitching assumed.',
    }
    if status_reason:
        res['reason'] = status_reason
    _save_diag(res)
    return res


def handle_session_failure(runner, state, out, current, verdict, active_request, exc, req=None, opt_in=False):
    verdict.update(status='failed', error=repr(exc), error_code=getattr(exc, 'code', type(exc).__name__))
    if active_request is not None:
        save(out / 'responses' / f'{active_request:04d}.json', {'failed': True, 'error': repr(exc),
             'error_code': verdict['error_code'], 'run_dir': str(runner.run_dir)})
    action = req.get('action') if isinstance(req, dict) else None
    try:
        diag = collect_startup_logcat_diagnostic(
            runner, state, out, active_request=active_request, action=action, opt_in=opt_in
        )
        verdict['startup_diagnostic'] = diag
    except Exception as diag_exc:
        verdict['startup_diagnostic'] = {
            'status': 'failed',
            'reason': 'diagnostic_exception',
            'error': repr(diag_exc),
        }


def perform_session_cleanup(runner, state, out, current, verdict, policy=None):
    current['phase'] = 'stopping'
    save(out / 'current-session.json', current)
    runner.cleanup_deadline = min(runner.deadline, time.monotonic()+CLEANUP_S)
    try:
        if runner.boot_attempted and runner.state_file.is_file():
            runner.execute_cmd('stop', [runner.powershell_exe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                str(runner.launcher_script), '-Stop', '-StateFile', str(runner.state_file),
                '-StopTimeoutSec', '25', '-NativeTimeoutSec', '8'], timeout=30, cleanup=True)
        runner.check_preflight_absent('final', settle=True)
        verdict['stopped'] = True
    except Exception as exc:
        verdict.update(status='failed', cleanup_error=repr(exc))
    if runner.state_file.is_file():
        shutil.copyfile(runner.state_file, runner.run_dir / 'state-final.json')
    if runner.session_dir.is_dir():
        shutil.copytree(runner.session_dir, runner.run_dir / 'observations', dirs_exist_ok=True)
    try:
        runner.verify_manifest('manifest-after')
    except Exception as exc:
        verdict.update(status='failed', manifest_error=repr(exc))
    calls = policy.calls if policy else 0
    used = policy.used if policy else 0
    text_succeeded = policy.text_succeeded if policy else False
    independent_readback = policy.independent_readback if policy else False
    refresh_triggered = getattr(policy, 'refresh_clicked', False) if policy else False
    refresh_in_progress = getattr(policy, 'refresh_in_progress', False) if policy else False
    refresh_ui_completed = getattr(policy, 'refresh_ui_completed', False) if policy else False
    network_verified = getattr(policy, 'network_verified', False) if policy else False
    verdict.update(cli_calls=calls, input_calls=used,
                   text_backend_verified=text_succeeded, independent_readback=independent_readback,
                   refresh_triggered=refresh_triggered,
                   refresh_in_progress=refresh_in_progress,
                   refresh_ui_completed=refresh_ui_completed,
                   network_verified=network_verified,
                   unknown_input_state=runner.unknown_input_state, elapsed_s=round(time.monotonic()-runner.start_time, 3),
                   ended_utc=support.utc_now(), run_dir=str(runner.run_dir))
    save(runner.run_dir / 'stepwise-verdict.json', verdict)
    save(out / 'result.json', verdict)
    current['phase'] = 'stopped' if verdict['stopped'] else 'cleanup_failed'
    save(out / 'current-session.json', current)


def serve(out):
    with (out / 'worker-claimed.json').open('x', encoding='utf-8') as handle:
        json.dump({'pid': os.getpid(), 'claimed_utc': support.utc_now()}, handle)
    cfg = load(out / 'config.json')
    mode = cfg.get('mode', 'stepwise_input')
    opt_in = cfg.get('enable_startup_logcat_diagnostic', False)
    low_ram = cfg.get('low_ram', False)
    if type(low_ram) is not bool:
        raise ValueError('low_ram must be a boolean')
    project = Path(cfg['project_root']).resolve()
    manifests = list(map(Path, cfg['manifests']))
    verify_files(project, manifests)
    runner = Session(project_root=project, python_exe=Path(cfg['python']), adb_exe=Path(cfg['adb']),
                     manifest_path=manifests[0], output_base=out, dry_run=False)
    runner.manifests = manifests
    runner.tool_hashes = cfg['tool_hashes']
    runner.deadline = runner.start_time + TOTAL_S
    policy = Policy.from_config(cfg)
    if not policy.controlled_evidence_dir:
        policy.controlled_evidence_dir = str(out.parent)
    current = {'run_dir': str(runner.run_dir), 'state_file': str(runner.state_file),
               'session_dir': str(runner.session_dir), 'phase': 'booting', 'next_seq': 1,
               'mode': mode,
               'max_session_s': TOTAL_S, 'cleanup_reserved_s': CLEANUP_S, 'max_cli_calls': MAX_CALLS}
    save(out / 'current-session.json', current)
    verdict = {'status': 'running', 'goal_verified': False, 'stopped': False, 'decision_mode': 'external_stepwise',
               'mode': mode, 'started_utc': support.utc_now(), 'max_session_s': TOTAL_S}
    active_request = None
    validator = None
    try:
        runner.verify_manifest('manifest-before')
        runner.check_preflight_absent('preflight')
        gate = json.loads(runner.execute_cmd('bundle-check', [str(runner.python_exe), str(project / 'sandbox.py'), 'check'], timeout=20, check=False))
        save(runner.run_dir / 'bundle-check.json', gate)
        if gate.get('ready_for_device_integration') is not True:
            raise ValueError('live project bundle has not passed integration gate')
        runner.boot_attempted = True
        start_cmd = [runner.powershell_exe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                     str(runner.launcher_script), '-StateFile', str(runner.state_file),
                     '-BootTimeoutSec', '120', '-NativeTimeoutSec', '8']
        if low_ram:
            start_cmd.append('-LowRam')
        runner.execute_cmd('start', start_cmd, timeout=140)
        state = load(runner.state_file)
        save(runner.run_dir / 'state-start.json', state)
        if not support.valid_emulator_serial(state.get('serial')):
            raise ValueError('invalid state-derived serial')
        runner.execute_cmd('owned-status', [runner.powershell_exe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                          str(runner.launcher_script), '-Status', '-StateFile', str(runner.state_file),
                                          '-NativeTimeoutSec', '8'], timeout=25)
        runner.owned_status_verified = True
        runner.verified_state_file = runner.state_file.resolve()
        runner.verified_serial = state.get('serial')
        runner.verified_pid = state.get('pid')
        runner.verified_pid_created_utc = state.get('pid_created_utc') or state.get('started_utc')
        runner.verified_avd = state.get('avd')
        runner.startup_stage = True
        spec = importlib.util.spec_from_file_location('stepwise_live_validator', project / 'integration/validate_response.py')
        validator = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(project))
        spec.loader.exec_module(validator)
        current.update(phase='ready', serial=state['serial'], elapsed_s=round(time.monotonic()-runner.start_time, 3))
        policy.expected_device_serial = state['serial']
        save(out / 'current-session.json', current)
        while runner.remaining_s() > 0:
            seq = current['next_seq']
            request_path = out / 'requests' / f'{seq:04d}.json'
            if not request_path.is_file():
                time.sleep(0.15)
                continue
            active_request = seq
            req = load(request_path)
            if req.get('seq') != seq:
                raise ValueError('request sequence mismatch')
            if req.get('action') != 'launch':
                runner.startup_stage = False
            if req.get('evidence_sha256') != current.get('last_response_sha256'):
                raise ValueError('decision is not bound to the latest complete response')
            min_click_budget = (support.CLICK_BUDGET_S + support.OBSERVE_BUDGET_S) if policy.mode == 'application_refresh' else (support.CLICK_BUDGET_S + support.SET_TEXT_MIN_REMAINING_S)
            if req.get('action') == 'click' and runner.remaining_s() < min_click_budget:
                raise TimeoutError('insufficient budget for click, operation and reserved cleanup')
            args = policy.prepare(req)
            save(runner.run_dir / f'decision-{seq:04d}.json', req)
            if req['action'] == 'finish':
                verdict.update(status='completed' if policy.goal_verified else 'incomplete',
                               goal_verified=policy.goal_verified,
                               refresh_ui_completed=getattr(policy, 'refresh_ui_completed', False),
                               network_verified=getattr(policy, 'network_verified', False),
                               finish_reason=req['reason'])
                save(out / 'responses' / f'{seq:04d}.json', {
                    'finished': True,
                    'goal_verified': policy.goal_verified,
                    'refresh_ui_completed': getattr(policy, 'refresh_ui_completed', False),
                    'network_verified': getattr(policy, 'network_verified', False)
                })
                active_request = None
                break
            runner.verify_manifest(f'manifest-before-action-{seq:04d}')
            label = f'agent-{seq:04d}-{req["action"]}'
            payload, checked, recovery = runner.formal_action(label, req, args, policy, validator, state['serial'])
            response = {'seq': seq, 'payload': payload, 'validation': checked,
                        'goal_verified': policy.goal_verified,
                        'elapsed_s': round(time.monotonic()-runner.start_time, 3),
                        'remaining_operational_s': round(runner.remaining_s(), 3)}
            if recovery is not None:
                response['recovery'] = recovery
            response_path = out / 'responses' / f'{seq:04d}.json'
            save(response_path, response)
            current.update(next_seq=seq+1, last_response_sha256=digest(response_path), elapsed_s=response['elapsed_s'],
                           remaining_operational_s=response['remaining_operational_s'], goal_verified=policy.goal_verified)
            save(out / 'current-session.json', current)
            active_request = None
        else:
            raise TimeoutError('fixed operational deadline reached while waiting for agent')
    except Exception as exc:
        handle_session_failure(
            runner, state if 'state' in locals() else None, out, current, verdict,
            active_request, exc, req=req if 'req' in locals() else None, opt_in=opt_in
        )
    finally:
        perform_session_cleanup(
            runner, state if 'state' in locals() else None, out, current, verdict,
            policy if 'policy' in locals() else None
        )
    return 0 if verdict['status'] == 'completed' and verdict['stopped'] else 1


def begin(args):
    if os.name != 'nt':
        raise ValueError('live lifecycle is Windows PowerShell 5.1 only')
    mode = getattr(args, 'mode', 'stepwise_input')
    low_ram = getattr(args, 'low_ram', False)
    if type(low_ram) is not bool:
        raise ValueError('low_ram must be a boolean')
    project, out = Path(args.project_root).resolve(), Path(args.out).resolve()
    manifests = [str(Path(p).resolve()) for p in args.manifest]
    count = verify_files(project, manifests)
    out.mkdir(parents=True, exist_ok=True)
    if mode == 'application_refresh':
        claim = {'max_session_s': TOTAL_S, 'cleanup_reserved_s': CLEANUP_S, 'max_cli_calls': MAX_CALLS,
                 'max_launches': 1, 'max_clicks': 5, 'max_set_text': 0, 'max_observes': 3, 'max_refresh_observes': 2,
                 'mode': mode, 'source_files': count, 'decision_mode': 'external_stepwise', 'no_input_replay': True}
    else:
        claim = {'max_session_s': TOTAL_S, 'cleanup_reserved_s': CLEANUP_S, 'max_cli_calls': MAX_CALLS,
                 'max_launches': 1, 'max_clicks': 1, 'max_set_text': 1, 'value': 'Aurora', 'mode': mode,
                 'source_files': count, 'decision_mode': 'external_stepwise', 'no_input_replay': True}
    with (out / 'device-session-claimed.json').open('x', encoding='utf-8') as handle:
        json.dump(claim, handle, indent=2)
    (out / 'requests').mkdir()
    (out / 'responses').mkdir()
    paths = [Path(__file__).resolve(), Path(support.__file__).resolve()]
    cfg = {'project_root': str(project), 'python': str(Path(sys.executable).resolve()), 'adb': str(Path(args.adb).resolve()),
           'manifests': manifests, 'mode': mode, 'session_id': getattr(args, 'session_id', None),
           'enable_startup_logcat_diagnostic': getattr(args, 'enable_startup_logcat_diagnostic', False),
           'low_ram': low_ram,
           'controlled_evidence_dir': str(out.parent),
           'tool_hashes': {str(p): digest(p) for p in paths}}
    save(out / 'config.json', cfg)
    with (out / 'worker.stdout').open('xb') as stdout, (out / 'worker.stderr').open('xb') as stderr:
        proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'serve', '--out', str(out)],
            stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, close_fds=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONDONTWRITEBYTECODE': '1'})
    save(out / 'worker.json', {'pid': proc.pid, 'started_utc': support.utc_now(), 'out': str(out)})
    return {'worker_pid': proc.pid, 'out': str(out), 'max_session_s': TOTAL_S, 'phase': 'starting'}


def submit(args):
    out = Path(args.out).resolve()
    state = load(out / 'current-session.json')
    if state['phase'] != 'ready':
        raise ValueError('session is not accepting actions: ' + state['phase'])
    seq = state['next_seq']
    req = {k: getattr(args, k) for k in ['action', 'reason', 'observation_id', 'node_id', 'value']}
    net_val = getattr(args, 'network_check', None)
    if net_val:
        if isinstance(net_val, str) and Path(net_val).is_file():
            req['network_check'] = load(net_val)
        elif isinstance(net_val, str):
            try:
                req['network_check'] = json.loads(net_val)
            except Exception:
                req['network_check'] = net_val
        else:
            req['network_check'] = net_val
    req.update(seq=seq, decision_source='external_model', submitted_utc=support.utc_now(),
               evidence_sha256=state.get('last_response_sha256'))
    # Exclusive claim protects against two concurrent clients choosing the same sequence.
    with (out / 'requests' / f'{seq:04d}.claim').open('x'):
        pass
    save(out / 'requests' / f'{seq:04d}.json', req)
    response_path = out / 'responses' / f'{seq:04d}.json'
    deadline = time.monotonic()+100
    while time.monotonic() < deadline:
        if response_path.is_file():
            # The worker publishes the response before its hash/next sequence.
            # Wait for that commit boundary before allowing another client call.
            committed = load(out / 'current-session.json')
            if committed['phase'] == 'ready' and committed['next_seq'] == seq:
                time.sleep(0.05)
                continue
            result = load(response_path)
            return {**{k: v for k, v in result.items() if k != 'payload'},
                    'response_file': str(response_path),
                    'summary': summary(result['payload']) if 'payload' in result else None}
        if (out / 'result.json').is_file():
            return {'session_ended': load(out / 'result.json')}
        time.sleep(0.15)
    raise TimeoutError('client wait exhausted; worker owns deadline and cleanup, do not replay')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    start = sub.add_parser('begin')
    start.add_argument('--project-root', required=True)
    start.add_argument('--adb', required=True)
    start.add_argument('--manifest', action='append', required=True)
    start.add_argument('--out', required=True)
    start.add_argument('--mode', choices=['stepwise_input', 'application_refresh'], default='stepwise_input')
    start.add_argument('--session-id', default=None)
    start.add_argument('--enable-startup-logcat-diagnostic', action='store_true', default=False)
    start.add_argument('--low-ram', action='store_true', default=False,
                       help='Explicitly enable the experimental 1536 MiB low-RAM launcher mode; default is off')
    worker = sub.add_parser('serve')
    worker.add_argument('--out', required=True)
    action = sub.add_parser('submit')
    action.add_argument('--out', required=True)
    action.add_argument('--action', required=True, choices=['status', 'launch', 'observe', 'click', 'set-text', 'finish'])
    action.add_argument('--reason', required=True)
    action.add_argument('--observation-id', default=None)
    action.add_argument('--node-id', default=None)
    action.add_argument('--value', default=None)
    action.add_argument('--network-check', default=None)
    args = parser.parse_args(argv)
    if args.command == 'serve':
        return serve(Path(args.out).resolve())
    print(json.dumps(begin(args) if args.command == 'begin' else submit(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
