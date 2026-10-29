"""Records raw thermal frames in labelled sessions, for training a model.

Recording runs inside the web program, from the panel on the page, because
only one program can read the camera at a time.

Each session is one folder:

    <RECORD_DIR>/<label>/<YYYYmmdd-HHMMSS>/
        session.json      label, note, start and end time, frame count, settings
        chunk_0001.npz    frames (N, 24, 32) float32 in degrees C, timestamps (N,)
        chunk_0002.npz    ...

Frames are written every CHUNK_FRAMES, so a crash or a pulled power cable
loses only the last few seconds. They are the camera's own temperatures,
flipped to match the picture on the page but not smoothed, so a model can be
trained on exactly what the sensor measured.

RECORD_DIR defaults to a folder outside the repository: "update" runs
git reset --hard, and the recordings must never end up in the public repo.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np

# What can be recorded. The label becomes a folder name, so only these exist.
LABELS = (
    "person_standing",
    "person_sitting",
    "person_lying",
    "multiple_people",
    "empty_room",
    "hot_object",        # heater, laptop, kettle, warm wall: no person
    "own_hand",          # the wearer's own hand or arm in front of the camera
    "fire",
    "other",
)

RECORD_DIR = os.environ.get(
    "RECORD_DIR", str(Path.home() / "smart-firefighter-recordings")
)

CHUNK_FRAMES = 40          # about 10 s at 4 frames per second
MAX_NOTE_LENGTH = 200
SESSION_PATTERN = re.compile(r"^\d{8}-\d{6}(-\d+)?$")


class Recorder:
    """Collects frames while a session is running and writes them to disk."""

    def __init__(self, root: str = RECORD_DIR, settings: dict | None = None) -> None:
        self.root = Path(root)
        self.settings = dict(settings or {})
        self._lock = threading.Lock()
        self._session: dict | None = None
        self._folder: Path | None = None
        self._frames: list[np.ndarray] = []
        self._times: list[float] = []
        self._chunks = 0

    # -- control ---------------------------------------------------------

    def start(self, label: str, note: str = "") -> dict:
        if label not in LABELS:
            raise ValueError(f"unknown label '{label}'")

        with self._lock:
            if self._session is not None:
                self._finish()

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            folder = self.root / label / stamp
            suffix = 1
            while folder.exists():               # two starts in the same second
                suffix += 1
                folder = self.root / label / f"{stamp}-{suffix}"
            folder.mkdir(parents=True)

            self._folder = folder
            self._frames, self._times, self._chunks = [], [], 0
            self._session = {
                "label": label,
                "note": str(note)[:MAX_NOTE_LENGTH],
                "started_at": time.time(),
                "ended_at": None,
                "frames": 0,
                "shape": None,
                "units": "degC",
                "settings": self.settings,
            }
            self._write_meta()

        print(f"[record] started '{label}' in {folder}")
        return self.status()

    def stop(self) -> dict:
        with self._lock:
            if self._session is not None:
                self._finish()
        return self.status()

    def add(self, frame: np.ndarray) -> None:
        """Called by the capture loop for every frame; cheap when idle."""
        if self._session is None:
            return
        with self._lock:
            if self._session is None:
                return
            self._frames.append(np.array(frame, dtype=np.float32, copy=True))
            self._times.append(time.time())
            self._session["frames"] += 1
            if self._session["shape"] is None:
                self._session["shape"] = list(frame.shape)
            if len(self._frames) >= CHUNK_FRAMES:
                self._flush()

    # -- reporting -------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            session = self._session
            current = None
            if session is not None:
                current = {
                    "label": session["label"],
                    "note": session["note"],
                    "frames": session["frames"],
                    "seconds": round(time.time() - session["started_at"], 1),
                    "folder": str(self._folder),
                }
        return {
            "recording": current is not None,
            "session": current,
            "labels": list(LABELS),
            "totals": self.totals(),
            "folder": str(self.root),
        }

    def totals(self) -> dict:
        """Frames recorded so far for each label, read back from disk."""
        counts: Counter = Counter()
        if self.root.exists():
            for meta in self.root.glob("*/*/session.json"):
                try:
                    data = json.loads(meta.read_text())
                    counts[data["label"]] += int(data.get("frames", 0))
                except (OSError, ValueError, KeyError):
                    continue
        return dict(counts)

    def sessions(self) -> list[dict]:
        """Every recorded session, newest first, for the browser page."""
        found = []
        if self.root.exists():
            for meta in self.root.glob("*/*/session.json"):
                try:
                    data = json.loads(meta.read_text())
                except (OSError, ValueError):
                    continue
                found.append({
                    "label": meta.parent.parent.name,
                    "session": meta.parent.name,
                    "note": data.get("note", ""),
                    "frames": int(data.get("frames", 0)),
                    "started_at": data.get("started_at"),
                })
        found.sort(key=lambda s: s["started_at"] or 0, reverse=True)
        return found

    def session_folder(self, label: str, session: str) -> Path | None:
        """The folder of one session, checked so no other path can be reached."""
        if label not in LABELS or not SESSION_PATTERN.match(session):
            return None
        folder = self.root / label / session
        return folder if (folder / "session.json").exists() else None

    # -- internals (call with the lock held) ------------------------------

    def _flush(self) -> None:
        if not self._frames:
            return
        self._chunks += 1
        np.savez(
            self._folder / f"chunk_{self._chunks:04d}.npz",
            frames=np.stack(self._frames),
            timestamps=np.array(self._times, dtype=np.float64),
        )
        self._frames, self._times = [], []
        self._write_meta()

    def _finish(self) -> None:
        self._flush()
        self._session["ended_at"] = time.time()
        self._write_meta()
        print(f"[record] stopped '{self._session['label']}', "
              f"{self._session['frames']} frames in {self._folder}")
        self._session = None
        self._folder = None

    def _write_meta(self) -> None:
        (self._folder / "session.json").write_text(json.dumps(self._session, indent=2))


def load_session(folder: str | Path) -> tuple[dict, np.ndarray, np.ndarray]:
    """Read one recorded session back: (meta, frames, timestamps)."""
    folder = Path(folder)
    meta = json.loads((folder / "session.json").read_text())
    frames, times = [], []
    for chunk in sorted(folder.glob("chunk_*.npz")):
        with np.load(chunk) as data:
            frames.append(data["frames"])
            times.append(data["timestamps"])
    if not frames:
        return meta, np.empty((0, 24, 32), np.float32), np.empty(0)
    return meta, np.concatenate(frames), np.concatenate(times)
