"""AI-Based Firefighter Assistance System - Version 1.

Reads the MLX90640 thermal camera and serves a live thermal image on a web page.

    python3 v1_thermal_camera.py              # on the Raspberry Pi
    python3 v1_thermal_camera.py --simulate   # anywhere, no hardware needed

Then open http://<raspberry-pi-ip>:5000 in a browser.

Tested against Python 3.12 and 3.13.
"""

from __future__ import annotations

import argparse
import io
import math
import os
import sys
import threading
import time

import numpy as np
from flask import Flask, Response, jsonify, render_template_string
from PIL import Image

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

SENSOR_WIDTH = 32           # MLX90640 pixel grid
SENSOR_HEIGHT = 24
SENSOR_PIXELS = SENSOR_WIDTH * SENSOR_HEIGHT

DISPLAY_WIDTH = 640         # size of the picture shown in the browser
DISPLAY_HEIGHT = 480
JPEG_QUALITY = 85

# The MLX90640 sends one half of the image at a time, so a complete frame
# arrives at about half this rate. 8 Hz gives a clean image; 16 Hz is faster
# but noisier.
REFRESH_HZ = int(os.environ.get("REFRESH_HZ", "8"))

FLIP_HORIZONTAL = True      # set to match how the board is mounted
FLIP_VERTICAL = False

# The colour scale stretches between the coldest and the hottest pixel of the
# frame. This is the smallest temperature span it is allowed to use, so a flat
# scene does not turn into amplified noise.
MIN_SPAN_C = 5.0

HOST = "0.0.0.0"
PORT = 5000


# --------------------------------------------------------------------------
# Colour palette (black -> blue -> red -> yellow -> white)
# --------------------------------------------------------------------------

def build_palette() -> np.ndarray:
    """Return a 256x3 uint8 lookup table in the usual thermal colours."""
    stops = [
        (0.00, (0, 0, 0)),
        (0.25, (0, 0, 140)),
        (0.45, (140, 0, 150)),
        (0.62, (225, 60, 20)),
        (0.80, (255, 170, 0)),
        (0.92, (255, 240, 120)),
        (1.00, (255, 255, 255)),
    ]
    positions = np.array([s[0] for s in stops])
    colours = np.array([s[1] for s in stops], dtype=np.float64)

    x = np.linspace(0.0, 1.0, 256)
    table = np.empty((256, 3), dtype=np.uint8)
    for channel in range(3):
        table[:, channel] = np.interp(x, positions, colours[:, channel]).astype(np.uint8)
    return table


PALETTE = build_palette()


# --------------------------------------------------------------------------
# Camera
# --------------------------------------------------------------------------

class ThermalCamera:
    """Reads temperature frames from the MLX90640."""

    def __init__(self) -> None:
        import adafruit_mlx90640
        import board
        import busio

        # The real bus speed comes from /boot/firmware/config.txt
        # (dtparam=i2c_arm_baudrate=1000000). Without a fast bus the sensor
        # cannot deliver full frames and getFrame() keeps timing out.
        i2c = busio.I2C(board.SCL, board.SDA, frequency=1_000_000)

        self._mlx = adafruit_mlx90640.MLX90640(i2c)
        self._mlx.refresh_rate = getattr(
            adafruit_mlx90640.RefreshRate, f"REFRESH_{REFRESH_HZ}_HZ"
        )
        self._buffer = [0.0] * SENSOR_PIXELS

        serial = "-".join(format(word, "04x") for word in self._mlx.serial_number)
        print(f"[camera] MLX90640 found, serial {serial}, {REFRESH_HZ} Hz")

    def read(self) -> np.ndarray:
        """Return one frame as a (24, 32) array of degrees Celsius."""
        self._mlx.getFrame(self._buffer)
        return np.asarray(self._buffer, dtype=np.float32).reshape(
            SENSOR_HEIGHT, SENSOR_WIDTH
        )


class SimulatedCamera:
    """Fake camera, so the web interface can be developed without hardware."""

    def __init__(self) -> None:
        self._t0 = time.monotonic()
        grid_y, grid_x = np.mgrid[0:SENSOR_HEIGHT, 0:SENSOR_WIDTH]
        self._grid_x = grid_x.astype(np.float32)
        self._grid_y = grid_y.astype(np.float32)
        self._rng = np.random.default_rng(1)
        print("[camera] running in SIMULATION mode - no sensor is being read")

    def read(self) -> np.ndarray:
        time.sleep(1.0 / max(REFRESH_HZ / 2, 1))
        elapsed = time.monotonic() - self._t0

        frame = np.full((SENSOR_HEIGHT, SENSOR_WIDTH), 21.0, dtype=np.float32)

        # a warm body walking across the view
        cx = (math.sin(elapsed * 0.6) * 0.5 + 0.5) * (SENSOR_WIDTH - 1)
        cy = SENSOR_HEIGHT * 0.55 + math.sin(elapsed * 1.3) * 1.5
        distance = (self._grid_x - cx) ** 2 / 9.0 + (self._grid_y - cy) ** 2 / 20.0
        frame += 15.0 * np.exp(-distance)

        # a small hot spot, like a flame in the corner
        hot = (self._grid_x - 27.0) ** 2 / 2.5 + (self._grid_y - 5.0) ** 2 / 2.5
        frame += (40.0 + 10.0 * math.sin(elapsed * 5)) * np.exp(-hot)

        frame += self._rng.normal(0.0, 0.25, frame.shape).astype(np.float32)
        return frame


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------

def frame_to_jpeg(frame: np.ndarray) -> bytes:
    """Turn a temperature array into a coloured, enlarged JPEG image."""
    if FLIP_HORIZONTAL:
        frame = np.fliplr(frame)
    if FLIP_VERTICAL:
        frame = np.flipud(frame)

    low = float(frame.min())
    high = float(frame.max())
    if high - low < MIN_SPAN_C:                 # keep a sensible colour scale
        middle = (high + low) / 2.0
        low, high = middle - MIN_SPAN_C / 2.0, middle + MIN_SPAN_C / 2.0

    normalised = (frame - low) / (high - low)
    indexes = np.clip(normalised * 255.0, 0, 255).astype(np.uint8)
    coloured = PALETTE[indexes]                 # (24, 32, 3)

    image = Image.fromarray(coloured, mode="RGB").resize(
        (DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.BICUBIC
    )

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=JPEG_QUALITY)
    return output.getvalue()


# --------------------------------------------------------------------------
# Shared frame store
# --------------------------------------------------------------------------

class FrameStore:
    """Holds the newest JPEG frame and lets readers wait for the next one."""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._jpeg: bytes | None = None
        self._stats: dict = {}
        self._sequence = 0

    def publish(self, jpeg: bytes, stats: dict) -> None:
        with self._condition:
            self._jpeg = jpeg
            self._stats = stats
            self._sequence += 1
            self._condition.notify_all()

    def wait_for_next(self, last_seen: int, timeout: float = 5.0):
        """Block until a newer frame exists. Returns (jpeg, sequence)."""
        with self._condition:
            if self._sequence == last_seen:
                self._condition.wait(timeout)
            return self._jpeg, self._sequence

    @property
    def stats(self) -> dict:
        with self._condition:
            return dict(self._stats)


store = FrameStore()


def capture_loop(camera) -> None:
    """Read the camera forever and push rendered frames into the store."""
    failures = 0
    last_time = time.monotonic()
    fps = 0.0

    while True:
        try:
            frame = camera.read()
        except RuntimeError as error:
            # getFrame() raises this when the I2C bus drops data. It is normal
            # once in a while; only a long run of failures is a real problem.
            failures += 1
            if failures == 10:
                print("[camera] frames keep failing. The usual cause is the I2C bus")
                print("[camera] still running at 100 kHz. Add this to")
                print("[camera]   /boot/firmware/config.txt   ->  dtparam=i2c_arm_baudrate=1000000")
                print("[camera] then reboot. Run check_camera.py to confirm.")
                print("[camera] As a stop-gap, try:  REFRESH_HZ=2 python3 ...")
            if failures % 10 == 0:
                print(f"[camera] {failures} dropped frames ({error})")
            continue
        except Exception as error:               # noqa: BLE001
            print(f"[camera] stopped: {error}")
            return

        failures = 0

        now = time.monotonic()
        interval = now - last_time
        last_time = now
        if interval > 0:
            fps = 0.8 * fps + 0.2 * (1.0 / interval) if fps else 1.0 / interval

        stats = {
            "min_c": round(float(frame.min()), 1),
            "max_c": round(float(frame.max()), 1),
            "centre_c": round(float(frame[SENSOR_HEIGHT // 2, SENSOR_WIDTH // 2]), 1),
            "fps": round(fps, 1),
        }
        store.publish(frame_to_jpeg(frame), stats)


# --------------------------------------------------------------------------
# Web interface
# --------------------------------------------------------------------------

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Thermal Camera</title>
<style>
  :root { --bg:#12141a; --panel:#1b1e26; --line:#2b303b; --text:#e8eaf0; --muted:#9aa3b2; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 1.25rem; margin: 0 0 4px; font-weight: 650; }
  p.sub { margin: 0 0 20px; color: var(--muted); font-size: .9rem; }
  .card { background: var(--panel); border: 1px solid var(--line);
          border-radius: 12px; padding: 12px; }
  img { width: 100%; height: auto; display: block; border-radius: 8px; background:#000; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
           gap: 10px; margin-top: 14px; }
  .stat { background: var(--panel); border: 1px solid var(--line);
          border-radius: 10px; padding: 10px 12px; }
  .stat .label { color: var(--muted); font-size: .72rem; text-transform: uppercase;
                 letter-spacing: .06em; }
  .stat .value { font-size: 1.3rem; font-weight: 650; margin-top: 2px;
                 font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Thermal Camera</h1>
  <p class="sub">AI-Based Firefighter Assistance System &mdash; MLX90640 live view</p>

  <div class="card"><img src="/stream.mjpg" alt="Live thermal image"></div>

  <div class="stats">
    <div class="stat"><div class="label">Coldest</div><div class="value" id="min">--</div></div>
    <div class="stat"><div class="label">Hottest</div><div class="value" id="max">--</div></div>
    <div class="stat"><div class="label">Centre</div><div class="value" id="centre">--</div></div>
    <div class="stat"><div class="label">Frames/s</div><div class="value" id="fps">--</div></div>
  </div>
</div>

<script>
async function refresh() {
  try {
    const r = await fetch('/api/stats', { cache: 'no-store' });
    const s = await r.json();
    document.getElementById('min').textContent    = s.min_c    + ' \\u00B0C';
    document.getElementById('max').textContent    = s.max_c    + ' \\u00B0C';
    document.getElementById('centre').textContent = s.centre_c + ' \\u00B0C';
    document.getElementById('fps').textContent    = s.fps;
  } catch (e) { /* keep the last values on a hiccup */ }
}
refresh();
setInterval(refresh, 500);
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/api/stats")
def api_stats():
    return jsonify(store.stats or {"min_c": 0, "max_c": 0, "centre_c": 0, "fps": 0})


def mjpeg_stream():
    """Yield frames as a multipart MJPEG stream the browser can display."""
    sequence = 0
    while True:
        jpeg, sequence = store.wait_for_next(sequence)
        if jpeg is None:
            continue
        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n" + jpeg + b"\r\n"
        )


@app.route("/stream.mjpg")
def stream():
    return Response(
        mjpeg_stream(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-store"},
    )


# --------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="MLX90640 thermal camera web view")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use a fake camera instead of the real sensor",
    )
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    if args.simulate:
        camera = SimulatedCamera()
    else:
        try:
            camera = ThermalCamera()
        except Exception as error:               # noqa: BLE001
            print(f"Could not start the thermal camera: {error}", file=sys.stderr)
            print(
                "\nCheck that:\n"
                "  - the camera is wired to 3.3 V, GND, SDA and SCL\n"
                "  - I2C is enabled and 'i2cdetect -y 1' shows address 0x33\n"
                "  - config.txt has dtparam=i2c_arm_baudrate=1000000\n"
                "\nTo work on the web page without hardware, run with --simulate.",
                file=sys.stderr,
            )
            return 1

    threading.Thread(target=capture_loop, args=(camera,), daemon=True).start()

    print(f"[web] open http://<raspberry-pi-ip>:{args.port} in a browser")
    app.run(host=HOST, port=args.port, threaded=True, debug=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
