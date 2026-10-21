"""Opt-in API35 collector. No input, at most two read attempts, one 20s monotonic deadline.

Supports Python-level launch pacing bounded strictly within the same monotonic 20.0s deadline.
Preserves full XML byte equality and strict rejection boundaries.
"""
import base64
import hashlib
import json
import math
import struct
from pathlib import Path
import time
import uuid
import xml.etree.ElementTree as ET

MAX_XML_BYTES = 2 * 1024 * 1024
MAX_TOTAL_TIMEOUT_S = 20.0
LAUNCH_PRE_SETTLE_S = 1.5
LAUNCH_INTER_ATTEMPT_PACING_S = 4.0
MIN_ATTEMPT_BUDGET_S = 1.0
MAX_FAILURE_XML_BYTES = 128 * 1024
MAX_FAILURE_BUNDLE_BYTES = MAX_FAILURE_XML_BYTES + 2048
FAILURE_PULL_CAP_S = 2.0
FAILURE_CLEANUP_RESERVE_S = 2.0


def decode_failure_snapshots(raw, nonce, manifest, diagnostics=None):
    """Decode diagnostics only; never feed these bytes to validate_report/latest observation."""
    if not isinstance(manifest, dict) or manifest.get('status') != 'available':
        raise ValueError('failure bundle not available')
    if type(manifest.get('bytes')) is not int or manifest['bytes'] != len(raw):
        raise ValueError('failure bundle length mismatch')
    if not 38 < len(raw) <= MAX_FAILURE_BUNDLE_BYTES or hashlib.sha256(raw).hexdigest() != manifest.get('sha256'):
        raise ValueError('failure bundle size/hash mismatch')
    if raw[:38] != b'AISF1' + nonce.encode('ascii') + b'\x02':
        raise ValueError('failure bundle identity mismatch')
    offset, total, snapshots = 38, 0, []
    for index in (1, 2):
        window, taken, cleared, pkg_len, xml_len = struct.unpack_from('>iqBHI', raw, offset)
        offset += 19
        if window < 0 or taken < 0 or cleared not in (0, 1) or not 0 < pkg_len <= 512 or xml_len <= 0:
            raise ValueError('invalid failure snapshot metadata')
        total += xml_len
        if total > MAX_FAILURE_XML_BYTES or offset + pkg_len + xml_len > len(raw):
            raise ValueError('invalid failure snapshot length')
        package = raw[offset:offset + pkg_len].decode('utf-8')
        offset += pkg_len
        data = raw[offset:offset + xml_len]
        offset += xml_len
        item = dict(index=index, window_id=window, taken_ms=taken, cache_cleared=bool(cleared),
                    package=package, xml_bytes=xml_len, sha256=hashlib.sha256(data).hexdigest())
        if diagnostics is not None:
            if not isinstance(diagnostics, list) or len(diagnostics) != 2 or diagnostics[index-1] != item:
                raise ValueError('failure snapshot diagnostics mismatch')
        snapshots.append((item, data))
    if offset != len(raw):
        raise ValueError('trailing failure bundle bytes')
    return snapshots


class DirectReadError(Exception):
    def __init__(self, detail):
        super().__init__('Stable direct UI read failed; no input retried')
        self.detail = detail


def is_transient_error(err_str: str) -> bool:
    """Classify whether a collector failure was caused by transient UI instability."""
    s = str(err_str).lower()
    transient_tokens = (
        "unstable_tree",
        "window_changed",
        "snapshot hash mismatch",
        "snapshots not separated",
        "bounds_changed",
        "package mismatch",
        "unexpected xml structure/package"
    )
    return any(t in s for t in transient_tokens)


def validate_sliding_audit(r):
    if r.get('sampling_protocol') != 'bounded_sliding_pair_v1':
        raise ValueError('missing sliding sampling protocol')
    n = r.get('total_snapshots_taken')
    rows, polls = r.get('all_snapshot_diagnostics'), r.get('all_root_ready_polls')
    if type(n) is not int or not 2 <= n <= 4 or not isinstance(rows, list) or len(rows) != n:
        raise ValueError('invalid total snapshots')
    if not isinstance(polls, list) or len(polls) != n or any(type(v) is not int or not 1 <= v <= 16 for v in polls):
        raise ValueError('invalid readiness audit')
    if type(r.get('collector_budget_ms')) is not int or not 1000 <= r['collector_budget_ms'] <= 6000:
        raise ValueError('invalid collector budget audit')
    last = None
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or type(row.get('index')) is not int or row['index'] != index:
            raise ValueError('invalid sample index')
        if type(row.get('taken_ms')) is not int or row['taken_ms'] < 0 or (last is not None and row['taken_ms'] - last < 300):
            raise ValueError('invalid adjacent sample gap')
        if type(row.get('cache_cleared')) is not bool or type(row.get('window_id')) is not int:
            raise ValueError('invalid sample metadata')
        if not isinstance(row.get('package'), str) or not row['package']:
            raise ValueError('invalid sample package')
        if type(row.get('xml_bytes')) is not int or not 0 < row['xml_bytes'] <= MAX_XML_BYTES:
            raise ValueError('invalid sample bytes')
        if not isinstance(row.get('sha256'), str) or len(row['sha256']) != 64 or any(c not in '0123456789abcdef' for c in row['sha256']):
            raise ValueError('invalid sample digest')
        last = row['taken_ms']
    return n, rows, polls


def validate_report(raw, nonce, expect_sliding=False):
    r = json.loads(raw)
    if not isinstance(r, dict) or r.get('version') != 1 or r.get('nonce') != nonce:
        raise ValueError('collector protocol/nonce mismatch')
    if r.get('ok') is not True or r.get('stable') is not True or r.get('snapshot_count') != 2:
        raise ValueError('collector refused tree: ' + str(r.get('error'))[:400])
    if type(r.get('snapshot_gap_ms')) is not int or r['snapshot_gap_ms'] < 300:
        raise ValueError('snapshots not separated')
    if r.get('cache_clear') != [True, True]:
        raise ValueError('fresh cache reads not confirmed')
    if type(r.get('window_id')) is not int or r['window_id'] < 0:
        raise ValueError('invalid active window')
    encoded = r.get('xml_b64')
    if not isinstance(encoded, str) or len(encoded) > (MAX_XML_BYTES + 2) // 3 * 4:
        raise ValueError('XML payload missing or oversized')
    data = base64.b64decode(encoded, validate=True)
    if not data or len(data) > MAX_XML_BYTES:
        raise ValueError('XML size outside bound')
    digest = hashlib.sha256(data).hexdigest()
    if r.get('sha256_first') != digest or r.get('sha256_second') != digest:
        raise ValueError('snapshot hash mismatch')
    root = ET.fromstring(data)
    first = root.find('node')
    if root.tag != 'hierarchy' or first is None or not r.get('package') or first.get('package') != r['package']:
        raise ValueError('unexpected XML structure/package')
    if expect_sliding or 'sampling_protocol' in r:
        n, rows, polls = validate_sliding_audit(r)
        if r.get('winning_pair_indices') != [n-1, n] or any(type(v) is not int for v in r['winning_pair_indices']):
            raise ValueError('invalid winning adjacent pair')
        for row in rows[-2:]:
            if row['sha256'] != digest or row['xml_bytes'] != len(data) or row['window_id'] != r['window_id'] or row['package'] != r['package'] or row['cache_cleared'] is not True:
                raise ValueError('winning pair audit mismatch')
        if rows[-1]['taken_ms']-rows[-2]['taken_ms'] != r['snapshot_gap_ms'] or r.get('root_ready_polls') != polls[-2:]:
            raise ValueError('winning pair timing/readiness mismatch')
    proof = {k: v for k, v in r.items() if k != 'xml_b64'}
    return data, proof


def collect(adb, jar_path, expected_jar_sha256, output_dir, total_timeout=20.0,
            is_launch=False, _clock=time.monotonic, _sleep=time.sleep):
    start = _clock()
    if not math.isfinite(total_timeout) or not (0 < total_timeout <= MAX_TOTAL_TIMEOUT_S):
        raise DirectReadError({'reason': 'invalid_budget', 'total_timeout_s': str(total_timeout)})
    deadline = start + total_timeout
    jar_path = Path(jar_path)
    if hashlib.sha256(jar_path.read_bytes()).hexdigest() != expected_jar_sha256:
        raise DirectReadError({'reason': 'collector_jar_hash_mismatch'})
    trace = {
        'backend': 'stable-direct',
        'total_timeout_s': total_timeout,
        'max_attempts': 2,
        'is_launch': bool(is_launch),
        'attempts': []
    }
    token = uuid.uuid4().hex
    evidence_path = Path(output_dir) / ('stable-direct-' + token + '.json')

    def run(args, cap, row):
        left = deadline - _clock()
        if left <= 0.05:
            raise TimeoutError('absolute read deadline exhausted')
        timeout = min(cap, left)
        cmd_start = _clock()
        entry = {'args': args, 'timeout_s': timeout, 'start_monotonic': cmd_start}
        row.setdefault('commands', []).append(entry)
        cmd_exc = None
        rc = -1
        out = ""
        err = ""
        try:
            rc, out, err = adb.run(args, timeout=timeout)
            return rc, out, err
        except Exception as exc:
            cmd_exc = exc
            err = str(exc)
            raise
        finally:
            cmd_end = _clock()
            entry['end_monotonic'] = cmd_end
            entry['elapsed_s'] = round(cmd_end - cmd_start, 4)
            entry.update(rc=rc, stderr=err[:1200])
            if cmd_exc is not None:
                entry['exception'] = repr(cmd_exc)
            if rc != 0 and out:
                entry['failure_stdout'] = out[:4096]
            if cmd_exc is None and _clock() > deadline:
                raise TimeoutError('command returned past absolute deadline')

    def checked(args, cap, row):
        rc, out, err = run(args, cap, row)
        if rc != 0:
            raise ValueError('command failed: ' + str(rc) + ' ' + err[:400])
        return out

    def preserve_failure(report, nonce, remote_sidecar, row):
        manifest = report.get('failure_evidence')
        if report.get('sampling_protocol') == 'bounded_sliding_pair_v1' and isinstance(manifest, dict) and manifest.get('status') == 'available':
            n, rows, _ = validate_sliding_audit(report)
            if report.get('failure_pair_indices') != [n-1, n]:
                raise ValueError('invalid failure adjacent pair')
            if report.get('snapshot_diagnostics') != [{**row, 'index': i} for i, row in enumerate(rows[-2:], 1)]:
                raise ValueError('failure pair audit mismatch')
            row['failure_pair_indices'] = [n-1, n]
            row['all_snapshot_diagnostics'] = rows
        row['failure_snapshots'] = {'status': 'unavailable'}
        if not isinstance(manifest, dict) or manifest.get('status') != 'available':
            return
        size = manifest.get('bytes')
        if type(size) is not int or not 38 < size <= MAX_FAILURE_BUNDLE_BYTES:
            row['failure_snapshots'] = {'status': 'rejected', 'reason': 'invalid_bundle_size'}
            return
        if deadline - _clock() < FAILURE_PULL_CAP_S + FAILURE_CLEANUP_RESERVE_S:
            row['failure_snapshots'] = {'status': 'skipped', 'reason': 'insufficient_read_budget'}
            return
        temporary = Path(output_dir) / ('stable-direct-' + nonce + '-failure.bin')
        created = []
        reserved = False
        try:
            with temporary.open('xb'):
                reserved = True
            checked(['pull', remote_sidecar, str(temporary)], FAILURE_PULL_CAP_S, row)
            if temporary.stat().st_size != size:
                raise ValueError('pulled failure bundle size mismatch')
            raw = temporary.read_bytes()
            snapshots = decode_failure_snapshots(raw, nonce, manifest, report.get('snapshot_diagnostics'))
            saved = []
            for item, data in snapshots:
                if _clock() >= deadline:
                    raise TimeoutError('absolute read deadline exhausted saving failure evidence')
                path = Path(output_dir) / ('stable-direct-' + nonce + '-snapshot-' + str(item['index']) + '.xml')
                with path.open('xb') as handle:
                    created.append(path)
                    handle.write(data)
                saved.append({**item, 'path': str(path)})
            if _clock() >= deadline:
                raise TimeoutError('failure evidence finished past absolute deadline')
            row['failure_snapshots'] = {'status': 'saved', 'diagnostic_only': True,
                                        'bundle_sha256': manifest['sha256'], 'snapshots': saved}
        except Exception as exc:
            for path in created:
                path.unlink(missing_ok=True)
            row['failure_snapshots'] = {'status': 'incomplete', 'reason': str(exc)[:500]}
        finally:
            if reserved:
                temporary.unlink(missing_ok=True)

    # Launch pre-settle pacing (only when is_launch=True)
    if is_launch:
        rem_before_settle = deadline - _clock()
        if rem_before_settle < LAUNCH_PRE_SETTLE_S + MIN_ATTEMPT_BUDGET_S:
            trace['failure'] = 'insufficient_budget_for_launch_pre_settle'
            trace.update(elapsed_s=_clock() - start, ok=False)
            evidence_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
            raise DirectReadError(trace)
        trace['pre_settle_delay_s'] = LAUNCH_PRE_SETTLE_S
        _sleep(LAUNCH_PRE_SETTLE_S)
        if _clock() >= deadline:
            trace['failure'] = 'absolute_deadline_exceeded'
            trace.update(elapsed_s=_clock() - start, ok=False)
            evidence_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
            raise DirectReadError(trace)

    answer = None
    for attempt in range(1, 3):
        if deadline - _clock() < MIN_ATTEMPT_BUDGET_S:
            break
        nonce = uuid.uuid4().hex
        remote_jar = '/data/local/tmp/ai-sandbox-direct-' + nonce + '.jar'
        remote_xml = '/sdcard/Download/ai-sandbox-direct-' + nonce + '.xml'
        remote_sidecar = remote_xml + '.failure.bin'
        save_failure = bool(is_launch and attempt == 2)
        attempt_start = _clock()
        row = {'attempt': attempt, 'nonce': nonce, 'start_monotonic': attempt_start}
        trace['attempts'].append(row)
        try:
            checked(['push', str(jar_path), remote_jar], 8, row)
            digest = checked(['shell', 'sha256sum', remote_jar], 8, row).split()
            if not digest or digest[0] != expected_jar_sha256:
                raise ValueError('device jar hash mismatch')
            remaining = deadline - _clock()
            shell_seconds = math.floor(min(8, remaining) - 0.25)
            if shell_seconds < 1:
                raise TimeoutError('insufficient time to start owned collector')
            collector_args = ['shell', 'timeout', '-s', 'KILL', str(shell_seconds), 'env',
                                            'CLASSPATH=' + remote_jar + ':/system/framework/uiautomator.jar',
                                            'app_process', '/system/bin', 'StableUiRead', remote_xml]
            if is_launch:
                collector_args.append('--sliding-ms=' + str(min(6000, max(1000, shell_seconds*1000-1000))))
            if save_failure:
                collector_args.append('--failure-snapshots')
            rc_app, raw_app, err_app = run(collector_args, 8, row)
            if rc_app == 0:
                data, proof = validate_report(raw_app, nonce, expect_sliding=is_launch)
                row['proof'] = proof
                answer = data
            else:
                err_classified = False
                try:
                    r_err = json.loads(raw_app)
                    if isinstance(r_err, dict) and r_err.get('version') == 1 and r_err.get('nonce') == nonce:
                        collector_err = str(r_err.get('error', ''))
                        row['failure'] = f'collector refused: {collector_err[:1500]}'
                        row['is_transient'] = is_transient_error(collector_err)
                        err_classified = True
                        if save_failure:
                            preserve_failure(r_err, nonce, remote_sidecar, row)
                except Exception:
                    pass
                if not err_classified:
                    row['failure'] = f'command failed: {rc_app} {err_app[:400]}'
                    row['is_transient'] = False
        except Exception as exc:
            row['failure'] = str(exc)[:1600]
            row['is_transient'] = is_transient_error(row['failure'])
        finally:
            try:
                cleanup_paths = [remote_jar, remote_xml]
                if save_failure:
                    cleanup_paths.append(remote_sidecar)
                rc, _, _ = run(['shell', 'rm', '-f', *cleanup_paths], 2, row)
                if rc != 0:
                    raise ValueError('owned collector cleanup failed')
            except Exception as exc:
                row['cleanup_failure'] = str(exc)[:500]
                answer = None
            attempt_end = _clock()
            row['end_monotonic'] = attempt_end
            row['elapsed_s'] = round(attempt_end - attempt_start, 4)
            for c in reversed(row.get('commands', [])):
                if 'StableUiRead' in c.get('args', []):
                    row['collector_start_monotonic'] = c.get('start_monotonic')
                    row['collector_end_monotonic'] = c.get('end_monotonic')
                    row['collector_elapsed_s'] = c.get('elapsed_s')
                    break

        if answer is not None and _clock() <= deadline:
            break
        answer = None

        # Launch inter-attempt pacing after first attempt failure (only if transient and is_launch=True)
        if attempt == 1 and is_launch:
            if row.get('is_transient', False):
                rem_before_pacing = deadline - _clock()
                if rem_before_pacing < LAUNCH_INTER_ATTEMPT_PACING_S + MIN_ATTEMPT_BUDGET_S:
                    row['pacing_refused'] = 'insufficient_budget_for_inter_attempt_pacing'
                    break
                row['pacing_delay_s'] = LAUNCH_INTER_ATTEMPT_PACING_S
                _sleep(LAUNCH_INTER_ATTEMPT_PACING_S)
                if _clock() >= deadline:
                    break
            # If not transient: do NOT add launch pacing delay; attempt 2 proceeds if budget allows.

    trace.update(elapsed_s=_clock() - start, ok=answer is not None)
    # Serialize proof after cleanup and reject any result arriving outside the same deadline.
    evidence_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    if answer is not None and _clock() <= deadline:
        return answer, str(evidence_path)
    trace['ok'] = False
    if _clock() > deadline:
        trace['failure'] = 'absolute_deadline_exceeded'
    evidence_path.write_text(json.dumps(trace, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    raise DirectReadError(trace)
