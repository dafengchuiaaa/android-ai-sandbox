"""One bounded session; external agent chooses each formal CLI action.

No automatic UI action sequence. A worker owns cleanup even while the agent
is between turns. Runtime requests are data, never shell commands.
"""
from __future__ import annotations

import argparse
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


def summary(payload):
    obs = observation(payload)
    result = {'ok': payload.get('ok'), 'action': payload.get('action'), 'error': payload.get('error')}
    if obs is None:
        result['data'] = payload.get('data')
        return result
    nodes = [n for n in obs['nodes'] if n.get('enabled') is True and
             (n.get('text') or n.get('content_desc') or n.get('class') == 'android.widget.EditText')]
    keys = ['node_id', 'parent_id', 'path', 'package', 'class', 'resource_id', 'text', 'content_desc',
            'bounds', 'bounds_valid', 'enabled', 'clickable', 'focused', 'password']
    result.update(observation_id=obs.get('observation_id'), package=obs.get('package'),
                  nodes=[{k: n.get(k) for k in keys} for n in nodes[:80]],
                  summary_truncated=len(nodes) > 80, total_nodes=len(obs['nodes']))
    return result


class Policy:
    def __init__(self):
        self.calls = 0
        self.used = {'launch': 0, 'click': 0, 'set-text': 0}
        self.latest = None
        self.target = None
        self.text_succeeded = False
        self.independent_readback = False

    def prepare(self, req):
        action = req.get('action')
        if action not in {'status', 'launch', 'observe', 'click', 'set-text', 'finish'}:
            raise ValueError('action not allowed')
        if not isinstance(req.get('reason'), str) or not 8 <= len(req['reason']) <= 2000:
            raise ValueError('a concrete decision reason is required')
        if action == 'finish':
            return []
        if self.calls >= MAX_CALLS:
            raise ValueError('CLI call limit exhausted')
        args = []
        if action == 'launch':
            if self.used[action] or self.calls != 0:
                raise ValueError('one initial launch only')
            args = ['--package', PACKAGE]
        elif action in {'click', 'set-text'}:
            if self.used[action]:
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
            args = ['--observation-id', oid, '--node-id', nid]
            if action == 'set-text':
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
        if obs is not None:
            self.latest = obs
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
    def verify_manifest(self, stage_label):
        count = verify_files(self.project_root, self.manifests)
        for path, expected in self.tool_hashes.items():
            if digest(path) != expected:
                raise ValueError('session tool changed during execution')
        save(self.run_dir / (stage_label + '.json'), {'verified': True, 'file_count': count, 'tool_hashes': self.tool_hashes})


def serve(out):
    with (out / 'worker-claimed.json').open('x', encoding='utf-8') as handle:
        json.dump({'pid': os.getpid(), 'claimed_utc': support.utc_now()}, handle)
    cfg = load(out / 'config.json')
    project = Path(cfg['project_root']).resolve()
    manifests = list(map(Path, cfg['manifests']))
    verify_files(project, manifests)
    runner = Session(project_root=project, python_exe=Path(cfg['python']), adb_exe=Path(cfg['adb']),
                     manifest_path=manifests[0], output_base=out, dry_run=False)
    runner.manifests = manifests
    runner.tool_hashes = cfg['tool_hashes']
    runner.deadline = runner.start_time + TOTAL_S
    policy = Policy()
    current = {'run_dir': str(runner.run_dir), 'state_file': str(runner.state_file),
               'session_dir': str(runner.session_dir), 'phase': 'booting', 'next_seq': 1,
               'max_session_s': TOTAL_S, 'cleanup_reserved_s': CLEANUP_S, 'max_cli_calls': MAX_CALLS}
    save(out / 'current-session.json', current)
    verdict = {'status': 'running', 'goal_verified': False, 'stopped': False, 'decision_mode': 'external_stepwise',
               'started_utc': support.utc_now(), 'max_session_s': TOTAL_S}
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
        runner.execute_cmd('start', [runner.powershell_exe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                   str(runner.launcher_script), '-StateFile', str(runner.state_file),
                                   '-BootTimeoutSec', '120', '-NativeTimeoutSec', '8'], timeout=140)
        state = load(runner.state_file)
        save(runner.run_dir / 'state-start.json', state)
        if not support.valid_emulator_serial(state.get('serial')):
            raise ValueError('invalid state-derived serial')
        runner.execute_cmd('owned-status', [runner.powershell_exe, '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                                          str(runner.launcher_script), '-Status', '-StateFile', str(runner.state_file),
                                          '-NativeTimeoutSec', '8'], timeout=25)
        spec = importlib.util.spec_from_file_location('stepwise_live_validator', project / 'integration/validate_response.py')
        validator = importlib.util.module_from_spec(spec)
        sys.path.insert(0, str(project))
        spec.loader.exec_module(validator)
        current.update(phase='ready', serial=state['serial'], elapsed_s=round(time.monotonic()-runner.start_time, 3))
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
            if req.get('evidence_sha256') != current.get('last_response_sha256'):
                raise ValueError('decision is not bound to the latest complete response')
            if req.get('action') == 'click' and runner.remaining_s() < support.CLICK_BUDGET_S + support.SET_TEXT_MIN_REMAINING_S:
                raise TimeoutError('insufficient budget for click, text, independent readback and reserved cleanup')
            args = policy.prepare(req)
            save(runner.run_dir / f'decision-{seq:04d}.json', req)
            if req['action'] == 'finish':
                verdict.update(status='completed' if policy.independent_readback else 'incomplete',
                               goal_verified=policy.independent_readback, finish_reason=req['reason'])
                save(out / 'responses' / f'{seq:04d}.json', {'finished': True, 'goal_verified': policy.independent_readback})
                active_request = None
                break
            runner.verify_manifest(f'manifest-before-action-{seq:04d}')
            label = f'agent-{seq:04d}-{req["action"]}'
            payload = runner.call_cli(label, req['action'], *args)
            checked = validator.validate(payload, runner.commands[-1]['exit_code'], req['action'],
                                         expected_serial=state['serial'], expected_profile=PROFILE)
            save(runner.run_dir / f'validation-{seq:04d}.json', checked)
            if checked.get('protocol_valid') is not True:
                raise ValueError('formal response contract rejected: ' + repr(checked.get('errors')))
            policy.accept(req['action'], payload)
            response = {'seq': seq, 'payload': payload, 'validation': checked,
                        'goal_verified': policy.independent_readback,
                        'elapsed_s': round(time.monotonic()-runner.start_time, 3),
                        'remaining_operational_s': round(runner.remaining_s(), 3)}
            response_path = out / 'responses' / f'{seq:04d}.json'
            save(response_path, response)
            current.update(next_seq=seq+1, last_response_sha256=digest(response_path), elapsed_s=response['elapsed_s'],
                           remaining_operational_s=response['remaining_operational_s'], goal_verified=policy.independent_readback)
            save(out / 'current-session.json', current)
            active_request = None
        else:
            raise TimeoutError('fixed operational deadline reached while waiting for agent')
    except Exception as exc:
        verdict.update(status='failed', error=repr(exc), error_code=getattr(exc, 'code', type(exc).__name__))
        if active_request is not None:
            save(out / 'responses' / f'{active_request:04d}.json', {'failed': True, 'error': repr(exc),
                 'error_code': verdict['error_code'], 'run_dir': str(runner.run_dir)})
    finally:
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
        verdict.update(cli_calls=policy.calls, input_calls=policy.used,
                       text_backend_verified=policy.text_succeeded, independent_readback=policy.independent_readback,
                       unknown_input_state=runner.unknown_input_state, elapsed_s=round(time.monotonic()-runner.start_time, 3),
                       ended_utc=support.utc_now(), run_dir=str(runner.run_dir))
        save(runner.run_dir / 'stepwise-verdict.json', verdict)
        save(out / 'result.json', verdict)
        current['phase'] = 'stopped' if verdict['stopped'] else 'cleanup_failed'
        save(out / 'current-session.json', current)
    return 0 if verdict['status'] == 'completed' and verdict['stopped'] else 1


def begin(args):
    if os.name != 'nt':
        raise ValueError('live lifecycle is Windows PowerShell 5.1 only')
    project, out = Path(args.project_root).resolve(), Path(args.out).resolve()
    manifests = [str(Path(p).resolve()) for p in args.manifest]
    count = verify_files(project, manifests)
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'device-session-claimed.json').open('x', encoding='utf-8') as handle:
        json.dump({'max_session_s': TOTAL_S, 'cleanup_reserved_s': CLEANUP_S, 'max_cli_calls': MAX_CALLS,
                   'max_launches': 1, 'max_clicks': 1, 'max_set_text': 1, 'value': 'Aurora',
                   'source_files': count, 'decision_mode': 'external_stepwise', 'no_input_replay': True}, handle, indent=2)
    (out / 'requests').mkdir()
    (out / 'responses').mkdir()
    paths = [Path(__file__).resolve(), Path(support.__file__).resolve()]
    cfg = {'project_root': str(project), 'python': str(Path(sys.executable).resolve()), 'adb': str(Path(args.adb).resolve()),
           'manifests': manifests, 'tool_hashes': {str(p): digest(p) for p in paths}}
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
    worker = sub.add_parser('serve')
    worker.add_argument('--out', required=True)
    action = sub.add_parser('submit')
    action.add_argument('--out', required=True)
    action.add_argument('--action', required=True, choices=['status', 'launch', 'observe', 'click', 'set-text', 'finish'])
    action.add_argument('--reason', required=True)
    action.add_argument('--observation-id', default=None)
    action.add_argument('--node-id', default=None)
    action.add_argument('--value', default=None)
    args = parser.parse_args(argv)
    if args.command == 'serve':
        return serve(Path(args.out).resolve())
    print(json.dumps(begin(args) if args.command == 'begin' else submit(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    sys.exit(main())
