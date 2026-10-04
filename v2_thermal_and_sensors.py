"""AI-Based Firefighter Assistance System - Version 2.

Version 1 plus the flame sensors and the gas sensor. The web page shows the
live thermal image and an indicator for each sensor that turns red when
something is detected.

    python3 v2_thermal_and_sensors.py              # on the Raspberry Pi
    python3 v2_thermal_and_sensors.py --simulate   # anywhere, no hardware needed

Then open http://<raspberry-pi-ip>:5000 in a browser.

Tested against Python 3.12 and 3.13.

Wiring note: the MQ-2 runs on 5 V and its D0 pin is also 5 V. The Raspberry Pi
GPIO accepts only 3.3 V, so that line must go through a level shifter or a
resistor divider. The flame sensors can be powered from 3.3 V, which makes
their D0 output safe to connect directly.
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

SENSOR_WIDTH = 32
SENSOR_HEIGHT = 24
SENSOR_PIXELS = SENSOR_WIDTH * SENSOR_HEIGHT

DISPLAY_WIDTH = 640
DISPLAY_HEIGHT = 480
JPEG_QUALITY = 85

REFRESH_HZ = int(os.environ.get("REFRESH_HZ", "8"))

FLIP_HORIZONTAL = True
FLIP_VERTICAL = False

MIN_SPAN_C = 5.0

# BCM pin numbers
# BCM pin numbers, matching how the board is actually wired. Both can be
# overridden from the environment, so the pins can be changed without editing
# this file:  FLAME_SENSOR_PINS=24,27  GAS_SENSOR_PIN=23  python3 ...
#
# One flame sensor is fitted. To add a second, list both pins separated by a
# comma; the rest of the program and the web page follow the list length.
FLAME_SENSOR_PINS = tuple(
    int(pin) for pin in os.environ.get("FLAME_SENSOR_PINS", "24").split(",") if pin.strip()
)
GAS_SENSOR_PIN = int(os.environ.get("GAS_SENSOR_PIN", "23"))   # MQ-2 D0

# These modules pull their D0 output LOW when they detect something.
# Set to False if your modules behave the other way round.
SENSORS_ACTIVE_LOW = True

# A reading must stay the same for this long before the indicator changes.
# It stops the indicators flickering on a noisy threshold.
DEBOUNCE_SECONDS = 0.2

SENSOR_POLL_HZ = 20

HOST = "0.0.0.0"
PORT = 5000


# --------------------------------------------------------------------------
# Colour palette
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

        i2c = busio.I2C(board.SCL, board.SDA, frequency=1_000_000)
        self._mlx = adafruit_mlx90640.MLX90640(i2c)
        self._mlx.refresh_rate = getattr(
            adafruit_mlx90640.RefreshRate, f"REFRESH_{REFRESH_HZ}_HZ"
        )
        self._buffer = [0.0] * SENSOR_PIXELS

        serial = "-".join(format(word, "04x") for word in self._mlx.serial_number)
        print(f"[camera] MLX90640 found, serial {serial}, {REFRESH_HZ} Hz")

    def read(self) -> np.ndarray:
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

        cx = (math.sin(elapsed * 0.6) * 0.5 + 0.5) * (SENSOR_WIDTH - 1)
        cy = SENSOR_HEIGHT * 0.55 + math.sin(elapsed * 1.3) * 1.5
        distance = (self._grid_x - cx) ** 2 / 9.0 + (self._grid_y - cy) ** 2 / 20.0
        frame += 15.0 * np.exp(-distance)

        hot = (self._grid_x - 27.0) ** 2 / 2.5 + (self._grid_y - 5.0) ** 2 / 2.5
        frame += (40.0 + 10.0 * math.sin(elapsed * 5)) * np.exp(-hot)

        frame += self._rng.normal(0.0, 0.25, frame.shape).astype(np.float32)
        return frame


# --------------------------------------------------------------------------
# Digital sensors (flame and gas)
# --------------------------------------------------------------------------

class DigitalSensor:
    """One module with a digital output, read through gpiozero.

    gpiozero talks to the Pi 5 through lgpio. The old RPi.GPIO library does
    not work on the Pi 5 at all, because the GPIO pins are behind the RP1
    chip rather than on the main processor.
    """

    def __init__(self, name: str, pin: int) -> None:
        from gpiozero import DigitalInputDevice

        self.name = name
        self.pin = pin
        # active_state=True means .value simply reports the pin level; the
        # active-low handling is done below so it stays easy to read.
        self._device = DigitalInputDevice(pin, pull_up=None, active_state=True)

        self._state = False
        self._pending = False
        self._pending_since = 0.0
        self._closed = False

    def _raw(self) -> bool:
        level = bool(self._device.value)
        return (not level) if SENSORS_ACTIVE_LOW else level

    def poll(self) -> bool:
        """Read the pin and apply the debounce. Returns the settled state."""
        # The polling thread is a daemon, so on shutdown it can still be in
        # here after main() has closed the devices. Reading a closed gpiozero
        # device raises, which looked like a crash on Ctrl-C.
        if self._closed:
            return self._state

        try:
            reading = self._raw()
        except Exception:                        # noqa: BLE001
            self._closed = True
            return self._state

        now = time.monotonic()

        if reading != self._pending:
            self._pending = reading
            self._pending_since = now
        elif reading != self._state and (now - self._pending_since) >= DEBOUNCE_SECONDS:
            self._state = reading

        return self._state

    @property
    def state(self) -> bool:
        return self._state

    def close(self) -> None:
        self._closed = True                      # stop poll() touching it first
        self._device.close()


class SimulatedSensor:
    """Fake digital sensor that trips every so often, for testing the page."""

    def __init__(self, name: str, pin: int, period: float, duty: float) -> None:
        self.name = name
        self.pin = pin
        self._period = period
        self._duty = duty
        self._t0 = time.monotonic()
        self._state = False

    def poll(self) -> bool:
        phase = ((time.monotonic() - self._t0) % self._period) / self._period
        self._state = phase < self._duty
        return self._state

    @property
    def state(self) -> bool:
        return self._state

    def close(self) -> None:
        pass


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
    if high - low < MIN_SPAN_C:
        middle = (high + low) / 2.0
        low, high = middle - MIN_SPAN_C / 2.0, middle + MIN_SPAN_C / 2.0

    normalised = (frame - low) / (high - low)
    indexes = np.clip(normalised * 255.0, 0, 255).astype(np.uint8)
    coloured = PALETTE[indexes]

    image = Image.fromarray(coloured, mode="RGB").resize(
        (DISPLAY_WIDTH, DISPLAY_HEIGHT), Image.BICUBIC
    )

    output = io.BytesIO()
    image.save(output, format="JPEG", quality=JPEG_QUALITY)
    return output.getvalue()


# --------------------------------------------------------------------------
# Shared state
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
        with self._condition:
            if self._sequence == last_seen:
                self._condition.wait(timeout)
            return self._jpeg, self._sequence

    @property
    def stats(self) -> dict:
        with self._condition:
            return dict(self._stats)


class SensorState:
    """Holds the latest reading of every digital sensor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: dict = {"flame": [], "gas": False}

    def update(self, flame: list[bool], gas: bool) -> None:
        with self._lock:
            self._values = {"flame": list(flame), "gas": bool(gas)}

    @property
    def snapshot(self) -> dict:
        with self._lock:
            flame = list(self._values["flame"])
            gas = self._values["gas"]
        return {
            "flame": flame,
            "flame_any": any(flame),
            "gas": gas,
            "alarm": any(flame) or gas,
        }


store = FrameStore()
sensors = SensorState()


def explain_frame_failures(error: Exception) -> None:
    """Say what a run of failed frames usually means, based on which error."""
    print("[camera] frames keep failing.")
    if isinstance(error, ValueError):
        print("[camera] The readings are arriving corrupted. That normally means")
        print("[camera] the I2C bus is running FASTER than the wiring can carry.")
        print("[camera] Try 400000 in /boot/firmware/config.txt instead of 1000000:")
        print("[camera]     dtparam=i2c_arm_baudrate=400000")
        print("[camera] Short, firmly seated wires matter a lot at these speeds.")
    else:
        print("[camera] The sensor is not delivering frames in time, which")
        print("[camera] normally means the I2C bus is too SLOW. Add to")
        print("[camera] /boot/firmware/config.txt:")
        print("[camera]     dtparam=i2c_arm_baudrate=400000")
    print("[camera] Reboot, then run check_camera.py to confirm.")
    print("[camera] As a stop-gap, try:  REFRESH_HZ=2 python3 ...")


def capture_loop(camera) -> None:
    """Read the camera forever and push rendered frames into the store."""
    failures = 0
    last_time = time.monotonic()
    fps = 0.0

    while True:
        try:
            frame = camera.read()
        except (RuntimeError, ValueError, OSError) as error:
            # RuntimeError: the sensor had no frame ready in time.
            # ValueError:   the driver got numbers it cannot use, because the
            #               data arrived corrupted ("math domain error").
            # Either way one frame is lost. Neither is worth giving up for, so
            # this must not fall through to the handler below.
            failures += 1
            if failures == 10:
                explain_frame_failures(error)
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


def sensor_loop(flame_sensors: list, gas_sensor) -> None:
    """Poll the digital sensors forever and keep the shared state up to date."""
    interval = 1.0 / SENSOR_POLL_HZ
    previous = None

    while True:
        flame = [s.poll() for s in flame_sensors]
        gas = gas_sensor.poll()
        sensors.update(flame, gas)

        current = (tuple(flame), gas)
        if current != previous:                  # a line in the log per change
            for index, value in enumerate(flame, start=1):
                print(f"[sensors] flame {index}: {'DETECTED' if value else 'clear'}")
            print(f"[sensors] gas    : {'DETECTED' if gas else 'clear'}")
            previous = current

        time.sleep(interval)


# --------------------------------------------------------------------------
# Web interface
# --------------------------------------------------------------------------

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Firefighter Assistance</title>
<style>
  :root { --bg:#12141a; --panel:#1b1e26; --line:#2b303b; --text:#e8eaf0;
          --muted:#9aa3b2; --ok:#2e9e6b; --alarm:#e5484d; }
  * { box-sizing: border-box; }
  body { margin:0; padding:24px 16px; background:var(--bg); color:var(--text);
         font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  .wrap { max-width: 720px; margin: 0 auto; }
  h1 { font-size: 1.25rem; margin: 0 0 4px; font-weight: 650; }
  p.sub { margin: 0 0 20px; color: var(--muted); font-size: .9rem; }
  .card { background: var(--panel); border: 1px solid var(--line);
          border-radius: 12px; padding: 12px; }
  img { width: 100%; height: auto; display: block; border-radius: 8px; background:#000; }

  .banner { margin-top: 14px; padding: 12px 14px; border-radius: 10px;
            font-weight: 650; letter-spacing: .02em; text-align: center;
            border: 1px solid var(--line); background: var(--panel); color: var(--muted); }
  .banner.alarm { background: var(--alarm); border-color: var(--alarm); color:#fff; }

  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
          gap: 10px; margin-top: 10px; }
  .chip { display:flex; align-items:center; gap:10px; background: var(--panel);
          border: 1px solid var(--line); border-radius: 10px; padding: 11px 13px; }
  .dot { width: 13px; height: 13px; border-radius: 50%; background: var(--ok);
         flex: 0 0 auto; }
  .chip.on .dot { background: var(--alarm); }
  .chip .name { font-size: .82rem; color: var(--muted); }
  .chip .val  { font-size: .95rem; font-weight: 600; }

  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
           gap: 10px; margin-top: 10px; }
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
  <h1>Firefighter Assistance System</h1>
  <p class="sub">Thermal camera, flame sensors and gas sensor &mdash; live view</p>

  <div class="card"><img src="/stream.mjpg" alt="Live thermal image"></div>

  <div class="banner" id="banner">All clear</div>

  <div class="grid" id="chips"></div>

  <div class="stats">
    <div class="stat"><div class="label">Coldest</div><div class="value" id="min">--</div></div>
    <div class="stat"><div class="label">Hottest</div><div class="value" id="max">--</div></div>
    <div class="stat"><div class="label">Centre</div><div class="value" id="centre">--</div></div>
    <div class="stat"><div class="label">Frames/s</div><div class="value" id="fps">--</div></div>
  </div>
</div>

<script>
function chip(name, on) {
  return '<div class="chip' + (on ? ' on' : '') + '">' +
         '<span class="dot"></span><div>' +
         '<div class="name">' + name + '</div>' +
         '<div class="val">' + (on ? 'DETECTED' : 'Clear') + '</div>' +
         '</div></div>';
}

async function refresh() {
  try {
    const [statsRes, statusRes] = await Promise.all([
      fetch('/api/stats',  { cache: 'no-store' }),
      fetch('/api/status', { cache: 'no-store' })
    ]);
    const s = await statsRes.json();
    const t = await statusRes.json();

    document.getElementById('min').textContent    = s.min_c    + ' \\u00B0C';
    document.getElementById('max').textContent    = s.max_c    + ' \\u00B0C';
    document.getElementById('centre').textContent = s.centre_c + ' \\u00B0C';
    document.getElementById('fps').textContent    = s.fps;

    let html = '';
    t.flame.forEach((v, i) => { html += chip('Flame sensor ' + (i + 1), v); });
    html += chip('Gas / smoke', t.gas);
    document.getElementById('chips').innerHTML = html;

    const banner = document.getElementById('banner');
    banner.classList.toggle('alarm', t.alarm);
    banner.textContent = t.alarm ? 'WARNING - hazard detected' : 'All clear';
  } catch (e) { /* keep the last values on a hiccup */ }
}
refresh();
setInterval(refresh, 400);
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


@app.route("/api/status")
def api_status():
    return jsonify(sensors.snapshot)


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

def build_hardware(simulate: bool):
    """Create the camera and the sensors, real or simulated."""
    if simulate:
        camera = SimulatedCamera()
        flame = [
            SimulatedSensor(f"flame{i}", pin, period=9.0 + 4.0 * i, duty=0.18)
            for i, pin in enumerate(FLAME_SENSOR_PINS)
        ]
        gas = SimulatedSensor("gas", GAS_SENSOR_PIN, period=14.0, duty=0.25)
        print("[sensors] running in SIMULATION mode - no pins are being read")
        return camera, flame, gas

    camera = ThermalCamera()
    flame = [DigitalSensor(f"flame{i + 1}", pin) for i, pin in enumerate(FLAME_SENSOR_PINS)]
    gas = DigitalSensor("gas", GAS_SENSOR_PIN)
    pins = ", ".join(str(p) for p in FLAME_SENSOR_PINS)
    print(f"[sensors] flame on GPIO {pins}, gas on GPIO {GAS_SENSOR_PIN}")
    return camera, flame, gas


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Thermal camera with flame and gas sensor indicators"
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use fake hardware instead of the real sensors",
    )
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    try:
        camera, flame_sensors, gas_sensor = build_hardware(args.simulate)
    except Exception as error:                   # noqa: BLE001
        print(f"Could not start the hardware: {error}", file=sys.stderr)
        print(
            "\nCheck that:\n"
            "  - the camera is wired to 3.3 V, GND, SDA and SCL\n"
            "  - I2C is enabled and 'i2cdetect -y 1' shows address 0x33\n"
            "  - config.txt has dtparam=i2c_arm_baudrate=1000000\n"
            "  - python3-lgpio is installed (the Pi 5 does not support RPi.GPIO)\n"
            "\nTo work on the web page without hardware, run with --simulate.",
            file=sys.stderr,
        )
        return 1

    threading.Thread(target=capture_loop, args=(camera,), daemon=True).start()
    threading.Thread(
        target=sensor_loop, args=(flame_sensors, gas_sensor), daemon=True
    ).start()

    print(f"[web] open http://<raspberry-pi-ip>:{args.port} in a browser")
    try:
        app.run(host=HOST, port=args.port, threaded=True, debug=False)
    finally:
        for sensor in flame_sensors:
            sensor.close()
        gas_sensor.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
