from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import unittest
from pathlib import Path
from unittest import mock


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
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cm.run_parallel(worker, [1, 2, 3])

        out = stdout.getvalue()
        err = stderr.getvalue()
        self.assertFalse(result)
        # Successes stay on stdout; the "Errors:" block goes to stderr.
        self.assertIn("started instance 2", out)
        self.assertNotIn("Errors:", out)
        self.assertLess(err.index("Errors:"), err.index("failed instance 1"))
        self.assertLess(err.index("Errors:"), err.index("Instance 3: unexpected error: boom"))

    def test_run_parallel_reports_system_exit_without_discarding_results(self):
        cm = load_cm()

        def worker(n: int):
            if n == 1:
                return (n, True, "started instance 1")
            raise SystemExit("authorized_keys missing")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = cm.run_parallel(worker, [1, 2])

        out = stdout.getvalue()
        err = stderr.getvalue()
        self.assertFalse(result)
        self.assertIn("started instance 1", out)
        self.assertIn("Errors:", err)
        self.assertIn(
            "Instance 2: unexpected exit: authorized_keys missing",
            err,
        )

    def test_run_parallel_prints_collected_results_on_keyboard_interrupt(self):
        cm = load_cm()

        def worker(n: int):
            return (n, True, f"started instance {n}")

        def interrupted_as_completed(futures):
            futures = list(futures)
            yield futures[0]
            raise KeyboardInterrupt

        stdout = io.StringIO()
        with mock.patch.object(cm, "as_completed", interrupted_as_completed), \
                contextlib.redirect_stdout(stdout):
            result = cm.run_parallel(worker, [1, 2, 3])

        output = stdout.getvalue()
        self.assertFalse(result)
        self.assertIn("started instance", output)
        self.assertIn("Interrupted; run 'cm list' to inspect instance state.", output)


if __name__ == "__main__":
    unittest.main()
