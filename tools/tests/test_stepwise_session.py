"""Offline policy tests; importing these modules never starts a worker/device."""
from pathlib import Path
import copy
import hashlib
import json
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


def req(action, **kw):
    return dict(action=action, reason='Choose the unique field in the current observation.', **kw)


def ready():
    p = m.Policy()
    p.latest = {'observation_id': OID, 'nodes': [copy.deepcopy(NODE)]}
    return p


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


if __name__ == '__main__':
    unittest.main()
