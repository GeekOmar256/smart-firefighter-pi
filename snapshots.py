"""Saved thermal pictures: taken from the page, or automatically on an alert.

Each snapshot is three files with the same name in SNAPSHOT_DIR:

    20261029-141502-person.jpg     the picture as it was shown, with boxes
    20261029-141502-person.npy     the temperatures, (24, 32) in degrees C
    20261029-141502-person.json    why it was taken and what was detected

Like the recordings, they are kept outside the repository.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

SNAPSHOT_DIR = os.environ.get(
    "SNAPSHOT_DIR", str(Path.home() / "smart-firefighter-snapshots")
)

# Adjustable from the settings page
AUTO_SNAPSHOT = True            # save a picture whenever a warning or critical alert sounds
MAX_SNAPSHOTS = 500             # the oldest are deleted beyond this

NAME_PATTERN = re.compile(r"^\d{8}-\d{6}(-\d+)?-[a-z_]+$")


class SnapshotStore:
    def __init__(self, root: str = SNAPSHOT_DIR) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def save(self, jpeg: bytes | None, frame, reason: str, stats: dict | None = None) -> str | None:
        if jpeg is None:
            return None
        reason = re.sub(r"[^a-z_]", "", reason.lower())[:30] or "manual"
        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            name, suffix = f"{stamp}-{reason}", 1
            while (self.root / f"{name}.jpg").exists():
                suffix += 1
                name = f"{stamp}-{suffix}-{reason}"

            (self.root / f"{name}.jpg").write_bytes(jpeg)
            if frame is not None:
                np.save(self.root / f"{name}.npy", np.asarray(frame, dtype=np.float32))
            info = {"reason": reason, "time": time.time()}
            if stats:
                info.update({k: stats.get(k) for k in
                             ("min_c", "max_c", "background_c", "human_count", "detections")})
            (self.root / f"{name}.json").write_text(json.dumps(info, indent=2))
            self._trim()
        print(f"[snapshot] saved {name}")
        return name

    def _trim(self) -> None:
        pictures = sorted(self.root.glob("*.jpg"))
        for old in pictures[:-MAX_SNAPSHOTS] if len(pictures) > MAX_SNAPSHOTS else []:
            for extension in (".jpg", ".npy", ".json"):
                old.with_suffix(extension).unlink(missing_ok=True)

    def list(self, limit: int = 60) -> list[dict]:
        if not self.root.exists():
            return []
        items = []
        for picture in sorted(self.root.glob("*.jpg"), reverse=True)[:limit]:
            try:
                info = json.loads(picture.with_suffix(".json").read_text())
            except (OSError, ValueError):
                info = {}
            items.append({
                "name": picture.stem,
                "reason": info.get("reason", ""),
                "time": info.get("time"),
                "max_c": info.get("max_c"),
                "human_count": info.get("human_count"),
            })
        return items

    def path(self, name: str) -> Path | None:
        """The picture for a name from list(), or None for anything else."""
        if not NAME_PATTERN.match(name):
            return None
        picture = self.root / f"{name}.jpg"
        return picture if picture.exists() else None


def _register_settings() -> None:
    import sys

    from settings_store import settings

    me = sys.modules[__name__]
    settings.register(me, "AUTO_SNAPSHOT", "Save a picture on every alert", "Pictures",
                      "bool", help="Warning and critical alerts only.")
    settings.register(me, "MAX_SNAPSHOTS", "Pictures to keep", "Pictures", "int",
                      10, 5000, 10, "The oldest are deleted beyond this.")


_register_settings()
