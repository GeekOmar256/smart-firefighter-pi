"""Human and fire detection from MLX90640 thermal frames.

This is a rule-based detector. It started from the heat-cluster detector in
the Helmet project (RPi/app.py, _detect_heat_clusters) and changes it where
that one gives false alarms or misses people:

  * A pixel counts as warm when it is warmer than the room around it, not only
    when it sits inside a fixed 28-42 C window. In a hot room the fixed window
    turned the whole background into one huge "person".
  * The confidence score decides. In the Helmet version every cluster of 50 or
    more pixels was a person and the score was only printed on the image.
  * The smallest person is 6 pixels instead of 50, so someone a few metres
    away is not ignored.
  * A warm ring around something hotter than skin (a heater, a pan, an engine)
    is rejected as a hot object.
  * "Person present" needs PERSIST_NEEDED of the last PERSIST_WINDOW frames,
    so the answer does not flicker from one frame to the next.

It is the baseline that a trained model can be compared against later:
score_cluster() is the part a model would replace.

Every threshold can be changed from the environment without editing this file.
"""

from __future__ import annotations

import os
from collections import deque

import numpy as np
from scipy import ndimage


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


# Coldest pixel that can still be part of a person. Clothing seen from a few
# metres away often reads only 26-30 C, so this is lower than skin.
HUMAN_TEMP_MIN_C = _env_float("HUMAN_TEMP_MIN_C", 25.0)
# Hotter than this is not a person: a heater, a hot surface, a fire.
HUMAN_TEMP_MAX_C = _env_float("HUMAN_TEMP_MAX_C", 42.0)
# How much warmer than the room a pixel must be to belong to a warm area.
HUMAN_CONTRAST_C = _env_float("HUMAN_CONTRAST_C", 2.0)
HUMAN_MIN_PIXELS = int(_env_float("HUMAN_MIN_PIXELS", 6))
# A warm area scoring below this is not reported as a person.
HUMAN_MIN_CONFIDENCE = _env_float("HUMAN_MIN_CONFIDENCE", 0.5)

FIRE_TEMP_MIN_C = _env_float("FIRE_TEMP_MIN_C", 80.0)
FIRE_MIN_PIXELS = int(_env_float("FIRE_MIN_PIXELS", 3))

# The room temperature is taken as this percentile of the frame rather than
# the median, so it stays right when a person fills half the view.
BACKGROUND_PERCENTILE = 30

# A warm area whose outline is more than this fraction "too hot for skin" is
# the warm halo around a hot object, not a person.
HOT_RING_FRACTION = 0.3

PERSIST_WINDOW = 5
PERSIST_NEEDED = 3

# Diagonal neighbours count as touching. The sensor's checkerboard read-out
# otherwise breaks one person into several pieces.
EIGHT_CONNECTED = np.ones((3, 3), dtype=bool)


def _bounding_box(mask: np.ndarray) -> tuple[int, int, int, int]:
    ys, xs = np.nonzero(mask)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return x0, y0, x1 - x0 + 1, y1 - y0 + 1


def score_cluster(
    frame: np.ndarray,
    mask: np.ndarray,
    background: float,
    too_hot: np.ndarray,
) -> float:
    """How much one warm area looks like a person, from 0 to 1."""
    temps = frame[mask]
    _, _, w, h = _bounding_box(mask)

    # A warm area wrapped around something hotter than skin is the halo of a
    # hot object (heater, pan). The hot core is never in the mask, so it shows
    # up as a hole. A person standing NEXT to a fire does not enclose it, so
    # is not rejected by this.
    enclosed = ndimage.binary_fill_holes(mask) & ~mask
    if (enclosed & too_hot).any():
        return 0.0
    # A warm area mostly bordered by too-hot pixels is the edge of a hot
    # object that runs off the frame.
    outline = ndimage.binary_dilation(mask, structure=EIGHT_CONNECTED) & ~mask
    if outline.any() and float(too_hot[outline].mean()) > HOT_RING_FRACTION:
        return 0.0

    peak = float(temps.max())
    score = 0.2

    contrast = float(temps.mean()) - background
    if contrast >= 2 * HUMAN_CONTRAST_C:
        score += 0.25
    elif contrast >= HUMAN_CONTRAST_C:
        score += 0.1

    # Exposed skin (face, neck, hands) reads about 30-38 C on this sensor.
    # A warm wall or a cushion that someone sat on rarely gets that hot, and
    # something hotter than about 39 C is a surface heated by the fire.
    if 30.0 <= peak <= 38.0:
        score += 0.25
    elif peak > 39.0:
        score -= 0.3

    # Taller than wide (standing, sitting) or much wider than tall (lying).
    # The Helmet version only rewarded tall shapes, which penalised the person
    # lying on the floor, the case that matters most in a rescue.
    aspect = h / w
    if aspect >= 1.3 or aspect <= 0.6:
        score += 0.15

    # A body fills a good part of its bounding box; a thin streak does not.
    if mask.sum() / (w * h) >= 0.3:
        score += 0.15

    return min(score, 1.0)


def find_fires(frame: np.ndarray) -> list[dict]:
    hot = frame >= FIRE_TEMP_MIN_C
    if not hot.any():
        return []

    fires = []
    labels, count = ndimage.label(hot, structure=EIGHT_CONNECTED)
    for index in range(1, count + 1):
        mask = labels == index
        size = int(mask.sum())
        if size < FIRE_MIN_PIXELS:
            continue
        temps = frame[mask]
        x, y, w, h = _bounding_box(mask)
        fires.append({
            "type": "fire",
            "x": x, "y": y, "w": w, "h": h,
            "size": size,
            "temp_avg": round(float(temps.mean()), 1),
            "temp_max": round(float(temps.max()), 1),
            "confidence": round(min(1.0, 0.5 + (float(temps.mean()) - FIRE_TEMP_MIN_C) / 100.0), 2),
        })
    return fires


def find_people(frame: np.ndarray, background: float) -> list[dict]:
    too_hot = frame > HUMAN_TEMP_MAX_C
    warm = (
        (frame >= HUMAN_TEMP_MIN_C)
        & ~too_hot
        & (frame >= background + HUMAN_CONTRAST_C)
    )
    if not warm.any():
        return []

    people = []
    labels, count = ndimage.label(warm, structure=EIGHT_CONNECTED)
    for index in range(1, count + 1):
        mask = labels == index
        size = int(mask.sum())
        if size < HUMAN_MIN_PIXELS:
            continue

        confidence = score_cluster(frame, mask, background, too_hot)
        if confidence < HUMAN_MIN_CONFIDENCE:
            continue

        temps = frame[mask]
        x, y, w, h = _bounding_box(mask)
        people.append({
            "type": "human",
            "x": x, "y": y, "w": w, "h": h,
            "size": size,
            "temp_avg": round(float(temps.mean()), 1),
            "temp_max": round(float(temps.max()), 1),
            "confidence": round(confidence, 2),
        })
    return people


def detect(frame: np.ndarray) -> tuple[list[dict], float]:
    """Find fires and people in one (24, 32) frame of degrees Celsius.

    Returns the detections and the room temperature that was used. Box
    coordinates are in sensor pixels of the frame as given.
    """
    frame = np.asarray(frame, dtype=np.float32)
    background = float(np.percentile(frame, BACKGROUND_PERCENTILE))
    return find_fires(frame) + find_people(frame, background), background


class Detector:
    """Runs detect() on every frame and steadies the yes/no answers."""

    def __init__(self) -> None:
        self._human_history: deque[bool] = deque(maxlen=PERSIST_WINDOW)
        self._fire_history: deque[bool] = deque(maxlen=PERSIST_WINDOW)
        self._human = False
        self._fire = False

    def update(self, frame: np.ndarray) -> dict:
        detections, background = detect(frame)
        people = sum(1 for d in detections if d["type"] == "human")
        fires = sum(1 for d in detections if d["type"] == "fire")

        if self._human_history.maxlen != PERSIST_WINDOW:   # changed on the settings page
            self._human_history = deque(self._human_history, maxlen=PERSIST_WINDOW)
            self._fire_history = deque(self._fire_history, maxlen=PERSIST_WINDOW)
        needed = min(PERSIST_NEEDED, PERSIST_WINDOW)

        self._human_history.append(people > 0)
        self._fire_history.append(fires > 0)
        human = sum(self._human_history) >= needed
        fire = sum(self._fire_history) >= needed

        if human != self._human:                 # a line in the log per change
            print(f"[detect] person: {'DETECTED (' + str(people) + ' in view)' if human else 'clear'}")
        if fire != self._fire:
            print(f"[detect] fire (thermal): {'DETECTED' if fire else 'clear'}")
        self._human, self._fire = human, fire

        return {
            "detections": detections,
            "human": human,
            "human_count": people,
            "fire_thermal": fire,
            "background_c": round(background, 1),
        }


# --------------------------------------------------------------------------
# Adjustable from the settings page
# --------------------------------------------------------------------------

def _register_settings() -> None:
    import sys

    from settings_store import settings

    me = sys.modules[__name__]
    group = "Human detection"
    settings.register(me, "HUMAN_CONTRAST_C", "Warmer than the room by", group, "float",
                      0.5, 15, 0.5, "C. Lower finds people in hot rooms, higher gives fewer false boxes.")
    settings.register(me, "HUMAN_MIN_CONFIDENCE", "Minimum confidence", group, "float",
                      0, 1, 0.05, "0-1. A warm area scoring less is not reported as a person.")
    settings.register(me, "HUMAN_MIN_PIXELS", "Smallest person (pixels)", group, "int",
                      1, 300, 1, "Out of 768. About 6 is a person 4-5 m away.")
    settings.register(me, "HUMAN_TEMP_MIN_C", "Coldest body pixel", group, "float",
                      10, 35, 0.5, "C. Clothing seen from a distance reads 26-30 C.")
    settings.register(me, "HUMAN_TEMP_MAX_C", "Hottest body pixel", group, "float",
                      35, 60, 0.5, "C. Hotter than this is a hot object, not a person.")
    settings.register(me, "BACKGROUND_PERCENTILE", "Room temperature percentile", group, "int",
                      5, 50, 1, "Which percentile of the frame is taken as the room.")
    settings.register(me, "PERSIST_WINDOW", "Frames to look back", group, "int",
                      1, 20, 1, "Detection is steadied over this many frames.")
    settings.register(me, "PERSIST_NEEDED", "Frames that must agree", group, "int",
                      1, 20, 1, "Out of the frames above. Higher means slower but steadier.")

    group = "Fire detection (thermal)"
    settings.register(me, "FIRE_TEMP_MIN_C", "Fire temperature", group, "float",
                      40, 300, 1, "C. Pixels at or above this count as fire.")
    settings.register(me, "FIRE_MIN_PIXELS", "Smallest fire (pixels)", group, "int",
                      1, 100, 1, "")


_register_settings()
