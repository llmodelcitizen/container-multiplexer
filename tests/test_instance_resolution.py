from __future__ import annotations

import unittest

from tests.support import FakeClient, load_cm


class InstanceResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_instance_resolution_test")

    def test_parse_instances_accepts_ranges_reversed_ranges_and_dedupes(self) -> None:
        self.assertEqual(self.cm.parse_instances(["3", "1-2", "5-4", "2"]), [1, 2, 3, 4, 5])

    def test_parse_instances_rejects_invalid_range(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.cm.parse_instances(["1-two"])

        self.assertEqual(str(ctx.exception), "Invalid range: 1-two")

    def test_parse_instances_rejects_all_mixed_with_numbers(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            self.cm.parse_instances(["all", "1"])

        self.assertEqual(str(ctx.exception), "Invalid instance: all")

    def test_parse_instances_rejects_non_positive_and_too_large(self) -> None:
        with self.subTest("zero"):
            with self.assertRaises(SystemExit) as ctx:
                self.cm.parse_instances(["0"])
            self.assertEqual(str(ctx.exception), "Instance 0 must be positive")

        with self.subTest("too large"):
            with self.assertRaises(SystemExit) as ctx:
                self.cm.parse_instances([str(self.cm.MAX_INSTANCE + 1)])
            self.assertEqual(
                str(ctx.exception),
                f"Instance {self.cm.MAX_INSTANCE + 1} exceeds maximum ({self.cm.MAX_INSTANCE})",
            )

    def test_get_running_instances_uses_real_summary_parser(self) -> None:
        client = FakeClient(
            summaries=[
                {"Names": ["/cm-003"]},
                {"Names": ["/not-cm"]},
                {"Names": []},
                {"Names": ["/cm-001"]},
                {"Names": ["/cm-bad"]},
            ]
        )

        self.assertEqual(self.cm.get_running_instances(client), [1, 3])
        self.assertEqual(
            client.api.calls,
            [{"filters": {"label": "cm.managed=true", "status": "running"}}],
        )

    def test_get_dead_instances_filters_running_and_bad_names(self) -> None:
        client = FakeClient(
            summaries=[
                {"Names": ["/cm-003"], "State": "exited"},
                {"Names": ["/cm-002"], "State": "running"},
                {"Names": ["/cm-001"], "State": "created"},
                {"Names": ["/cm-bad"], "State": "exited"},
                {"Names": [], "State": "exited"},
            ]
        )

        self.assertEqual(self.cm.get_dead_instances(client), [1, 3])
        self.assertEqual(
            client.api.calls,
            [{"all": True, "filters": {"label": "cm.managed=true"}}],
        )


if __name__ == "__main__":
    unittest.main()
