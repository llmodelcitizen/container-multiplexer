from __future__ import annotations

import unittest
import warnings
from pathlib import Path
from unittest import mock

from tests.support import load_cm


class ImportSideEffectsTests(unittest.TestCase):
    """Executing cm.py must not mutate process-global state (issue #83)."""

    def test_executing_cm_leaves_warnings_filters_untouched(self) -> None:
        before = list(warnings.filters)

        load_cm("cm_import_side_effects_filters")

        self.assertEqual(list(warnings.filters), before)

    def test_urllib3_warning_filter_is_applied_when_cli_starts(self) -> None:
        cm = load_cm("cm_import_side_effects_main")
        original_argv = cm.sys.argv
        cm.cmd_version = lambda args: 0
        cm.sys.argv = ["cm", "version"]
        with warnings.catch_warnings():
            warnings.resetwarnings()
            try:
                result = cm.main()
            finally:
                cm.sys.argv = original_argv
            patterns = [
                message.pattern
                for _, message, *_ in warnings.filters
                if message is not None
            ]

        self.assertEqual(result, 0)
        self.assertIn("urllib3 v2 only supports OpenSSL", patterns)

    def test_executing_cm_survives_unresolvable_home(self) -> None:
        with mock.patch.object(Path, "home", side_effect=RuntimeError("no home")):
            cm = load_cm("cm_import_side_effects_nohome")

        self.assertEqual(cm.CM_HOME.name, ".cm")
        self.assertEqual(cm.WORKSPACES_DIR, cm.CM_HOME / "workspaces")
        self.assertEqual(cm.AUTHORIZED_KEYS_PATH, cm.CM_HOME / "authorized_keys")

