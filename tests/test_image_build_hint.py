from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_cm():
    loader = importlib.machinery.SourceFileLoader("cm_image_hint_test", str(ROOT / "cm.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class FakeNotFound(Exception):
    pass


class FakeContainers:
    def get(self, name):
        raise FakeNotFound(name)


class FakeImages:
    def get(self, name):
        raise FakeNotFound(name)


class FakeClient:
    def __init__(self):
        self.containers = FakeContainers()
        self.images = FakeImages()


class ImageBuildHintTests(unittest.TestCase):
    def test_start_missing_runtime_image_prints_two_step_build_hint(self):
        cm = load_cm()
        cm._docker_sdk = types.SimpleNamespace(
            errors=types.SimpleNamespace(
                APIError=Exception,
                DockerException=Exception,
                NotFound=FakeNotFound,
            )
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            cm.WORKSPACES_DIR = Path(temp_dir) / "workspaces"
            stdout = io.StringIO()

            with contextlib.redirect_stdout(stdout):
                result = cm.start_instance(FakeClient(), 1)

        output = stdout.getvalue()
        self.assertFalse(result)
        self.assertIn("Image 'cm:latest' not found.", output)
        self.assertIn("docker build -t cm-base:latest -f Dockerfile.base .", output)
        self.assertIn("docker build -t cm:latest .", output)
