import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from verify_baseline import audit, STAGES


class IndependentAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.run = Path(self.temp.name) / "SYNTHETIC-run"
        self.run.mkdir()
        self.fields = ["timestamp_utc", "elapsed_ms", "run_id", "route", "stage", "total_physical_bytes", "available_physical_bytes", "used_physical_bytes", "error"]
        stats = {}
        for index, (stage, duration) in enumerate(STAGES.items()):
            start = datetime(2026, 10, 15, tzinfo=timezone.utc) + timedelta(hours=index)
            rows = [[(start + timedelta(seconds=i)).isoformat(), i * 1000, self.run.name, "emulator", stage, 10 * 2**30, 5 * 2**30, 5 * 2**30, ""] for i in range(duration or 2)]
            self.write_stage(stage, rows)
            stats[stage] = {k + "_used_bytes": 5 * 2**30 for k in ("median", "p95", "peak")}
        summary = {"all_runs_complete": True, "runs": [{"run_id": self.run.name, "route": "emulator", "stages": stats}]}
        (self.run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        inter = self.run / "interaction"
        inter.mkdir()
        ops = [{"op": i + 1, "ok": True, "echo_ok": True, "content_ok": True, "latency_ms_total": 1000} for i in range(20)]
        (inter / "ops.jsonl").write_text("\n".join(json.dumps(op) for op in ops), encoding="utf-8")
        (inter / "ops_summary.json").write_text(json.dumps({"ops_requested": 20, "ops_ok": 20, "ops_failed": 0, "ops_echo_ok": 20, "ops_content_ok": 20}), encoding="utf-8")

    def write_stage(self, stage, rows):
        with self.stage_path(stage).open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            writer.writerow(self.fields)
            writer.writerows(rows)

    def stage_path(self, stage):
        return self.run / ("memory_%s_emulator_%s_SYNTHETIC.csv" % (self.run.name, stage))

    def tearDown(self):
        self.temp.cleanup()

    def test_full_synthetic_run_and_unproven_op_window(self):
        result = audit(self.run)
        self.assertTrue(result["data_integrity_pass"], result["errors"])
        self.assertEqual(result["operation_absolute_window_coverage"], "unverified")

    def test_complete_flag_cannot_hide_a_short_stage(self):
        path = self.stage_path("app")
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join(lines[:101]), encoding="utf-8")
        result = audit(self.run)
        self.assertFalse(result["data_integrity_pass"])
        self.assertIn("app: prescribed sampling coverage missing", result["errors"])

    def test_memory_identity_failure_not_hidden_by_matching_summary(self):
        path = self.stage_path("idle")
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(str(10 * 2**30), str(9 * 2**30), 1), encoding="utf-8")
        result = audit(self.run)
        self.assertFalse(result["data_integrity_pass"])
        self.assertTrue(any("invalid physical-memory identity" in item for item in result["errors"]))

    def test_summary_cannot_claim_success_for_missing_op(self):
        path = self.run / "interaction/ops.jsonl"
        path.write_text("\n".join(path.read_text(encoding="utf-8").splitlines()[:-1]), encoding="utf-8")
        result = audit(self.run)
        self.assertFalse(result["data_integrity_pass"])
        self.assertIn("ops summary and actual records disagree", result["errors"])


if __name__ == "__main__":
    unittest.main()
