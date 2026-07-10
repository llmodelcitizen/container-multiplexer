from __future__ import annotations

import collections
import contextlib
import io
import unittest
from unittest import mock

from tests.support import load_cm


FakeVersion = collections.namedtuple("FakeVersion", ["major", "minor"])


class PythonVersionGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cm = load_cm("cm_python_version_test")

    def test_main_rejects_python_older_than_39(self) -> None:
        err = io.StringIO()
        with mock.patch.object(self.cm.sys, "version_info", FakeVersion(3, 8)):
            with contextlib.redirect_stderr(err):
                result = self.cm.main()

        self.assertEqual(result, 1)
        self.assertIn("3.9", err.getvalue())
