from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_cm():
    loader = importlib.machinery.SourceFileLoader("cm_parallel_test", str(ROOT / "cm.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class ParallelOutputTests(unittest.TestCase):
    def test_run_parallel_prints_errors_after_success_section(self):
        cm = load_cm()

        def worker(n: int):
            if n == 1:
                return (n, False, "failed instance 1")
            if n == 2:
                return (n, True, "started instance 2")
            raise RuntimeError("boom")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = cm.run_parallel(worker, [1, 2, 3])

        output = stdout.getvalue()
        self.assertFalse(result)
        self.assertLess(output.index("started instance 2"), output.index("Errors:"))
        self.assertLess(output.index("Errors:"), output.index("failed instance 1"))
        self.assertLess(output.index("Errors:"), output.index("Instance 3: unexpected error: boom"))

    def test_run_parallel_reports_system_exit_without_discarding_results(self):
        cm = load_cm()

        def worker(n: int):
            if n == 1:
                return (n, True, "started instance 1")
            raise SystemExit("authorized_keys missing")

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = cm.run_parallel(worker, [1, 2])

        output = stdout.getvalue()
        self.assertFalse(result)
        self.assertIn("started instance 1", output)
        self.assertIn("Errors:", output)
        self.assertIn(
            "Instance 2: unexpected exit: authorized_keys missing",
            output,
        )


if __name__ == "__main__":
    unittest.main()
