"""AI-Based Firefighter Assistance System - Version 3.

Version 2 (thermal camera + flame and gas sensors on a web page) plus the
Firebase link, so the Pi can be watched and controlled from anywhere.

    python3 v3_remote.py              # on the Raspberry Pi
    python3 v3_remote.py --simulate   # anywhere, no hardware needed
    python3 v3_remote.py --no-cloud   # exactly like version 2

The camera, the sensors and the image rendering are imported from version 2
rather than copied, so there is only one place to change them.

Tested against Python 3.12 and 3.13.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time

from flask import Flask, Response, jsonify, render_template_string

from v2_thermal_and_sensors import (
    PAGE,
    SENSOR_HEIGHT,
    SENSOR_WIDTH,
    FrameStore,
    SensorState,
    build_hardware,
    frame_to_jpeg,
)

HOST = "0.0.0.0"
PORT = 5000
SENSOR_POLL_HZ = 20

store = FrameStore()
sensors = SensorState()
cloud = None


# --------------------------------------------------------------------------
# Background work
# --------------------------------------------------------------------------

def capture_loop(camera) -> None:
    """Read the camera forever and push rendered frames into the store."""
    failures = 0
    last_time = time.monotonic()
    fps = 0.0

    while True:
        try:
            frame = camera.read()
        except RuntimeError as error:
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


def sensor_loop(flame_sensors: list, gas_sensor) -> None:
    """Poll the digital sensors forever and keep the shared state up to date."""
    interval = 1.0 / SENSOR_POLL_HZ
    previous = None

    while True:
        flame = [s.poll() for s in flame_sensors]
        gas = gas_sensor.poll()
        sensors.update(flame, gas)

        current = (tuple(flame), gas)
        if current != previous:
            for index, value in enumerate(flame, start=1):
                print(f"[sensors] flame {index}: {'DETECTED' if value else 'clear'}")
            print(f"[sensors] gas    : {'DETECTED' if gas else 'clear'}")
            previous = current

        time.sleep(interval)


def status_for_cloud() -> dict:
    """The extra fields the Pi reports to Firebase alongside its own health."""
    stats = store.stats
    state = sensors.snapshot
    return {
        "camera": {
            "fps": stats.get("fps", 0),
            "min_c": stats.get("min_c"),
            "max_c": stats.get("max_c"),
            "centre_c": stats.get("centre_c"),
        },
        "sensors": {
            "flame": state["flame"],
            "flame_any": state["flame_any"],
            "gas": state["gas"],
            "alarm": state["alarm"],
        },
    }


# --------------------------------------------------------------------------
# Web interface (same page as version 2)
# --------------------------------------------------------------------------

app = Flask(__name__)


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/api/stats")
def api_stats():
    return jsonify(store.stats or {"min_c": 0, "max_c": 0, "centre_c": 0, "fps": 0})


@app.route("/api/status")
def api_status():
    return jsonify(sensors.snapshot)


@app.route("/api/health")
def api_health():
    """Everything at once, which is also what gets sent to Firebase."""
    payload = status_for_cloud()
    payload["cloud_connected"] = cloud is not None
    return jsonify(payload)


def mjpeg_stream():
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
    global cloud

    parser = argparse.ArgumentParser(
        description="Thermal camera and sensors, with Firebase remote control"
    )
    parser.add_argument("--simulate", action="store_true",
                        help="use fake hardware instead of the real sensors")
    parser.add_argument("--no-cloud", action="store_true",
                        help="skip Firebase and run purely on the local network")
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
    threading.Thread(target=sensor_loop,
                     args=(flame_sensors, gas_sensor), daemon=True).start()

    if not args.no_cloud:
        try:
            from cloud_link import CloudLink

            cloud = CloudLink()
            cloud.set_status_source(status_for_cloud)
            cloud.start()
        except Exception as error:               # noqa: BLE001
            # The Pi must keep working on the local network even when the
            # internet or the key is missing, so this is a warning, not a stop.
            print(f"[cloud] not connected: {error}", file=sys.stderr)
            print("[cloud] carrying on without remote control", file=sys.stderr)

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
