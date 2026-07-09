from __future__ import annotations

import importlib.machinery
import importlib.util
import types
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class FakeNotFound(Exception):
    pass


class FakeAPIError(Exception):
    pass


class FakeDockerException(Exception):
    pass


def load_cm(module_name: str = "cm_under_test"):
    loader = importlib.machinery.SourceFileLoader(module_name, str(ROOT / "cm.py"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    install_fake_docker_sdk(module)
    return module


def install_fake_docker_sdk(module) -> None:
    module._docker_sdk = types.SimpleNamespace(
        errors=types.SimpleNamespace(
            APIError=FakeAPIError,
            DockerException=FakeDockerException,
            NotFound=FakeNotFound,
        )
    )


def managed_labels() -> dict[str, str]:
    return {"cm.managed": "true"}


class FakeImage:
    def __init__(self, image_id: str | None = "sha256:latest", created: str | None = None):
        self.id = image_id
        self.attrs: dict[str, Any] = {}
        if created is not None:
            self.attrs["Created"] = created


class FakeImages:
    def __init__(
        self,
        image_id: str | None = "sha256:latest",
        *,
        exc: Exception | None = None,
        images: dict[str, Any] | None = None,
    ):
        self.image_id = image_id
        self.exc = exc
        self.images = images or {}
        self.get_names: list[str] = []

    def get(self, name: str):
        self.get_names.append(name)
        if name in self.images:
            image = self.images[name]
            if isinstance(image, Exception):
                raise image
            return image
        if self.exc is not None:
            raise self.exc
        return FakeImage(self.image_id)


class FakeContainer:
    def __init__(
        self,
        name: str = "cm-001",
        *,
        status: str = "running",
        labels: dict[str, str] | None = None,
        image_id: str | None = "sha256:old",
        attrs: dict[str, Any] | None = None,
        registry: dict[str, "FakeContainer"] | None = None,
        logs_output: Any = b"",
    ):
        self.name = name
        self.status = status
        self.registry = registry
        self.logs_output = logs_output
        self.started = 0
        self.stopped = 0
        self.removed = 0
        self.renames: list[str] = []
        self.remove_calls: list[dict[str, Any]] = []
        self.log_calls: list[dict[str, Any]] = []
        self.attrs = attrs or {
            "Image": image_id,
            "State": {"Status": status},
            "Config": {"Labels": labels if labels is not None else managed_labels(), "Env": []},
        }

    @property
    def labels(self):
        return self.attrs.get("Config", {}).get("Labels", {})

    def start(self):
        self.started += 1
        self.status = "running"
        self.attrs.setdefault("State", {})["Status"] = "running"

    def stop(self):
        self.stopped += 1
        self.status = "exited"
        self.attrs.setdefault("State", {})["Status"] = "exited"

    def rename(self, name: str):
        if self.registry is not None:
            self.registry.pop(self.name, None)
        self.name = name
        self.renames.append(name)
        if self.registry is not None:
            self.registry[name] = self

    def remove(self, **kwargs):
        self.removed += 1
        self.remove_calls.append(dict(kwargs))
        if self.registry is not None:
            self.registry.pop(self.name, None)

    def logs(self, **kwargs):
        self.log_calls.append(dict(kwargs))
        output = self.logs_output
        if callable(output):
            return output(**kwargs)
        return output


RunEffect = Callable[["FakeContainers", str, dict[str, Any]], Any]


class FakeContainers:
    def __init__(
        self,
        registry: dict[str, FakeContainer] | None = None,
        *,
        image_id: str | None = "sha256:latest",
        run_effects: list[RunEffect | Exception] | None = None,
    ):
        self.registry = registry if registry is not None else {}
        self.image_id = image_id
        self.run_effects = list(run_effects or [])
        self.get_names: list[str] = []
        self.run_calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, name: str):
        self.get_names.append(name)
        if name not in self.registry:
            raise FakeNotFound(name)
        return self.registry[name]

    def run(self, image: str, **kwargs):
        self.run_calls.append((image, kwargs))
        if self.run_effects:
            effect = self.run_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect(self, image, kwargs)
        return self.create_running(kwargs)

    def create_running(self, kwargs: dict[str, Any]) -> FakeContainer:
        name = kwargs["name"]
        container = FakeContainer(
            name,
            status="running",
            labels=kwargs.get("labels"),
            image_id=self.image_id,
            registry=self.registry,
        )
        self.registry[name] = container
        return container


class FakeAPI:
    def __init__(self, summaries: list[dict[str, Any]] | None = None):
        self.summaries = summaries or []
        self.calls: list[dict[str, Any]] = []

    def containers(self, **kwargs):
        self.calls.append(dict(kwargs))
        return list(self.summaries)


class FakeClient:
    def __init__(
        self,
        registry: dict[str, FakeContainer] | None = None,
        *,
        summaries: list[dict[str, Any]] | None = None,
        image_id: str | None = "sha256:latest",
        image_exc: Exception | None = None,
        images: dict[str, Any] | None = None,
        run_effects: list[RunEffect | Exception] | None = None,
    ):
        self.registry = registry if registry is not None else {}
        self.containers = FakeContainers(
            self.registry,
            image_id=image_id,
            run_effects=run_effects,
        )
        self.images = FakeImages(image_id, exc=image_exc, images=images)
        self.api = FakeAPI(summaries)
        self.closed = 0

    def close(self):
        self.closed += 1


def make_failed_port_container_then_raise(message: str) -> RunEffect:
    def effect(containers: FakeContainers, image: str, kwargs: dict[str, Any]):
        containers.create_running(kwargs)
        raise FakeAPIError(message)

    return effect
