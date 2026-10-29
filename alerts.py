"""Spoken sound alerts for the firefighter.

Every alert is a WAV file in sounds/: a short warning tone followed by a
spoken message, made once with tools/generate_sounds.py. Nothing is generated
while the system runs, so alerts work without an internet connection and with
no delay.

The AlertManager is given the current conditions twice a second (flame, gas,
person, ...). When a condition switches on, its sound plays. A critical
condition that stays on is repeated every CRITICAL_REPEAT_S so it is not
forgotten, and when the last critical condition clears, "hazard cleared"
plays. The same alert is never played twice within ALERT_COOLDOWN_S.

Sound is played with aplay, which is part of Raspberry Pi OS. The Pi 5 has no
headphone jack: use a USB speaker, an HDMI display with speakers, or a USB
sound card, and set ALSA_DEVICE (for example plughw:2,0, see "aplay -l") if
the default output is not the right one.
"""

from __future__ import annotations

import itertools
import queue
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

SOUND_DIR = Path(__file__).resolve().parent / "sounds"

# name: (level, title shown on the page)
EVENTS = {
    "flame": ("critical", "Flame detected"),
    "gas": ("critical", "Gas or smoke detected"),
    "fire_thermal": ("critical", "Fire seen by the thermal camera"),
    "high_heat": ("critical", "Dangerous heat around the firefighter"),
    "person": ("warning", "Person detected"),
    "camera_fault": ("warning", "Thermal camera not responding"),
    "device_overheat": ("warning", "Device overheating"),
    "all_clear": ("info", "Hazard cleared"),
    "system_ready": ("info", "System ready"),
}
LEVEL_ORDER = {"critical": 0, "warning": 1, "info": 2}

# Adjustable from the settings page (see _register_settings below)
SOUND_ENABLED = True
ALERT_COOLDOWN_S = 30.0         # the same alert is not played again sooner
CRITICAL_REPEAT_S = 20.0        # a critical alert still active is repeated; 0 = never
HIGH_HEAT_C = 60.0              # room temperature (from the thermal camera) for "high_heat"
DEVICE_OVERHEAT_C = 80.0        # Raspberry Pi CPU temperature for "device_overheat"
CAMERA_FAULT_S = 5.0            # seconds without a frame before "camera_fault"
ALSA_DEVICE = ""                # empty = the default output
STALE_AFTER_S = 10.0            # a queued alert older than this whose cause has gone is dropped

for _name in EVENTS:            # one on/off switch per alert: SOUND_FLAME, SOUND_GAS, ...
    globals()[f"SOUND_{_name.upper()}"] = True


def sound_path(event: str) -> Path:
    return SOUND_DIR / f"{event}.wav"


def cpu_temperature_c() -> float | None:
    try:
        return int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000.0
    except (OSError, ValueError):
        return None


def player_name() -> str | None:
    """Which program will play the sounds on this machine."""
    if sys.platform == "win32":
        return "winsound"
    for program in ("aplay", "pw-play", "paplay"):
        if shutil.which(program):
            return program
    return None


def play_file(path: Path) -> None:
    """Play one WAV file and wait until it has finished."""
    program = player_name()
    if program == "winsound":                    # for testing on a Windows laptop
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
        return
    if program is None:
        raise RuntimeError("no audio player found (install alsa-utils for aplay)")

    command = [program]
    if program == "aplay":
        command.append("-q")
        if ALSA_DEVICE.strip():
            command += ["-D", ALSA_DEVICE.strip()]
    elif program == "pw-play" and ALSA_DEVICE.strip():
        command += ["--target", ALSA_DEVICE.strip()]
    command.append(str(path))

    result = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{program} exited with {result.returncode}")


class AlertManager:
    def __init__(self) -> None:
        self._queue: queue.PriorityQueue = queue.PriorityQueue()
        self._order = itertools.count()
        self._queued: set[str] = set()
        self._last_played: dict[str, float] = {}
        self._active: set[str] = set()
        self._history: deque = deque(maxlen=40)
        self._lock = threading.Lock()
        self._listeners: list = []
        self.last_error = ""

        missing = [e for e in EVENTS if not sound_path(e).exists()]
        if missing:
            print(f"[alerts] missing sounds: {', '.join(missing)}. "
                  "Run tools/generate_sounds.py to make them.")
        print(f"[alerts] playing through {player_name() or 'NOTHING - no audio player found'}")

    # -- input -----------------------------------------------------------

    def on_alert(self, callback) -> None:
        """Call callback(event, level) whenever an alert is raised."""
        self._listeners.append(callback)

    def trigger(self, event: str, force: bool = False, reason: str = "") -> bool:
        """Raise an alert: log it, tell the listeners, and queue its sound
        unless it is muted. force=True skips the cooldown and the mute."""
        if event not in EVENTS:
            return False
        level = EVENTS[event][0]
        now = time.monotonic()

        with self._lock:
            if not force and now - self._last_played.get(event, -1e9) < ALERT_COOLDOWN_S:
                return False
            if event in self._queued:
                return False
            # A muted alert is still logged and still saves a picture; it is
            # only not played.
            audible = force or (SOUND_ENABLED and globals().get(f"SOUND_{event.upper()}", True))
            self._last_played[event] = now
            self._history.appendleft({
                "event": event,
                "level": level,
                "title": EVENTS[event][1],
                "time": time.time(),
                "reason": reason if audible else f"{reason}, muted".lstrip(", "),
            })
            if audible:
                self._queued.add(event)
                self._queue.put((LEVEL_ORDER[level], next(self._order), event, now))
        for callback in self._listeners:
            try:
                callback(event, level)
            except Exception as error:            # noqa: BLE001
                print(f"[alerts] listener failed: {error}")
        return True

    def update(self, conditions: dict[str, bool]) -> None:
        """Give the current state of every condition; plays what changed."""
        now = time.monotonic()
        active = {name for name, on in conditions.items() if on and name in EVENTS}

        for event in active - self._active:                      # just switched on
            self.trigger(event, reason="started")

        if CRITICAL_REPEAT_S > 0:                                # still on: remind
            for event in active & self._active:
                if EVENTS[event][0] == "critical" and \
                        now - self._last_played.get(event, 0) >= max(CRITICAL_REPEAT_S, ALERT_COOLDOWN_S):
                    self.trigger(event, reason="still active")

        was_critical = any(EVENTS[e][0] == "critical" for e in self._active)
        is_critical = any(EVENTS[e][0] == "critical" for e in active)
        if was_critical and not is_critical:
            self.trigger("all_clear", reason="hazards cleared")

        self._active = active

    def watch(self, get_conditions, interval: float = 0.5) -> None:
        """Check get_conditions() forever on a background thread."""
        def loop() -> None:
            while True:
                try:
                    self.update(get_conditions())
                except Exception as error:        # noqa: BLE001
                    print(f"[alerts] condition check failed: {error}")
                time.sleep(interval)
        threading.Thread(target=loop, daemon=True).start()

    # -- output ----------------------------------------------------------

    def start(self) -> None:
        threading.Thread(target=self._play_loop, daemon=True).start()
        self.trigger("system_ready", reason="startup")

    def _play_loop(self) -> None:
        while True:
            _, _, event, queued_at = self._queue.get()
            with self._lock:
                self._queued.discard(event)

            # Drop a warning whose cause has already gone while it waited
            # behind other sounds. Critical alerts always play.
            level = EVENTS[event][0]
            if level == "warning" and event not in self._active and \
                    time.monotonic() - queued_at > STALE_AFTER_S:
                continue

            path = sound_path(event)
            if not path.exists():
                self.last_error = f"missing {path.name}"
                continue
            try:
                play_file(path)
                self.last_error = ""
            except Exception as error:            # noqa: BLE001
                self.last_error = str(error)
                print(f"[alerts] could not play {path.name}: {error}")
                time.sleep(1.0)

    # -- reporting -------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            history = list(self._history)
        return {
            "enabled": SOUND_ENABLED,
            "player": player_name(),
            "last_error": self.last_error,
            "active": sorted(self._active),
            "history": history,
            "events": [
                {
                    "name": name,
                    "level": level,
                    "title": title,
                    "enabled": globals().get(f"SOUND_{name.upper()}", True),
                    "has_sound": sound_path(name).exists(),
                }
                for name, (level, title) in EVENTS.items()
            ],
        }


def _register_settings() -> None:
    from settings_store import settings

    me = sys.modules[__name__]
    group = "Sound alerts"
    settings.register(me, "SOUND_ENABLED", "Sound alerts on", group, "bool",
                      help="Turns every spoken alert on or off.")
    settings.register(me, "ALERT_COOLDOWN_S", "Gap before repeating the same alert", group,
                      "float", 0, 600, 5, "Seconds.")
    settings.register(me, "CRITICAL_REPEAT_S", "Repeat a critical alert every", group,
                      "float", 0, 300, 5, "Seconds while it is still active. 0 = say it once.")
    settings.register(me, "HIGH_HEAT_C", "Dangerous room temperature", group, "float",
                      30, 200, 1, "C, measured by the thermal camera.")
    settings.register(me, "DEVICE_OVERHEAT_C", "Device overheating at", group, "float",
                      50, 90, 1, "C, Raspberry Pi CPU temperature.")
    settings.register(me, "CAMERA_FAULT_S", "Camera fault after", group, "float",
                      2, 60, 1, "Seconds without a new frame.")
    settings.register(me, "ALSA_DEVICE", "Audio output device", group, "str",
                      help="Empty for the default. For a USB speaker try plughw:2,0 (see aplay -l).")
    for name, (level, title) in EVENTS.items():
        settings.register(me, f"SOUND_{name.upper()}", title, "Sound alerts: which ones",
                          "bool", help=level)


_register_settings()
