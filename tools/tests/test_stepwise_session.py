"""Offline policy tests; importing these modules never starts a worker/device."""
from pathlib import Path
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import stepwise_session as m

OID = 'a' * 64
NODE = {'node_id': 'aaaaaaaaaaaa:R.1', 'path': 'R.1', 'package': m.PACKAGE,
        'class': 'android.widget.EditText', 'resource_id': None, 'text': '', 'enabled': True,
        'bounds_valid': True, 'bounds': [112, 56, 608, 168], 'password': False, 'focused': False}

PERM_OID = '282cd9cda4dd3f5f9e2b14bf8a3b2fc031ad5cf34c8d3283a6cf396ff97f57db'
PERM_NODE = {
    'node_id': f'{PERM_OID[:12]}:R',
    'parent_id': None,
    'path': 'R',
    'package': 'com.google.android.permissioncontroller',
    'class': 'android.widget.FrameLayout',
    'resource_id': None,
    'text': None,
    'content_desc': None,
    'bounds': [18, 640, 702, 640],
    'bounds_valid': False,
    'enabled': True,
    'clickable': False,
    'focused': False,
    'password': False,
}


def req(action, **kw):
    return dict(action=action, reason='Choose the unique field in the current observation.', **kw)


def ready():
    p = m.Policy()
    p.latest = {'observation_id': OID, 'nodes': [copy.deepcopy(NODE)]}
    return p


def saved_task91_response():
    """Locate the saved task91 live response wherever this tree is checked out.

    Override with TASK91_RESPONSE=<path to 0001.json>. The file is replayed as-is; it is
    never reconstructed as a fixture, because a rebuilt sample would not prove anything
    about the real parsed node shape.
    """
    candidates = []
    if os.environ.get('TASK91_RESPONSE'):
        candidates.append(Path(os.environ['TASK91_RESPONSE']))
    here = Path(__file__).resolve()
    for root in here.parents:
        candidates.append(root / 'task91-next-model' / 'session' / 'responses' / '0001.json')
        candidates.append(root / 'intake' / 'task91-next-model' / 'session' / 'responses' / '0001.json')
        candidates.append(root / 'round5-memory' / 'intake' / 'task91-next-model' / 'session' / 'responses' / '0001.json')
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise AssertionError('saved task91 response not found; tried: ' + repr([str(c) for c in candidates[:8]]))


class PolicyTests(unittest.TestCase):
    def test_only_formal_allowlisted_actions(self):
        for action in ['tap', 'shell', 'back', 'install', 'rm', 'launch; whoami']:
            with self.subTest(action=action), self.assertRaises(ValueError):
                ready().prepare(req(action))

    def test_full_latest_hash_and_exact_node_required(self):
        for oid, nid in [(OID[:12], NODE['node_id']), ('b'*64, NODE['node_id']), (OID, 'foreign')]:
            with self.subTest(oid=oid, nid=nid), self.assertRaises(ValueError):
                ready().prepare(req('click', observation_id=oid, node_id=nid))

    def test_only_one_input_claim_even_if_call_never_returns(self):
        p = ready()
        request = req('set-text', observation_id=OID, node_id=NODE['node_id'], value='Aurora')
        self.assertEqual(p.prepare(request)[-2:], ['--value', 'Aurora'])
        with self.assertRaises(ValueError):
            p.prepare(request)

    def test_initial_empty_unique_unmasked_field(self):
        for mutation in ['nonempty', 'duplicate', 'password', 'wrong_package']:
            p = ready()
            if mutation == 'nonempty': p.latest['nodes'][0]['text'] = 'old'
            if mutation == 'duplicate': p.latest['nodes'].append(copy.deepcopy(NODE))
            if mutation == 'password': p.latest['nodes'][0]['password'] = True
            if mutation == 'wrong_package': p.latest['nodes'][0]['package'] = 'other'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                p.prepare(req('set-text', observation_id=OID, node_id=NODE['node_id'], value='Aurora'))

    def test_no_arbitrary_text_or_package_argument(self):
        with self.assertRaises(ValueError):
            ready().prepare(req('set-text', observation_id=OID, node_id=NODE['node_id'], value='secret'))
        p = ready()
        self.assertEqual(p.prepare(req('launch', package='foreign')), ['--package', m.PACKAGE])
        with self.assertRaises(ValueError): p.prepare(req('launch'))

    def test_no_false_goal_from_backend_only(self):
        p = ready()
        p.prepare(req('set-text', observation_id=OID, node_id=NODE['node_id'], value='Aurora'))
        node = copy.deepcopy(NODE); node.update(text='Aurora', focused=True)
        p.accept('set-text', {'ok': True, 'data': {'verified': True, 'readback_text': 'Aurora',
                 'after_observation': {'observation_id': 'b'*64, 'nodes': [node]}}})
        self.assertTrue(p.text_succeeded)
        self.assertFalse(p.independent_readback)
        node['bounds'][2] = 704
        p.accept('observe', {'ok': True, 'data': {'observation_id': 'c'*64, 'nodes': [node]}})
        self.assertTrue(p.independent_readback)

    def test_wrong_focus_or_anchor_rejects_independent_read(self):
        for mutation in ['focus', 'left', 'resource']:
            p = ready(); p.target = copy.deepcopy(NODE); p.text_succeeded = True
            n = copy.deepcopy(NODE); n.update(text='Aurora', focused=True)
            if mutation == 'focus': n['focused'] = False
            if mutation == 'left': n['bounds'][0] += 1
            if mutation == 'resource': n['resource_id'] = 'other'
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                p.accept('observe', {'ok': True, 'data': {'observation_id': OID, 'nodes': [n]}})

    def test_fixed_cli_limit_and_reason_required(self):
        p = ready(); p.calls = m.MAX_CALLS
        with self.assertRaises(ValueError): p.prepare(req('observe'))
        with self.assertRaises(ValueError): ready().prepare({'action': 'click', 'reason': ''})
        self.assertEqual(p.prepare(req('finish')), [])

    def test_manifest_drift_and_path_escape(self):
        private = Path(__file__).resolve().parent / '_tmp'
        private.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private) as temp:
            root = Path(temp); project = root / 'project'; project.mkdir()
            source = project / 'x.py'; source.write_text('pass\n')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'files': {'x.py': m.digest(source)}}))
            self.assertEqual(m.verify_files(project, [manifest]), 1)
            source.write_text('changed\n')
            with self.assertRaises(ValueError): m.verify_files(project, [manifest])
            manifest.write_text(json.dumps({'files': {'../outside': 'a'*64}}))
            with self.assertRaises(ValueError): m.verify_files(project, [manifest])

    def test_support_contains_no_automatic_run(self):
        self.assertFalse(hasattr(m.support.SingleCaseRunner, 'run'))
        self.assertFalse(hasattr(m.support, 'main'))
        self.assertEqual(m.TOTAL_S, 360)
        self.assertEqual(m.CLEANUP_S, 40)

    def test_worker_deadline_stops_owned_instance_without_agent_or_input(self):
        private = Path(__file__).resolve().parent / '_tmp'
        private.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private) as temp:
            root = Path(temp); project = root / 'project'; out = root / 'out'
            (project / 'integration').mkdir(parents=True); out.mkdir()
            (out / 'requests').mkdir(); (out / 'responses').mkdir()
            source = project / 'integration/validate_response.py'
            source.write_text('def validate(*args, **kwargs): return {"protocol_valid": True}\n')
            manifest = root / 'manifest.json'
            manifest.write_text(json.dumps({'files': {'integration/validate_response.py': m.digest(source)}}))
            m.save(out / 'config.json', {'project_root': str(project), 'manifests': [str(manifest)],
                   'python': sys.executable, 'adb': str(root / 'fake-adb'), 'tool_hashes': {}})
            clock = [0.0]
            calls = []

            class FakeSession:
                def __init__(self, **kwargs):
                    self.project_root = project
                    self.run_dir = out / 'fake-run'; self.run_dir.mkdir()
                    self.state_file = self.run_dir / 'state.json'
                    self.session_dir = self.run_dir / 'runtime'
                    self.python_exe = Path(sys.executable)
                    self.adb_exe = root / 'fake-adb'
                    self.powershell_exe = 'fake-powershell'
                    self.launcher_script = root / 'fake-launcher'
                    self.start_time = 0.0; self.deadline = m.TOTAL_S
                    self.boot_attempted = False; self.unknown_input_state = False

                def verify_manifest(self, label): calls.append(label)
                def check_preflight_absent(self, label, **kwargs): calls.append(label)
                def remaining_s(self): return self.deadline-clock[0]-m.CLEANUP_S
                def execute_cmd(self, label, argv, **kwargs):
                    calls.append(label)
                    if label == 'bundle-check': return '{"ready_for_device_integration": true}'
                    if label == 'start': m.save(self.state_file, {'serial': 'emulator-5554'})
                    return '{}'
                def call_cli(self, *args, **kwargs):
                    raise AssertionError('worker must not choose UI actions on behalf of agent')

            def advance(_): clock[0] = 321.0

            with patch.object(m, 'Session', FakeSession), patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(m.time, 'sleep', side_effect=advance), \
                 patch.object(m.subprocess, 'run', side_effect=AssertionError('no real process')):
                self.assertEqual(m.serve(out), 1)
            result = m.load(out / 'result.json')
            self.assertTrue(result['stopped'])
            self.assertEqual(result['cli_calls'], 0)
            self.assertEqual(result['error_code'], 'TimeoutError')
            self.assertIn('stop', calls)
            self.assertIn('final', calls)
            self.assertFalse(result['goal_verified'])
            with self.assertRaises(FileExistsError):
                m.serve(out)  # A second worker cannot consume the same session claim.

    def test_summary_surfaces_permissioncontroller_zero_height_non_target_observation(self):
        launch_payload = {
            'ok': True,
            'action': 'launch',
            'data': {
                'package': m.PACKAGE,
                'after_observation': {
                    'observation_id': PERM_OID,
                    'nodes': [copy.deepcopy(PERM_NODE)],
                    'device_serial': 'emulator-5554',
                    'avd': 'ai-sandbox-api35-google',
                    'profile_id': m.PROFILE,
                    'coverage': 'partial',
                },
            },
            'error': None,
            'artifacts': ['hierarchy.xml'],
        }
        s = m.summary(launch_payload)
        self.assertTrue(s['ok'])
        self.assertEqual(s['observation_id'], PERM_OID)
        self.assertEqual(s['package'], 'com.google.android.permissioncontroller')
        self.assertEqual(s['observed_packages'], ['com.google.android.permissioncontroller'])
        self.assertEqual(s['target_package'], m.PACKAGE)
        self.assertFalse(s['target_package_ready'])
        self.assertEqual(s['target_readiness_reason'], "foreign_package: ['com.google.android.permissioncontroller']")
        self.assertEqual(s['nodes'], [])
        self.assertEqual(s['total_nodes'], 1)
        self.assertEqual(s['root_node_preview']['bounds'], [18, 640, 702, 640])
        self.assertFalse(s['root_node_preview']['bounds_valid'])

    def test_summary_reports_ready_true_when_fdroid_search_surface_present(self):
        home_oid = 'd' * 64
        fdroid_nodes = [
            {'node_id': f'{home_oid[:12]}:R.0', 'path': 'R.0', 'package': m.PACKAGE,
             'class': 'android.widget.FrameLayout', 'resource_id': None, 'text': None,
             'content_desc': None, 'bounds': [0, 0, 720, 1280], 'bounds_valid': True,
             'enabled': True, 'clickable': False, 'focused': False, 'password': False},
            {'node_id': f'{home_oid[:12]}:R.0.1', 'path': 'R.0.1', 'package': m.PACKAGE,
             'class': 'android.view.ViewGroup', 'resource_id': None, 'text': None,
             'content_desc': None, 'bounds': [240, 1100, 480, 1200], 'bounds_valid': True,
             'enabled': True, 'clickable': True, 'focused': False, 'password': False},
            {'node_id': f'{home_oid[:12]}:R.0.1.0', 'path': 'R.0.1.0', 'package': m.PACKAGE,
             'class': 'android.widget.TextView', 'resource_id': None, 'text': 'Search',
             'content_desc': None, 'bounds': [300, 1120, 420, 1180], 'bounds_valid': True,
             'enabled': True, 'clickable': False, 'focused': False, 'password': False},
        ]
        obs_payload = {
            'ok': True,
            'action': 'observe',
            'data': {'observation_id': home_oid, 'nodes': fdroid_nodes},
            'error': None,
        }
        s = m.summary(obs_payload)
        self.assertEqual(s['package'], m.PACKAGE)
        self.assertEqual(s['observed_packages'], [m.PACKAGE])
        self.assertTrue(s['target_package_ready'])
        self.assertEqual(s['target_readiness_reason'], 'fdroid_interactive_surface_ready')
        self.assertEqual(len(s['nodes']), 1)
        self.assertEqual(s['nodes'][0]['text'], 'Search')

    def test_summary_replays_actual_task91_response_with_hierarchy_wrapper(self):
        # Saved live evidence, no device access and no reconstruction as a fixture.
        response = saved_task91_response()
        payload = json.loads(response.read_text(encoding='utf-8'))['payload']
        s = m.summary(payload)
        self.assertEqual(s['total_nodes'], 2)
        self.assertEqual(s['nodes'], [])
        self.assertEqual(s['package'], 'com.google.android.permissioncontroller')
        self.assertFalse(s['target_package_ready'])
        preview = s['root_node_preview']
        self.assertEqual(preview['path'], 'R.0')
        self.assertEqual(preview['class'], 'android.widget.FrameLayout')
        self.assertIsNone(preview['bounds'])
        self.assertEqual(preview['bounds_raw'], '[18,640][702,640]')
        self.assertFalse(preview['bounds_valid'])

    def test_summary_does_not_treat_declared_package_as_observed_package(self):
        obs = {'observation_id': PERM_OID, 'package': m.PACKAGE,
               'nodes': [copy.deepcopy(PERM_NODE)]}
        s = m.summary({'ok': True, 'action': 'observe', 'data': obs})
        self.assertEqual(s['package'], PERM_NODE['package'])
        obs['nodes'].append(copy.deepcopy(NODE))
        s = m.summary({'ok': True, 'action': 'observe', 'data': obs})
        self.assertIsNone(s['package'])
        self.assertEqual(set(s['observed_packages']), {m.PACKAGE, PERM_NODE['package']})

    def test_non_target_launch_observation_can_advance_via_readonly_observe_without_relaunch(self):
        p = m.Policy()
        # 1. Initial launch returns ok=True but after_observation is permissioncontroller
        self.assertEqual(p.prepare(req('launch')), ['--package', m.PACKAGE])
        p.accept('launch', {
            'ok': True,
            'action': 'launch',
            'data': {
                'package': m.PACKAGE,
                'after_observation': {'observation_id': PERM_OID, 'nodes': [copy.deepcopy(PERM_NODE)]},
            },
        })
        # Re-launching or clicking permissioncontroller node is strictly rejected
        with self.assertRaises(ValueError):
            p.prepare(req('launch'))
        with self.assertRaises(ValueError):
            p.prepare(req('click', observation_id=PERM_OID, node_id=PERM_NODE['node_id']))

        # 2. Read-only observe advances to F-Droid home with Search label
        self.assertEqual(p.prepare(req('observe')), [])
        home_oid = 'e' * 64
        search_node = {
            'node_id': f'{home_oid[:12]}:R.0.1.0', 'path': 'R.0.1.0', 'package': m.PACKAGE,
            'class': 'android.widget.TextView', 'resource_id': None, 'text': 'Search',
            'bounds': [300, 1120, 420, 1180], 'bounds_valid': True, 'enabled': True,
            'clickable': False, 'focused': False, 'password': False,
        }
        p.accept('observe', {
            'ok': True,
            'action': 'observe',
            'data': {'observation_id': home_oid, 'nodes': [search_node]},
        })

        # 3. Click Search label -> transitions to unique empty EditText
        self.assertEqual(
            p.prepare(req('click', observation_id=home_oid, node_id=search_node['node_id'])),
            ['--observation-id', home_oid, '--node-id', search_node['node_id']],
        )
        p.accept('click', {
            'ok': True,
            'action': 'click',
            'data': {'after_observation': {'observation_id': OID, 'nodes': [copy.deepcopy(NODE)]}},
        })

        # 4. Set-text Aurora -> followed by independent observe readback
        self.assertEqual(
            p.prepare(req('set-text', observation_id=OID, node_id=NODE['node_id'], value='Aurora')),
            ['--observation-id', OID, '--node-id', NODE['node_id'], '--value', 'Aurora'],
        )
        typed_node = copy.deepcopy(NODE)
        typed_node.update(text='Aurora', focused=True, bounds=[112, 56, 704, 168])
        p.accept('set-text', {
            'ok': True,
            'action': 'set-text',
            'data': {
                'verified': True,
                'readback_text': 'Aurora',
                'after_observation': {'observation_id': 'b' * 64, 'nodes': [typed_node]},
            },
        })
        self.assertFalse(p.independent_readback)
        self.assertEqual(p.prepare(req('observe')), [])
        p.accept('observe', {
            'ok': True,
            'action': 'observe',
            'data': {'observation_id': 'c' * 64, 'nodes': [typed_node]},
        })
        self.assertTrue(p.independent_readback)


WRAPPER = {'node_id': f'{OID[:12]}:R', 'parent_id': None, 'path': 'R', 'package': None,
           'class': None, 'resource_id': None, 'text': None, 'content_desc': None,
           'bounds': None, 'bounds_raw': None, 'bounds_valid': False,
           'enabled': None, 'clickable': None, 'focused': None, 'password': None}


def nd(path, package=m.PACKAGE, cls='android.widget.TextView', text=None, bounds=(0, 0, 100, 100),
       valid=True, enabled=True, password=False, desc=None):
    """A node shaped exactly like the parser's projection (bounds=None when degenerate)."""
    return {'node_id': f'{OID[:12]}:{path}', 'parent_id': None, 'path': path, 'package': package,
            'class': cls, 'resource_id': None, 'text': text, 'content_desc': desc,
            'bounds': list(bounds) if valid else None,
            'bounds_raw': '[%d,%d][%d,%d]' % tuple(bounds),
            'bounds_valid': valid, 'enabled': enabled, 'clickable': False,
            'focused': False, 'password': password}


def fdroid_shell():
    """Target window with rendered containers but no search/edit surface yet."""
    return [copy.deepcopy(WRAPPER),
            nd('R.0', cls='android.widget.FrameLayout'),
            nd('R.0.0', cls='android.view.ViewGroup')]


def fdroid_home():
    """Rendered target window: wrapper + two containers + a Search label."""
    return fdroid_shell() + [nd('R.0.0.0', text='Search', bounds=(300, 1120, 420, 1180))]


def observe(obs):
    return m.summary({'ok': True, 'action': 'observe', 'data': obs})


class SummaryCoverageTests(unittest.TestCase):
    """Counterexamples for摘要失真: empty/mixed packages, unrendered surfaces, junk nodes."""

    def test_package_less_window_is_previewed_not_hidden(self):
        # A real window may parse with package=null on every node. The old code showed
        # root_node_preview=None and claimed 'foreign_package: none'.
        obs = {'observation_id': OID, 'nodes': [
            copy.deepcopy(WRAPPER),
            nd('R.0', package=None, text='Download', bounds=(0, 300, 720, 420))]}
        s = observe(obs)
        self.assertIsNone(s['package'])
        self.assertEqual(s['observed_packages'], [])
        self.assertFalse(s['target_package_ready'])
        self.assertEqual(s['target_readiness_reason'], 'no_package_in_observation')
        self.assertNotIn('foreign_package', s['target_readiness_reason'])
        self.assertEqual(len(s['nodes']), 1)
        self.assertEqual(s['root_node_preview']['path'], 'R.0')
        self.assertEqual(s['root_node_preview']['text'], 'Download')
        self.assertEqual(s['root_node_preview']['bounds_raw'], '[0,300][720,420]')
        self.assertEqual(s['malformed_nodes'], 0)

    def test_wrapper_only_observation_is_reported_honestly(self):
        s = observe({'observation_id': OID, 'nodes': [copy.deepcopy(WRAPPER)]})
        self.assertEqual(s['total_nodes'], 1)
        self.assertEqual(s['nodes'], [])
        self.assertIsNone(s['package'])
        self.assertEqual(s['observed_packages'], [])
        self.assertFalse(s['target_package_ready'])
        self.assertEqual(s['target_readiness_reason'], 'no_package_in_observation')
        # The bare wrapper is still shown, so a caller sees the dump had no content.
        self.assertEqual(s['root_node_preview']['path'], 'R')
        self.assertIsNone(s['root_node_preview']['bounds'])

    def test_zero_height_surface_is_not_reported_ready_and_still_blocks_input(self):
        cases = {
            'edit': dict(cls='android.widget.EditText', text='', bounds=(0, 640, 720, 640)),
            'search': dict(text='Search', bounds=(0, 640, 720, 640)),
        }
        for kind, kw in cases.items():
            with self.subTest(kind=kind):
                obs = {'observation_id': OID, 'nodes': fdroid_shell() + [nd('R.0.1', valid=False, **kw)]}
                s = observe(obs)
                self.assertFalse(s['target_package_ready'])
                self.assertEqual(s['target_readiness_reason'], 'interactive_surface_not_rendered')
                surface = [n for n in s['nodes'] if n['path'] == 'R.0.1'][0]
                self.assertIsNone(surface['bounds'])
                self.assertFalse(surface['bounds_valid'])
                self.assertEqual(surface['bounds_raw'], '[0,640][720,640]')
                p = m.Policy(); p.latest = obs
                with self.assertRaises(ValueError):
                    p.prepare(req('click', observation_id=OID, node_id=f'{OID[:12]}:R.0.1'))
                with self.assertRaises(ValueError):
                    p.prepare(req('set-text', observation_id=OID, node_id=f'{OID[:12]}:R.0.1', value='Aurora'))

    def test_zero_height_target_window_is_not_ready(self):
        obs = {'observation_id': OID, 'nodes': [
            copy.deepcopy(WRAPPER),
            nd('R.0', cls='android.widget.FrameLayout', bounds=(18, 640, 702, 640), valid=False),
            nd('R.0.0', cls='android.view.ViewGroup', bounds=(18, 640, 702, 640), valid=False),
            nd('R.0.0.0', text='Search', bounds=(18, 640, 702, 640), valid=False)]}
        s = observe(obs)
        self.assertFalse(s['target_package_ready'])
        self.assertEqual(s['target_readiness_reason'], 'no_valid_bounds_in_package')
        self.assertEqual(s['package'], m.PACKAGE)  # package still reported, readiness is not
        self.assertFalse(s['root_node_preview']['bounds_valid'])

    def test_mixed_package_observation_never_names_a_single_package(self):
        obs = {'observation_id': OID, 'nodes': fdroid_home() + [
            nd('R.1', package='com.google.android.permissioncontroller',
               cls='android.widget.FrameLayout', bounds=(0, 640, 720, 640), valid=False)]}
        s = observe(obs)
        self.assertIsNone(s['package'])
        self.assertEqual(s['observed_packages'], ['com.google.android.permissioncontroller', m.PACKAGE])
        # Readiness stays a claim about the target surface only; the mixed package list is
        # reported next to it so the caller can see the foreign window.
        p = m.Policy(); p.latest = obs
        with self.assertRaises(ValueError):
            p.prepare(req('click', observation_id=OID, node_id=f'{OID[:12]}:R.1'))

    def test_readiness_metadata_cannot_bypass_input_protection(self):
        obs = {'observation_id': OID, 'nodes': fdroid_home() + [
            nd('R.0.1', cls='android.widget.EditText', text='', bounds=(0, 640, 720, 640), valid=False)]}
        s = observe(obs)
        self.assertTrue(s['target_package_ready'])  # Search surface is rendered
        p = m.Policy(); p.latest = obs

        with self.assertRaises(ValueError):  # unrendered field, regardless of readiness
            p.prepare(req('click', observation_id=OID, node_id=f'{OID[:12]}:R.0.1'))
        with self.assertRaises(ValueError):  # readiness allows no arbitrary text
            p.prepare(req('set-text', observation_id=OID, node_id=f'{OID[:12]}:R.0.1', value='secret'))
        with self.assertRaises(ValueError):  # readiness does not accept a repaired hash
            p.prepare(req('click', observation_id=OID[:12], node_id=f'{OID[:12]}:R.0.0.0'))
        self.assertEqual(p.calls, 0)  # rejected decisions consume no call budget

    def test_malformed_nodes_do_not_crash_summary(self):
        obs = {'observation_id': OID, 'nodes': [nd('R.0'), None, 'oops', 7]}
        s = observe(obs)
        self.assertEqual(s['malformed_nodes'], 3)
        self.assertEqual(s['total_nodes'], 4)
        self.assertFalse(s['target_package_ready'])
        self.assertEqual(s['target_readiness_reason'], 'malformed_nodes')
        self.assertEqual(s['root_node_preview']['path'], 'R.0')
        self.assertEqual(m.support.is_fdroid_ready(obs), (False, 'malformed_nodes'))

    def test_password_text_is_redacted_from_summary_only(self):
        obs = {'observation_id': OID, 'nodes': fdroid_home() + [
            nd('R.0.1', cls='android.widget.EditText', text='hunter2', password=True)]}
        s = observe(obs)
        surface = [n for n in s['nodes'] if n['path'] == 'R.0.1'][0]
        self.assertIsNone(surface['text'])
        self.assertTrue(surface['password'])
        self.assertEqual(obs['nodes'][-1]['text'], 'hunter2')  # observation itself untouched

    def test_readiness_reasons_distinguish_absent_from_unrendered_surface(self):
        absent = {'observation_id': OID, 'nodes': [copy.deepcopy(WRAPPER),
                  nd('R.0', cls='android.widget.FrameLayout'), nd('R.0.0', cls='android.view.ViewGroup'),
                  nd('R.0.0.0', text='Browse', bounds=(10, 200, 200, 260))]}
        self.assertEqual(m.support.is_fdroid_ready(absent),
                         (False, 'fdroid_nodes_present_but_no_search_or_edit_surface'))
        self.assertEqual(m.support.is_fdroid_ready({'observation_id': OID, 'nodes': [copy.deepcopy(WRAPPER)]}),
                         (False, 'no_package_in_observation'))
        self.assertEqual(m.support.is_fdroid_ready({'observation_id': OID, 'nodes': [copy.deepcopy(PERM_NODE)]}),
                         (False, "foreign_package: ['com.google.android.permissioncontroller']"))
        self.assertEqual(m.support.is_fdroid_ready({'observation_id': OID, 'nodes': []}), (False, 'empty_nodes'))


class ApplicationRefreshPolicyTests(unittest.TestCase):
    """Offline policy tests for application_refresh mode."""

    def setUp(self):
        self.oid1 = '1' * 64
        self.oid2 = '2' * 64
        self.oid3 = '3' * 64
        self.oid4 = '4' * 64
        self.node_repo = {
            'node_id': f'{self.oid1[:12]}:R.repo', 'path': 'R.repo', 'package': m.PACKAGE,
            'class': 'android.view.View', 'content_desc': 'Repositories',
            'enabled': True, 'bounds_valid': True, 'bounds': [424, 64, 520, 160], 'password': False
        }
        self.node_fdroid = {
            'node_id': f'{self.oid2[:12]}:R.fdroid', 'path': 'R.fdroid', 'package': m.PACKAGE,
            'class': 'android.view.View', 'text': 'F-Droid',
            'enabled': True, 'bounds_valid': True, 'bounds': [0, 192, 720, 336], 'password': False
        }
        self.node_fdroid_address = {
            'node_id': f'{self.oid2[:12]}:R.fdroid_addr', 'path': 'R.fdroid_addr', 'package': m.PACKAGE,
            'class': 'android.widget.TextView', 'text': 'https://f-droid.org/repo',
            'enabled': True, 'bounds_valid': True, 'bounds': [0, 336, 720, 400], 'password': False
        }
        self.node_check_updates = {
            'node_id': f'{self.oid3[:12]}:R.check', 'path': 'R.check', 'package': m.PACKAGE,
            'class': 'android.view.View', 'content_desc': 'Check for updates',
            'enabled': True, 'bounds_valid': True, 'bounds': [520, 64, 616, 160], 'password': False
        }
        self.official_page = [self.node_fdroid, self.node_fdroid_address, self.node_check_updates]

    def test_set_text_refused_in_refresh_mode(self):
        p = m.Policy(mode='application_refresh')
        p.latest = {'observation_id': self.oid1, 'nodes': [self.node_repo]}
        with self.assertRaises(ValueError):
            p.prepare(dict(action='set-text', reason='Attempt typing in refresh mode',
                           observation_id=self.oid1, node_id=self.node_repo['node_id'], value='Aurora'))

    def test_multi_click_navigation_allowed_up_to_5(self):
        p = m.Policy(mode='application_refresh')
        p.latest = {'observation_id': self.oid1, 'nodes': [self.node_repo]}
        args1 = p.prepare(dict(action='click', reason='Click Repositories icon',
                               observation_id=self.oid1, node_id=self.node_repo['node_id']))
        self.assertIn('--node-id', args1)
        self.assertEqual(p.used['click'], 1)

        p.latest = {'observation_id': self.oid2, 'nodes': [self.node_fdroid]}
        args2 = p.prepare(dict(action='click', reason='Click F-Droid repository item',
                               observation_id=self.oid2, node_id=self.node_fdroid['node_id']))
        self.assertIn('--node-id', args2)
        self.assertEqual(p.used['click'], 2)

    def test_non_permitted_navigation_click_rejected(self):
        p = m.Policy(mode='application_refresh')
        # Attempt to click an arbitrary element like Search or Settings in org.fdroid.basic
        node_arbitrary = {
            'node_id': f'{self.oid1[:12]}:R.search', 'path': 'R.search', 'package': m.PACKAGE,
            'class': 'android.widget.TextView', 'text': 'Search',
            'enabled': True, 'bounds_valid': True, 'bounds': [100, 100, 200, 200], 'password': False
        }
        p.latest = {'observation_id': self.oid1, 'nodes': [node_arbitrary]}
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='click', reason='Click Search tab',
                           observation_id=self.oid1, node_id=node_arbitrary['node_id']))
        self.assertIn('outside permitted application_refresh navigation surface', str(ctx.exception))

    def test_observe_call_limits_enforced(self):
        p = m.Policy(mode='application_refresh')
        # 1. Before refresh triggered: max 1 launch readiness observe allowed
        p.prepare(dict(action='observe', reason='First launch readiness observe'))
        self.assertEqual(p.used['observe'], 1)
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='observe', reason='Second launch observe should fail'))
        self.assertIn('launch readiness observe limit exhausted', str(ctx.exception))

        # Trigger refresh on verified official F-Droid repo page
        p.latest = {'observation_id': self.oid3, 'nodes': list(self.official_page)}
        p.prepare(dict(action='click', reason='Click Check for updates button',
                       observation_id=self.oid3, node_id=self.node_check_updates['node_id']))
        self.assertTrue(p.refresh_triggered)

        # 2. After refresh triggered: max 2 refresh observes allowed
        p.prepare(dict(action='observe', reason='First post-refresh observe'))
        p.accept('observe', {'ok': True, 'action': 'observe', 'data': {'observation_id': self.oid3, 'nodes': list(self.official_page)}})
        self.assertEqual(p.refresh_observes_used, 1)

        p.prepare(dict(action='observe', reason='Second post-refresh observe'))
        p.accept('observe', {'ok': True, 'action': 'observe', 'data': {'observation_id': self.oid4, 'nodes': list(self.official_page)}})
        self.assertEqual(p.refresh_observes_used, 2)

        # 3. Third post-refresh observe must be rejected
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='observe', reason='Third post-refresh observe should fail'))
        self.assertIn('refresh observe limit exhausted', str(ctx.exception))

    def test_check_for_updates_triggers_refresh_and_blocks_further_clicks(self):
        p = m.Policy(mode='application_refresh')
        p.latest = {'observation_id': self.oid3, 'nodes': list(self.official_page)}
        args = p.prepare(dict(action='click', reason='Click Check for updates button',
                              observation_id=self.oid3, node_id=self.node_check_updates['node_id']))
        self.assertTrue(p.refresh_triggered)
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='click', reason='Click again while refreshing',
                           observation_id=self.oid3, node_id=self.node_check_updates['node_id']))
        self.assertIn('refresh already triggered', str(ctx.exception))

    def test_ui_completion_rejected_if_in_progress_never_observed(self):
        p = m.Policy(mode='application_refresh')
        p.latest = {'observation_id': self.oid3, 'nodes': list(self.official_page)}
        p.prepare(dict(action='click', reason='Click Check for updates',
                       observation_id=self.oid3, node_id=self.node_check_updates['node_id']))

        # Accept click: but after_observation does NOT show in-progress (e.g. miss)
        completed_nodes = [
            self.node_fdroid, self.node_fdroid_address,
            {'node_id': f'{self.oid3[:12]}:R.check', 'path': 'R.check', 'package': m.PACKAGE,
             'class': 'android.view.View', 'content_desc': 'Check for updates', 'bounds_valid': True,
             'enabled': True, 'bounds': [520, 64, 616, 160], 'password': False}
        ]
        p.accept('click', {'ok': True, 'action': 'click', 'data': {
            'after_observation': {'observation_id': self.oid3, 'nodes': completed_nodes}
        }})
        self.assertFalse(p.refresh_in_progress)

        # Accept observe: still no in-progress seen
        p.accept('observe', {'ok': True, 'action': 'observe', 'data': {
            'observation_id': self.oid4, 'nodes': completed_nodes
        }})
        # Must NOT claim UI completed because in_progress was never confirmed!
        self.assertFalse(p.refresh_ui_completed)
        self.assertFalse(p.goal_verified)

    def test_ui_recovery_without_network_fails_goal(self):
        p = m.Policy(mode='application_refresh')
        p.latest = {'observation_id': self.oid3, 'nodes': list(self.official_page)}
        p.prepare(dict(action='click', reason='Click Check for updates',
                       observation_id=self.oid3, node_id=self.node_check_updates['node_id']))

        after_click_nodes = [
            self.node_fdroid, self.node_fdroid_address,
            {'node_id': f'{self.oid3[:12]}:R.prog', 'path': 'R.prog', 'package': m.PACKAGE,
             'class': 'android.widget.ProgressBar', 'bounds_valid': True, 'enabled': True},
            {'node_id': f'{self.oid3[:12]}:R.check', 'path': 'R.check', 'package': m.PACKAGE,
             'class': 'android.view.View', 'content_desc': 'Check for updates', 'bounds_valid': True,
             'enabled': False, 'bounds': [520, 64, 616, 160], 'password': False}
        ]
        p.accept('click', {'ok': True, 'action': 'click', 'data': {
            'after_observation': {'observation_id': self.oid3, 'nodes': after_click_nodes}
        }})
        self.assertTrue(p.refresh_in_progress)

        completed_nodes = [
            self.node_fdroid, self.node_fdroid_address,
            {'node_id': f'{self.oid4[:12]}:R.check', 'path': 'R.check', 'package': m.PACKAGE,
             'class': 'android.view.View', 'content_desc': 'Check for updates', 'bounds_valid': True,
             'enabled': True, 'bounds': [520, 64, 616, 160], 'password': False}
        ]
        p.accept('observe', {'ok': True, 'action': 'observe', 'data': {
            'observation_id': self.oid4, 'nodes': completed_nodes
        }})
        # UI is completed, but goal is NOT verified until network proof is verified
        self.assertTrue(p.refresh_ui_completed)
        self.assertFalse(p.goal_verified)

        # Attempt finish without network check -> goal remains False
        p.prepare(dict(action='finish', reason='Finish without network evidence'))
        self.assertFalse(p.network_verified)
        self.assertFalse(p.goal_verified)

    def test_full_refresh_workflow_verifies_goal_with_network_conjunction(self):
        p = m.Policy(mode='application_refresh', session_id='test-session-123')
        p.latest = {'observation_id': self.oid3, 'nodes': list(self.official_page)}
        p.prepare(dict(action='click', reason='Click Check for updates',
                       observation_id=self.oid3, node_id=self.node_check_updates['node_id']))

        after_click_nodes = [
            self.node_fdroid, self.node_fdroid_address,
            {'node_id': f'{self.oid3[:12]}:R.prog', 'path': 'R.prog', 'package': m.PACKAGE,
             'class': 'android.widget.ProgressBar', 'bounds_valid': True, 'enabled': True},
            {'node_id': f'{self.oid3[:12]}:R.check', 'path': 'R.check', 'package': m.PACKAGE,
             'class': 'android.view.View', 'content_desc': 'Check for updates', 'bounds_valid': True,
             'enabled': False, 'bounds': [520, 64, 616, 160], 'password': False}
        ]
        p.accept('click', {'ok': True, 'action': 'click', 'data': {
            'after_observation': {'observation_id': self.oid3, 'nodes': after_click_nodes}
        }})
        self.assertTrue(p.refresh_in_progress)

        completed_nodes = [
            self.node_fdroid, self.node_fdroid_address,
            {'node_id': f'{self.oid4[:12]}:R.check', 'path': 'R.check', 'package': m.PACKAGE,
             'class': 'android.view.View', 'content_desc': 'Check for updates', 'bounds_valid': True,
             'enabled': True, 'bounds': [520, 64, 616, 160], 'password': False}
        ]
        p.accept('observe', {'ok': True, 'action': 'observe', 'data': {
            'observation_id': self.oid4, 'nodes': completed_nodes
        }})
        self.assertTrue(p.refresh_ui_completed)
        self.assertFalse(p.goal_verified)

        private = Path(__file__).resolve().parent / '_tmp'
        private.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private) as temp:
            log_file = Path(temp) / 'test-logcat.txt'
            log_bytes = b'valid logcat bytes\n'
            log_file.write_bytes(log_bytes)
            log_hash = hashlib.sha256(log_bytes).hexdigest()

            valid_net_check = {
                'session_id': 'test-session-123',
                'refresh_seq': 1,
                'device_serial': 'emulator-5554',
                'raw_log_path': str(log_file),
                'raw_log_sha256': log_hash,
                'worker_tag': 'org.fdroid.repo.RepoUpdateWorker@c2b35cb',
                'network_verified': True,
                'entry_jar_requested': True,
                'downloader_progress': True,
                'positive_bytes_transferred': True,
                'bytes_transferred': 2589,
                'repo_update_result': 'IndexUpdateResult$Unchanged',
                'workmanager_success': True,
                'pid_matched': True,
                'action_window_matched': True,
                'same_chain_matched': True,
                'error': None
            }

            # Finish with valid network check -> goal verified!
            p.prepare(dict(action='finish', reason='Finish with valid network evidence', network_check=valid_net_check))
            self.assertTrue(p.network_verified)
            self.assertTrue(p.goal_verified)

    def test_network_check_missing_conjunction_rejected(self):
        p = m.Policy(mode='application_refresh', session_id='test-session-123')
        p.refresh_ui_completed = True
        p.refresh_action_seq = 1

        # Test case: positive_bytes_transferred is False (zero bytes)
        net_zero_bytes = {
            'session_id': 'test-session-123',
            'refresh_seq': 1,
            'device_serial': 'emulator-5554',
            'raw_log_sha256': 'c' * 64,
            'worker_tag': 'org.fdroid.repo.RepoUpdateWorker@c2b35cb',
            'network_verified': False,
            'entry_jar_requested': True,
            'downloader_progress': True,
            'positive_bytes_transferred': False,
            'bytes_transferred': 0,
            'repo_update_result': 'IndexUpdateResult$Unchanged',
            'workmanager_success': True,
            'pid_matched': True,
            'action_window_matched': True,
            'same_chain_matched': True,
            'error': None
        }
        p.prepare(dict(action='finish', reason='Finish with zero bytes', network_check=net_zero_bytes))
        self.assertFalse(p.goal_verified)

        # Test case: repo_update_result is missing/invalid
        net_missing_index = dict(net_zero_bytes, positive_bytes_transferred=True, bytes_transferred=100, repo_update_result='Unknown')
        p.prepare(dict(action='finish', reason='Finish with missing index', network_check=net_missing_index))
        self.assertFalse(p.goal_verified)

        # Test case: pid_matched is False (foreign process)
        net_wrong_pid = dict(net_zero_bytes, positive_bytes_transferred=True, bytes_transferred=100, pid_matched=False)
        p.prepare(dict(action='finish', reason='Finish with wrong PID', network_check=net_wrong_pid))
        self.assertFalse(p.goal_verified)

        # Test case: same_chain_matched is False (cross-worker splice)
        net_cross_chain = dict(net_zero_bytes, positive_bytes_transferred=True, bytes_transferred=100, same_chain_matched=False)
        p.prepare(dict(action='finish', reason='Finish with cross-chain splice', network_check=net_cross_chain))
        self.assertFalse(p.goal_verified)

    def test_wide_ancestor_container_rejected(self):
        p = m.Policy(mode='application_refresh')
        # A wide container (like the root view) containing both Repositories and Search
        wide_container = {
            'node_id': f'{self.oid1[:12]}:R.root', 'path': 'R', 'package': m.PACKAGE,
            'class': 'android.widget.FrameLayout', 'clickable': True, 'enabled': True, 'bounds_valid': True,
            'bounds': [0, 0, 720, 1280], 'password': False
        }
        child1 = {
            'node_id': f'{self.oid1[:12]}:R.0.repo', 'path': 'R.0.repo', 'package': m.PACKAGE,
            'class': 'android.view.View', 'content_desc': 'Repositories', 'enabled': True, 'bounds_valid': True
        }
        child2 = {
            'node_id': f'{self.oid1[:12]}:R.0.search', 'path': 'R.0.search', 'package': m.PACKAGE,
            'class': 'android.view.View', 'text': 'Search', 'enabled': True, 'bounds_valid': True
        }
        p.latest = {'observation_id': self.oid1, 'nodes': [wide_container, child1, child2]}
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='click', reason='Click wide ancestor container',
                           observation_id=self.oid1, node_id=wide_container['node_id']))
        self.assertIn('outside permitted application_refresh navigation surface', str(ctx.exception))

    def test_wrong_repository_page_rejected(self):
        p = m.Policy(mode='application_refresh')
        # On a third party repo detail page (e.g. Guardian Project) instead of official F-Droid
        other_repo_title = {
            'node_id': f'{self.oid3[:12]}:R.guardian', 'path': 'R.guardian', 'package': m.PACKAGE,
            'class': 'android.view.View', 'text': 'Guardian Project Official Releases',
            'enabled': True, 'bounds_valid': True, 'bounds': [0, 192, 720, 336], 'password': False
        }
        p.latest = {'observation_id': self.oid3, 'nodes': [other_repo_title, self.node_check_updates]}
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='click', reason='Click Check for updates on wrong repo',
                           observation_id=self.oid3, node_id=self.node_check_updates['node_id']))
        self.assertIn('not on official F-Droid repository page', str(ctx.exception))

    def test_wrong_repository_address_rejected(self):
        p = m.Policy(mode='application_refresh')
        # Fake repo has title F-Droid but wrong address URL
        wrong_address_node = {
            'node_id': f'{self.oid3[:12]}:R.fake_addr', 'path': 'R.fake_addr', 'package': m.PACKAGE,
            'class': 'android.widget.TextView', 'text': 'https://fake-mirror.org/repo',
            'enabled': True, 'bounds_valid': True, 'bounds': [0, 336, 720, 400], 'password': False
        }
        p.latest = {'observation_id': self.oid3, 'nodes': [self.node_fdroid, wrong_address_node, self.node_check_updates]}
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='click', reason='Click Check for updates on fake repo URL',
                           observation_id=self.oid3, node_id=self.node_check_updates['node_id']))
        self.assertIn('not on official F-Droid repository page', str(ctx.exception))

    def test_pre_existing_in_progress_task_rejected(self):
        p = m.Policy(mode='application_refresh')
        # A ProgressBar is already running before clicking Check for updates
        pre_progress = {
            'node_id': f'{self.oid3[:12]}:R.prog', 'path': 'R.prog', 'package': m.PACKAGE,
            'class': 'android.widget.ProgressBar', 'bounds_valid': True, 'enabled': True
        }
        p.latest = {'observation_id': self.oid3, 'nodes': list(self.official_page) + [pre_progress]}
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='click', reason='Click while refresh already running',
                           observation_id=self.oid3, node_id=self.node_check_updates['node_id']))
        self.assertIn('pre-existing in-progress task detected before refresh click', str(ctx.exception))

    def test_ambiguous_multiple_check_buttons_rejected(self):
        p = m.Policy(mode='application_refresh')
        # Two Check for updates buttons on page
        dup_button = copy.deepcopy(self.node_check_updates)
        dup_button['node_id'] = f'{self.oid3[:12]}:R.check2'
        dup_button['path'] = 'R.check2'
        p.latest = {'observation_id': self.oid3, 'nodes': list(self.official_page) + [dup_button]}
        with self.assertRaises(ValueError) as ctx:
            p.prepare(dict(action='click', reason='Click when duplicate buttons exist',
                           observation_id=self.oid3, node_id=self.node_check_updates['node_id']))
        self.assertIn('expected exactly 1 Check for updates button', str(ctx.exception))

    def test_duplicate_check_button_in_subsequent_observation_rejected(self):
        p = m.Policy(mode='application_refresh')
        p.latest = {'observation_id': self.oid3, 'nodes': list(self.official_page)}
        p.prepare(dict(action='click', reason='Click Check for updates',
                       observation_id=self.oid3, node_id=self.node_check_updates['node_id']))

        # Accept click: show in-progress
        progress_nodes = [
            self.node_fdroid, self.node_fdroid_address,
            {'node_id': f'{self.oid3[:12]}:R.prog', 'path': 'R.prog', 'package': m.PACKAGE,
             'class': 'android.widget.ProgressBar', 'bounds_valid': True, 'enabled': True},
            {'node_id': f'{self.oid3[:12]}:R.check', 'path': 'R.check', 'package': m.PACKAGE,
             'class': 'android.view.View', 'content_desc': 'Check for updates', 'bounds_valid': True,
             'enabled': False, 'bounds': [520, 64, 616, 160], 'password': False}
        ]
        p.accept('click', {'ok': True, 'action': 'click', 'data': {
            'after_observation': {'observation_id': self.oid3, 'nodes': progress_nodes}
        }})
        self.assertTrue(p.refresh_in_progress)

        # In observe: a newly added / duplicate Check for updates button appears
        dup_button = {
            'node_id': f'{self.oid4[:12]}:R.check2', 'path': 'R.check2', 'package': m.PACKAGE,
            'class': 'android.view.View', 'content_desc': 'Check for updates', 'bounds_valid': True,
            'enabled': True, 'bounds': [520, 160, 616, 256], 'password': False
        }
        dup_completed_nodes = [
            self.node_fdroid, self.node_fdroid_address,
            {'node_id': f'{self.oid4[:12]}:R.check', 'path': 'R.check', 'package': m.PACKAGE,
             'class': 'android.view.View', 'content_desc': 'Check for updates', 'bounds_valid': True,
             'enabled': True, 'bounds': [520, 64, 616, 160], 'password': False},
            dup_button
        ]
        p.accept('observe', {'ok': True, 'action': 'observe', 'data': {
            'observation_id': self.oid4, 'nodes': dup_completed_nodes
        }})
        # Because multiple buttons appeared, uniqueness check fails -> refresh_ui_completed remains False!
        self.assertFalse(p.refresh_ui_completed)

    def test_tight_wrapper_around_check_button_accepted_and_completed(self):
        p = m.Policy(mode='application_refresh')
        wrapper_node = {
            'node_id': f'{self.oid3[:12]}:R.wrap', 'path': 'R.wrap', 'package': m.PACKAGE,
            'class': 'android.widget.FrameLayout', 'clickable': True, 'enabled': True,
            'bounds_valid': True, 'bounds': [520, 64, 616, 160], 'password': False
        }
        btn_child = {
            'node_id': f'{self.oid3[:12]}:R.wrap.btn', 'path': 'R.wrap.btn', 'package': m.PACKAGE,
            'class': 'android.view.View', 'content_desc': 'Check for updates',
            'enabled': True, 'bounds_valid': True, 'bounds': [520, 64, 616, 160], 'password': False
        }
        page_with_wrapper = [self.node_fdroid, self.node_fdroid_address, wrapper_node, btn_child]
        p.latest = {'observation_id': self.oid3, 'nodes': page_with_wrapper}

        # Click the wrapper container
        p.prepare(dict(action='click', reason='Click wrapper around Check for updates',
                       observation_id=self.oid3, node_id=wrapper_node['node_id']))
        self.assertTrue(p.refresh_triggered)
        self.assertEqual(p.requested_target_identity['node_id'], wrapper_node['node_id'])
        self.assertEqual(p.semantic_button_identity['node_id'], btn_child['node_id'])

        # Progress observation
        progress_child = dict(btn_child, enabled=False)
        p.accept('click', {'ok': True, 'action': 'click', 'data': {
            'after_observation': {'observation_id': self.oid3, 'nodes': [self.node_fdroid, self.node_fdroid_address, wrapper_node, progress_child]}
        }})
        self.assertTrue(p.refresh_in_progress)

        # Recovery observation: wrapper and child button are verified together
        recovered_child = dict(btn_child, enabled=True)
        p.accept('observe', {'ok': True, 'action': 'observe', 'data': {
            'observation_id': self.oid4, 'nodes': [self.node_fdroid, self.node_fdroid_address, wrapper_node, recovered_child]
        }})
        self.assertTrue(p.refresh_ui_completed)

    def test_foreign_or_old_session_network_check_rejected(self):
        p = m.Policy(mode='application_refresh', session_id='session-current-999')
        p.refresh_ui_completed = True
        p.refresh_action_seq = 1

        foreign_net = {
            'session_id': 'session-old-001',
            'refresh_seq': 1,
            'device_serial': 'emulator-5554',
            'raw_log_sha256': 'd' * 64,
            'worker_tag': 'org.fdroid.repo.RepoUpdateWorker@1111111',
            'network_verified': True,
            'entry_jar_requested': True,
            'downloader_progress': True,
            'positive_bytes_transferred': True,
            'bytes_transferred': 2589,
            'repo_update_result': 'IndexUpdateResult$Unchanged',
            'workmanager_success': True,
            'pid_matched': True,
            'action_window_matched': True,
            'same_chain_matched': True,
            'error': None
        }
        p.prepare(dict(action='finish', reason='Finish with foreign session check', network_check=foreign_net))
        self.assertFalse(p.goal_verified)

    def test_network_check_mismatched_refresh_seq_rejected(self):
        p = m.Policy(mode='application_refresh', session_id='session-current-999')
        p.refresh_ui_completed = True
        p.refresh_action_seq = 2  # refresh happened at seq 2

        seq_mismatch_net = {
            'session_id': 'session-current-999',
            'refresh_seq': 1,
            'device_serial': 'emulator-5554',
            'raw_log_sha256': 'd' * 64,
            'worker_tag': 'org.fdroid.repo.RepoUpdateWorker@1111111',
            'network_verified': True,
            'entry_jar_requested': True,
            'downloader_progress': True,
            'positive_bytes_transferred': True,
            'bytes_transferred': 2589,
            'repo_update_result': 'IndexUpdateResult$Unchanged',
            'workmanager_success': True,
            'pid_matched': True,
            'action_window_matched': True,
            'same_chain_matched': True,
            'error': None
        }
        p.prepare(dict(action='finish', reason='Finish with mismatched seq', network_check=seq_mismatch_net))
        self.assertFalse(p.goal_verified)

    def test_network_check_expected_device_serial_mismatch_rejected(self):
        p = m.Policy(mode='application_refresh', expected_device_serial='emulator-5554')
        p.refresh_ui_completed = True
        p.refresh_action_seq = 1

        wrong_serial_net = {
            'refresh_seq': 1,
            'device_serial': 'emulator-5556',
            'raw_log_sha256': 'd' * 64,
            'worker_tag': 'org.fdroid.repo.RepoUpdateWorker@1111111',
            'network_verified': True,
            'entry_jar_requested': True,
            'downloader_progress': True,
            'positive_bytes_transferred': True,
            'bytes_transferred': 2589,
            'repo_update_result': 'IndexUpdateResult$Unchanged',
            'workmanager_success': True,
            'pid_matched': True,
            'action_window_matched': True,
            'same_chain_matched': True,
            'error': None
        }
        p.prepare(dict(action='finish', reason='Finish with wrong device serial', network_check=wrong_serial_net))
        self.assertFalse(p.goal_verified)

    def test_network_check_raw_log_file_hash_mismatch_rejected(self):
        private = Path(__file__).resolve().parent / '_tmp'
        private.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private) as temp:
            log_file = Path(temp) / 'test-logcat.txt'
            log_file.write_bytes(b'actual log content on disk\n')
            actual_hash = hashlib.sha256(b'actual log content on disk\n').hexdigest()

            p = m.Policy(mode='application_refresh')
            p.refresh_ui_completed = True
            p.refresh_action_seq = 1

            # Hash mismatch: raw_log_sha256 differs from actual file hash
            tampered_net = {
                'refresh_seq': 1,
                'device_serial': 'emulator-5554',
                'raw_log_path': str(log_file),
                'raw_log_sha256': '0' * 64,
                'worker_tag': 'org.fdroid.repo.RepoUpdateWorker@1111111',
                'network_verified': True,
                'entry_jar_requested': True,
                'downloader_progress': True,
                'positive_bytes_transferred': True,
                'bytes_transferred': 2589,
                'repo_update_result': 'IndexUpdateResult$Unchanged',
                'workmanager_success': True,
                'pid_matched': True,
                'action_window_matched': True,
                'same_chain_matched': True,
                'error': None
            }
            p.prepare(dict(action='finish', reason='Finish with tampered log', network_check=tampered_net))
            self.assertFalse(p.goal_verified)

            # Untampered: raw_log_sha256 matches file
            valid_net = dict(tampered_net, raw_log_sha256=actual_hash)
            p.prepare(dict(action='finish', reason='Finish with valid log', network_check=valid_net))
            self.assertTrue(p.goal_verified)

    def test_worker_config_initialization_and_controlled_location_enforcement(self):
        private = Path(__file__).resolve().parent / '_tmp'
        private.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=private) as base_temp:
            curr_session_dir = Path(base_temp) / 'session-current'
            curr_session_dir.mkdir()
            old_session_dir = Path(base_temp) / 'session-old'
            old_session_dir.mkdir()

            curr_log = curr_session_dir / 'current-logcat.txt'
            curr_log.write_bytes(b'current session raw log bytes\n')
            curr_hash = hashlib.sha256(b'current session raw log bytes\n').hexdigest()

            old_log = old_session_dir / 'old-logcat.txt'
            old_log.write_bytes(b'old session raw log bytes\n')
            old_hash = hashlib.sha256(b'old session raw log bytes\n').hexdigest()

            # 1. Initialize via Policy.from_config with application_refresh mode
            cfg = {
                'mode': 'application_refresh',
                'session_id': 'session-current-123',
                'controlled_evidence_dir': str(curr_session_dir)
            }
            p = m.Policy.from_config(cfg)
            self.assertEqual(p.mode, 'application_refresh')
            self.assertEqual(p.session_id, 'session-current-123')
            self.assertEqual(p.controlled_evidence_dir, str(curr_session_dir))
            self.assertTrue(p.require_raw_log_file)

            p.refresh_ui_completed = True
            p.refresh_action_seq = 1

            base_net = {
                'session_id': 'session-current-123',
                'refresh_seq': 1,
                'device_serial': 'emulator-5554',
                'worker_tag': 'org.fdroid.repo.RepoUpdateWorker@1111111',
                'network_verified': True,
                'entry_jar_requested': True,
                'downloader_progress': True,
                'positive_bytes_transferred': True,
                'bytes_transferred': 2589,
                'repo_update_result': 'IndexUpdateResult$Unchanged',
                'workmanager_success': True,
                'pid_matched': True,
                'action_window_matched': True,
                'same_chain_matched': True,
                'error': None
            }

            # 2. Missing raw_log_path rejected
            net_no_path = dict(base_net, raw_log_sha256=curr_hash)
            p.prepare(dict(action='finish', reason='Finish with missing raw_log_path', network_check=net_no_path))
            self.assertFalse(p.goal_verified)

            # 3. Missing file on disk rejected
            net_missing_file = dict(base_net, raw_log_path=str(curr_session_dir / 'nonexistent.txt'), raw_log_sha256=curr_hash)
            p.prepare(dict(action='finish', reason='Finish with non-existent raw_log_path', network_check=net_missing_file))
            self.assertFalse(p.goal_verified)

            # 4. Old session file (escapes controlled_evidence_dir) rejected
            net_old_session = dict(base_net, raw_log_path=str(old_log), raw_log_sha256=old_hash)
            p.prepare(dict(action='finish', reason='Finish with old session raw log', network_check=net_old_session))
            self.assertFalse(p.goal_verified)

            # 5. Tampered file rejected
            net_tampered = dict(base_net, raw_log_path=str(curr_log), raw_log_sha256='f' * 64)
            p.prepare(dict(action='finish', reason='Finish with tampered raw log', network_check=net_tampered))
            self.assertFalse(p.goal_verified)

            # 6. Legal file in controlled_evidence_dir accepted
            net_legal = dict(base_net, raw_log_path=str(curr_log), raw_log_sha256=curr_hash)
            p.prepare(dict(action='finish', reason='Finish with legal current raw log', network_check=net_legal))
            self.assertTrue(p.goal_verified)

            # 7. Normal stepwise_input mode preserves existing contract
            input_cfg = {'mode': 'stepwise_input', 'session_id': 'session-input-456'}
            p_input = m.Policy.from_config(input_cfg)
            self.assertEqual(p_input.mode, 'stepwise_input')
            self.assertFalse(p_input.require_raw_log_file)


if __name__ == '__main__':
    unittest.main()
