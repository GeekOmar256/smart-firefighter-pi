"""Settings that can be changed from the web page while the program runs.

Each module registers the module-level values it wants to expose, for example

    settings.register(sys.modules[__name__], "HUMAN_CONTRAST_C", "Contrast", ...)

The value already in the module (the code default, or the environment
variable it was read from) becomes the default. A value saved from the
settings page is written straight back into the module, so the code keeps
reading its plain module-level names and every change takes effect on the
next frame without a restart.

Saved values live in SETTINGS_FILE, outside the repository, so an "update"
(git reset --hard) never throws them away.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

SETTINGS_FILE = os.environ.get(
    "SETTINGS_FILE",
    str(Path.home() / ".config" / "smart-firefighter" / "settings.json"),
)

KINDS = ("float", "int", "bool", "str")


@dataclass
class Setting:
    module: ModuleType
    name: str
    label: str
    group: str
    kind: str
    default: object
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    help: str = ""
    extra: dict = field(default_factory=dict)

    def parse(self, raw: object) -> object:
        """Turn a value from the page into the right type, or raise ValueError."""
        if self.kind == "bool":
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in ("1", "true", "yes", "on"):
                return True
            if text in ("0", "false", "no", "off"):
                return False
            raise ValueError("must be on or off")

        if self.kind == "str":
            text = str(raw).strip()
            if len(text) > 100:
                raise ValueError("is too long")
            return text

        try:
            number = float(raw)
        except (TypeError, ValueError):
            raise ValueError("must be a number") from None
        if number != number or number in (float("inf"), float("-inf")):
            raise ValueError("must be a number")
        if self.minimum is not None and number < self.minimum:
            raise ValueError(f"must be at least {self.minimum:g}")
        if self.maximum is not None and number > self.maximum:
            raise ValueError(f"must be at most {self.maximum:g}")
        if self.kind == "int":
            if number != int(number):
                raise ValueError("must be a whole number")
            return int(number)
        return number

    def describe(self) -> dict:
        return {
            "name": self.name,
            "label": self.label,
            "group": self.group,
            "kind": self.kind,
            "value": getattr(self.module, self.name),
            "default": self.default,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "help": self.help,
            **self.extra,
        }


class SettingsStore:
    def __init__(self, path: str = SETTINGS_FILE) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._settings: dict[str, Setting] = {}
        self._saved: dict = self._load()

    def _load(self) -> dict:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        values = {name: getattr(s.module, s.name) for name, s in self._settings.items()
                  if getattr(s.module, s.name) != s.default}
        # keep values for settings this run did not register (another version)
        for name, value in self._saved.items():
            if name not in self._settings:
                values[name] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values, indent=2, sort_keys=True))
        temporary.replace(self.path)            # never leave a half-written file
        self._saved = values

    def register(
        self,
        module: ModuleType,
        name: str,
        label: str,
        group: str,
        kind: str,
        minimum: float | None = None,
        maximum: float | None = None,
        step: float | None = None,
        help: str = "",
        **extra,
    ) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown kind {kind}")
        setting = Setting(module, name, label, group, kind, getattr(module, name),
                          minimum, maximum, step, help, extra)
        with self._lock:
            self._settings[name] = setting
            if name in self._saved:
                try:
                    setattr(module, name, setting.parse(self._saved[name]))
                except ValueError as error:
                    print(f"[settings] ignoring saved {name}: {error}")

    def schema(self) -> list[dict]:
        with self._lock:
            return [s.describe() for s in self._settings.values()]

    def update(self, changes: dict) -> tuple[dict, dict]:
        """Apply what is valid. Returns (applied, errors), both by name."""
        applied, errors, parsed = {}, {}, {}
        with self._lock:
            for name, raw in changes.items():
                setting = self._settings.get(name)
                if setting is None:
                    errors[name] = "unknown setting"
                    continue
                try:
                    parsed[name] = setting.parse(raw)
                except ValueError as error:
                    errors[name] = f"{setting.label} {error}"
            for name, value in parsed.items():
                setting = self._settings[name]
                setattr(setting.module, name, value)
                applied[name] = value
            if applied:
                self._save()
        if applied:
            print(f"[settings] changed {', '.join(f'{k}={v}' for k, v in applied.items())}")
        return applied, errors

    def reset(self, group: str | None = None) -> None:
        with self._lock:
            for setting in self._settings.values():
                if group is None or setting.group == group:
                    setattr(setting.module, setting.name, setting.default)
            self._save()
        print(f"[settings] reset {group or 'everything'} to defaults")


settings = SettingsStore()
