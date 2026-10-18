"""Opt-in API35 collector. No input, at most two read attempts, one 20s deadline."""
import base64
import hashlib
import json
import math
from pathlib import Path
import time
import uuid
import xml.etree.ElementTree as ET

MAX_XML_BYTES = 2 * 1024 * 1024

class DirectReadError(Exception):
    def __init__(self, detail):
        super().__init__('Stable direct UI read failed; no input retried')
        self.detail = detail

def validate_report(raw, nonce):
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
    proof = {k:v for k,v in r.items() if k != 'xml_b64'}
    return data, proof

def collect(adb, jar_path, expected_jar_sha256, output_dir, total_timeout=20.0):
    start = time.monotonic()
    if not math.isfinite(total_timeout) or not 0 < total_timeout <= 20:
        raise DirectReadError({'reason':'invalid_budget', 'total_timeout_s':str(total_timeout)})
    deadline = start + total_timeout
    jar_path = Path(jar_path)
    if hashlib.sha256(jar_path.read_bytes()).hexdigest() != expected_jar_sha256:
        raise DirectReadError({'reason':'collector_jar_hash_mismatch'})
    trace = {'backend':'stable-direct', 'total_timeout_s':total_timeout, 'max_attempts':2, 'attempts':[]}
    token = uuid.uuid4().hex
    evidence_path = Path(output_dir) / ('stable-direct-' + token + '.json')
    def run(args, cap, row):
        left = deadline - time.monotonic()
        if left <= 0.05:
            raise TimeoutError('absolute read deadline exhausted')
        timeout = min(cap,left)
        entry = {'args':args, 'timeout_s':timeout};row.setdefault('commands',[]).append(entry)
        rc,out,err = adb.run(args,timeout=timeout)
        entry.update(rc=rc, stderr=err[:1200])
        if rc != 0:entry['failure_stdout'] = out[:4096]
        if time.monotonic() > deadline:
            raise TimeoutError('command returned past absolute deadline')
        return rc,out,err
    def checked(args,cap,row):
        rc,out,err=run(args,cap,row)
        if rc != 0:raise ValueError('command failed: '+str(rc)+' '+err[:400])
        return out
    answer = None
    for attempt in range(1,3):
        if deadline-time.monotonic() < 1.0:break
        nonce = uuid.uuid4().hex
        remote_jar = '/data/local/tmp/ai-sandbox-direct-' + nonce + '.jar'
        remote_xml = '/sdcard/Download/ai-sandbox-direct-' + nonce + '.xml'
        row = {'attempt':attempt,'nonce':nonce};trace['attempts'].append(row)
        try:
            checked(['push',str(jar_path),remote_jar],8,row)
            digest = checked(['shell','sha256sum',remote_jar],8,row).split()
            if not digest or digest[0] != expected_jar_sha256:raise ValueError('device jar hash mismatch')
            remaining = deadline-time.monotonic()
            shell_seconds = math.floor(min(8,remaining)-0.25)
            if shell_seconds < 1:raise TimeoutError('insufficient time to start owned collector')
            raw=checked(['shell','timeout','-s','KILL',str(shell_seconds),'env',
                         'CLASSPATH='+remote_jar+':/system/framework/uiautomator.jar',
                         'app_process','/system/bin','StableUiRead',remote_xml],8,row)
            data,proof=validate_report(raw,nonce)
            row['proof']=proof
            answer=data
        except Exception as exc:
            row['failure']=str(exc)[:1600]
        finally:
            try:
                rc,_,_=run(['shell','rm','-f',remote_jar,remote_xml],2,row)
                if rc != 0:raise ValueError('owned collector cleanup failed')
            except Exception as exc:
                row['cleanup_failure']=str(exc)[:500];answer=None
        if answer is not None and time.monotonic() <= deadline:break
        answer=None
    trace.update(elapsed_s=time.monotonic()-start,ok=answer is not None)
    # Serialize proof after cleanup and reject any result arriving outside the same deadline.
    evidence_path.write_text(json.dumps(trace,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    if answer is not None and time.monotonic() <= deadline:
        return answer,str(evidence_path)
    trace['ok']=False
    if time.monotonic()>deadline:trace['failure']='absolute_deadline_exceeded'
    evidence_path.write_text(json.dumps(trace,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    raise DirectReadError(trace)
