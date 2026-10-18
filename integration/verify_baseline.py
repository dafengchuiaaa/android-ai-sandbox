"""Read-only independent audit of the completed baseline; no imports of its code."""
import argparse
import csv
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from statistics import median, quantiles
import sys

ROOT = Path(__file__).resolve().parents[1]
STAGES = {"baseline": 300, "boot": None, "idle": 600, "app": 900, "interaction": 300, "reclaim": 300}
GIB = 2 ** 30


def utc(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("timestamp lacks UTC offset")
    return parsed.astimezone(timezone.utc)


def audit(run_dir):
    run_dir = Path(run_dir)
    run_id = run_dir.name
    errors, warnings, stages, hashes = [], [], {}, {}

    def remember(path):
        hashes[str(path.relative_to(run_dir))] = hashlib.sha256(path.read_bytes()).hexdigest()

    for stage, seconds in STAGES.items():
        files = sorted(run_dir.glob("memory_%s_emulator_%s_*.csv" % (run_id, stage)))
        if len(files) != 1:
            errors.append("%s: expected exactly one memory CSV, got %s" % (stage, len(files)))
            continue
        path = files[0]
        remember(path)
        values, times, elapsed, row_errors = [], [], [], []
        with path.open(encoding="utf-8-sig", newline="") as stream:
            for line, row in enumerate(csv.DictReader(stream), 2):
                try:
                    if row["run_id"] != run_id or row["route"] != "emulator" or row["stage"] != stage:
                        raise ValueError("wrong run/route/stage")
                    if row["error"].strip():
                        raise ValueError("recorded sample error: " + row["error"])
                    total, available, used = (int(row[k]) for k in ("total_physical_bytes", "available_physical_bytes", "used_physical_bytes"))
                    tick = int(row["elapsed_ms"])
                    stamp = utc(row["timestamp_utc"])
                    if not (total > 0 and 0 <= available <= total and used == total - available and tick >= 0):
                        raise ValueError("invalid physical-memory identity or elapsed time")
                    if elapsed and (tick <= elapsed[-1] or stamp <= times[-1]):
                        raise ValueError("duplicate/backward time")
                    if elapsed and tick - elapsed[-1] > 2500:
                        raise ValueError("gap exceeds 2.5 sampling intervals")
                    values.append(used)
                    times.append(stamp)
                    elapsed.append(tick)
                except (KeyError, ValueError, TypeError) as exc:
                    row_errors.append("line %d: %s" % (line, exc))
        if row_errors:
            errors.extend("%s: %s" % (stage, item) for item in row_errors)
        if len(values) < 2:
            errors.append(stage + ": fewer than two valid samples")
            continue
        wall_span = (times[-1] - times[0]).total_seconds()
        mono_span = (elapsed[-1] - elapsed[0]) / 1000
        if abs(wall_span - mono_span) > 1:
            errors.append(stage + ": UTC and monotonic spans diverge by more than 1s")
        if seconds and (wall_span < seconds - 1.5 or mono_span < seconds - 1.5 or len(values) < seconds - 1):
            errors.append(stage + ": prescribed sampling coverage missing")
        stages[stage] = {"samples": len(values), "error_samples": len(row_errors), "utc_start": times[0].isoformat(),
                         "utc_end": times[-1].isoformat(), "span_seconds": wall_span,
                         "median_bytes": median(values), "p95_bytes": quantiles(values, n=100, method="inclusive")[94],
                         "peak_bytes": max(values)}
    baseline = stages.get("baseline", {}).get("median_bytes")
    if baseline is not None:
        for item in stages.values():
            for field in ("median", "p95", "peak"):
                item[field + "_gib"] = item[field + "_bytes"] / GIB
                item["delta_" + field + "_gib"] = (item[field + "_bytes"] - baseline) / GIB

    summary_path = run_dir / "summary.json"
    remember(summary_path)
    summary = json.loads(summary_path.read_text(encoding="utf-8-sig"))
    reports = summary.get("runs", [])
    if summary.get("all_runs_complete") is not True or len(reports) != 1 or reports[0].get("run_id") != run_id or reports[0].get("route") != "emulator":
        errors.append("summary identity/completeness mismatch")
    elif baseline is not None:
        for stage, actual in stages.items():
            reported = reports[0].get("stages", {}).get(stage, {})
            for field in ("median", "p95", "peak"):
                expected = reported.get(field + "_used_bytes")
                if not isinstance(expected, (int, float)) or abs(actual[field + "_bytes"] - expected) > 0.01:
                    errors.append("%s %s differs from reported summary" % (stage, field))

    ops_path = run_dir / "interaction" / "ops.jsonl"
    ops_summary_path = run_dir / "interaction" / "ops_summary.json"
    remember(ops_path)
    remember(ops_summary_path)
    ops = [json.loads(line) for line in ops_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    op_summary = json.loads(ops_summary_path.read_text(encoding="utf-8-sig"))
    ok = sum(op.get("ok") is True for op in ops)
    echo_ok = sum(op.get("echo_ok") is True for op in ops)
    content_ok = sum(op.get("content_ok") is True for op in ops)
    actual_counts = {"ops_requested": len(ops), "ops_ok": ok, "ops_failed": len(ops) - ok, "ops_echo_ok": echo_ok, "ops_content_ok": content_ok}
    if len(ops) != 20 or [op.get("op") for op in ops] != list(range(1, 21)):
        errors.append("expected exactly 20 distinct ordered operations")
    if any(op_summary.get(key) != count for key, count in actual_counts.items()):
        errors.append("ops summary and actual records disagree")
    if ok != 20:
        errors.append("not all operations succeeded")
    has_absolute_times = all(isinstance(op.get("started_utc"), str) and isinstance(op.get("ended_utc"), str) for op in ops)
    cumulative_ms = sum(op.get("latency_ms_total", 0) for op in ops)
    warnings.append("Operation records lack absolute timestamps; cumulative duration cannot prove containment in the sampling window." if not has_absolute_times else "Absolute operation times need explicit comparison to the sampling window.")
    warnings.extend(["Host-wide increments include concurrent activity; not emulator-exclusive allocation.",
                     "One run does not establish repeatability; a reclaim delta alone does not establish a leak.",
                     "Twenty successful log records do not by themselves validate the underlying content classifier."])
    return {"generator": "codex-independent-baseline-audit", "run_id": run_id,
            "data_integrity_pass": not errors, "errors": errors, "warnings": warnings,
            "stages": stages, "operation_counts": actual_counts, "cumulative_op_seconds": cumulative_ms / 1000,
            "operation_absolute_window_coverage": "unverified", "source_sha256": hashes,
            "method": "statistics.median and statistics.quantiles(method='inclusive'); no original audit/statistics module imported"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, default=ROOT / "departments/workbuddy/integration/runs/p1-01-r2")
    args = parser.parse_args(argv)
    try:
        result = audit(args.run_dir)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        result = {"data_integrity_pass": False, "errors": [str(exc)]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["data_integrity_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
