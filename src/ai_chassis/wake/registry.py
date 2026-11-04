"""Call-time config registry. One broken plugin cannot disable its siblings."""

import importlib
import inspect
from pathlib import Path

from .channel import Admission, DeliveryChannel
from .configuration import SELECTORS, resolve
from .deadlines import DeadlineSet
from .route import DoorbellHandler, Route
from .source import EventSource

_PACKAGE = "ai_chassis.wake"
DEFAULT_CONFIG = Path(__file__).resolve().parents[3] / "config" / "wake-dispatch.toml"


class Registry:
    def __init__(self, path=None, *, environ=None):
        self.path = Path(path) if path is not None else DEFAULT_CONFIG
        self.environ = environ
        self.status = {}
        self.source_context = {}

    def read(self):
        document, _ = resolve(self.path, self.environ)
        if not document:
            self.status["config"] = "degraded:invalid-config"
        return document

    def selectors(self):
        document, layers = resolve(self.path, self.environ)
        return {
            name: {
                "enabled": document.get(name, {}).get("enabled") is True,
                "layer": layers[name],
            }
            for name in SELECTORS
        }

    def enabled(self):
        section = self.read().get("dispatch", {})
        return isinstance(section, dict) and section.get("enabled") is True

    def construct(self, table, base, **context):
        if not isinstance(table, dict) or set(table) - {"class", "handler", "options"}:
            raise ValueError("malformed-table")
        dotted = table.get("class", table.get("handler"))
        options = table.get("options", {})
        if not isinstance(dotted, str) or not isinstance(options, dict):
            raise ValueError("malformed-table")
        module, _, name = dotted.replace(":", ".").rpartition(".")
        if module != _PACKAGE and not module.startswith(_PACKAGE + "."):
            raise ValueError("outside-package")
        # The installer's daemon fingerprint covers every module under ``ai_chassis.wake``.
        loaded = importlib.import_module(
            module
        )  # daemon-fingerprint: checkout-package ai_chassis.wake
        cls = getattr(loaded, name)
        if not isinstance(cls, type) or not issubclass(cls, base):
            raise ValueError("invalid-base")
        # Config options cannot replace host-owned paths, locks or state bindings.
        if options.keys() & context.keys():
            raise ValueError("reserved-option")
        parameters = inspect.signature(cls).parameters
        supplied = {key: value for key, value in context.items() if key in parameters}
        return cls(**supplied, **options)

    def load(self, category, **context):
        bases = {
            "sources": EventSource,
            "doorbells": DoorbellHandler,
            "routes": Route,
            "admissions": Admission,
            "channels": DeliveryChannel,
            "deadlines": DeadlineSet,
        }
        tables = self.read().get(category, {})
        loaded = {}
        self.status = {
            key: value
            for key, value in self.status.items()
            if not key.startswith(category + ".")
        }
        if not isinstance(tables, dict):
            self.status[category] = "degraded:malformed-table"
            return loaded
        for name, table in tables.items():
            try:
                bindings = context | (
                    self.source_context.get(name, {}) if category == "sources" else {}
                )
                loaded[name] = self.construct(table, bases[category], **bindings)
            except Exception as exc:
                self.status[f"{category}.{name}"] = f"degraded:{type(exc).__name__}"
        return loaded

    def dispatcher(self, **context):
        from .dispatcher import HostDispatcher

        table = dict(self.read().get("dispatch", {}))
        table.pop("enabled", None)
        return self.construct(table, HostDispatcher, registry=self, **context)
