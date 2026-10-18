"""Run the author's offline suite in Codex-owned temporary runtime paths."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def main():
    path = ROOT / "departments/antigravity/tests/test_phone.py"
    spec = importlib.util.spec_from_file_location("phone_author_tests", path)
    tests = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tests)
    with tempfile.TemporaryDirectory(dir=ROOT / "integration") as temp:
        test_root = Path(temp).resolve()
        if not test_root.is_relative_to((ROOT / "integration").resolve()):
            raise RuntimeError("test runtime outside intended workspace")
        os_temp = test_root / "temp"
        os_temp.mkdir()
        tests.PROJECT_ROOT = test_root
        with patch.object(tests.phone, "APPROVED_OUTPUT_ROOT", str(test_root / "departments/antigravity/runtime")), patch.object(tests.phone, "APPROVED_STATE_ROOT", str(test_root / "departments/workbuddy/runtime")), patch.object(tempfile, "tempdir", str(os_temp)):
            suite = unittest.defaultTestLoader.loadTestsFromModule(tests)
            result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
