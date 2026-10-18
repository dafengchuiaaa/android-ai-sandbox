"""Optional failure metadata must never turn a refusal into an observation."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'departments/antigravity'))
import stable_ui_read as reader

NONCE = '1' * 32
XML = b'<hierarchy><node package="org.fdroid.basic" /></hierarchy>'
DIGEST = hashlib.sha256(XML).hexdigest()


def metadata():
    return [{'index': i, 'window_id': i, 'package': 'org.fdroid.basic', 'sha256': DIGEST,
             'xml_bytes': len(XML), 'taken_ms': i*400, 'cache_cleared': True} for i in (1, 2)]


def success():
    return {'version': 1, 'nonce': NONCE, 'ok': True, 'stable': True, 'snapshot_count': 2,
            'snapshot_gap_ms': 400, 'cache_clear': [True, True], 'window_id': 1,
            'package': 'org.fdroid.basic', 'sha256_first': DIGEST, 'sha256_second': DIGEST,
            'xml_b64': base64.b64encode(XML).decode()}


class DiagnosticsCompatibility(unittest.TestCase):
    def test_refusal_stays_refusal_despite_matching_metadata(self):
        for error in ['window_changed', 'unstable_tree', 'null_root_after_bounded_readiness']:
            payload = success()
            payload.update(ok=False, error='java.lang.IllegalStateException: '+error, snapshot_diagnostics=metadata())
            with self.subTest(error=error), self.assertRaises(ValueError):
                reader.validate_report(json.dumps(payload), NONCE)

    def test_diagnostic_hashes_cannot_override_authoritative_mismatch(self):
        payload = success()
        payload.update(sha256_second='f'*64, snapshot_diagnostics=metadata())
        with self.assertRaises(ValueError): reader.validate_report(json.dumps(payload), NONCE)

    def test_existing_success_contract_remains_compatible(self):
        data, proof = reader.validate_report(json.dumps(success()), NONCE)
        self.assertEqual(data, XML)
        self.assertEqual(proof['sha256_first'], DIGEST)

    def test_two_metadata_records_fit_existing_failure_stdout_bound(self):
        payload = {'version': 1, 'nonce': NONCE, 'idle_result': 'short_idle_reached', 'ok': False,
                   'error': 'java.lang.IllegalStateException: unstable_tree', 'cause': 'null',
                   'elapsed_ms': 2000, 'snapshot_diagnostics': metadata()}
        self.assertLess(len(json.dumps(payload)), 4096)
        self.assertNotIn('xml_b64', payload)


if __name__ == '__main__':
    unittest.main()
