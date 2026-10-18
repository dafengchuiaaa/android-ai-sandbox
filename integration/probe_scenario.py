"""Finite offline negative probes of a frozen scenario revision; no CLI/device calls."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / "departments/deepseek/scenarios/fdroid_search.py"
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    spec = importlib.util.spec_from_file_location("scenario_under_review", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    probes = []
    for name, overrides in (
        ("positive count but empty candidate list", {"real_result_candidates": []}),
        ("unsupported page reported as verified", {"page_kind": "unsupported_app"}),
        ("boolean result count", {"result_count": True}),
    ):
        data = {"content_verified": True, "input_matches_query": True, "empty_state": False,
                "result_count": 1, "page_kind": "search_results", "network_verified": "unknown",
                "observed_at": "2026-10-15T09:00:00Z", "real_result_candidates": [{"label": "Termux"}]}
        data.update(overrides)
        outcome, _, _ = module._verdict(data, "termux")
        probes.append({"case": name, "input": data, "actual_outcome": outcome,
                       "rejected_false_success": outcome != "verified"})
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {"source_sha256_before": before, "source_sha256_after": after,
              "same_revision": before == after, "offline_function_probes_only": True, "probes": probes,
              "all_false_successes_rejected": all(p["rejected_false_success"] for p in probes)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["same_revision"] and result["all_false_successes_rejected"] else 1


if __name__ == "__main__":
    sys.exit(main())
