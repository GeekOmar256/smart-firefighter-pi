"""Work out why the MLX90640 is dropping frames.

Run this on the Raspberry Pi when the camera says "Too many retries":

    .venv/bin/python3 check_camera.py

It reports the real I2C bus speed, checks the sensor answers, then tries to
read frames at several refresh rates and tells you which ones actually work.
"""

from __future__ import annotations

import glob
import sys
import time

FRAMES_PER_TEST = 4
RATES_TO_TRY = (2, 4, 8, 16)

# A full frame is two subpages of 834 16-bit registers, plus the control and
# status reads: roughly 3.4 kB of traffic per frame.
BYTES_PER_FRAME = 3400


def read_bus_speed() -> int | None:
    """Find the I2C clock frequency the kernel is actually using."""
    patterns = [
        "/proc/device-tree/soc/i2c@*/clock-frequency",
        "/proc/device-tree/soc/*/i2c@*/clock-frequency",
        "/proc/device-tree/axi/*/*/i2c@*/clock-frequency",
        "/proc/device-tree/**/i2c@*/clock-frequency",
    ]
    found = []
    for pattern in patterns:
        for path in glob.glob(pattern, recursive=True):
            try:
                value = int.from_bytes(open(path, "rb").read(4), "big")
                found.append((path, value))
            except OSError:
                pass

    if not found:
        return None
    # The ARM I2C bus is the fastest configured one in practice.
    return max(value for _, value in found)


def describe_speed(hz: int | None) -> None:
    if hz is None:
        print("  bus speed : could not be read from the device tree")
        return

    khz = hz / 1000.0
    transfer_ms = (BYTES_PER_FRAME * 9) / hz * 1000.0   # ~9 bit-times per byte
    print(f"  bus speed : {khz:.0f} kHz")
    print(f"  a frame needs roughly {transfer_ms:.0f} ms of bus time at this speed")

    if hz < 400_000:
        print()
        print("  >> This is the problem. The bus is too slow for this sensor.")
        print("     Add this line to /boot/firmware/config.txt and reboot:")
        print("         dtparam=i2c_arm_baudrate=1000000")
    elif hz < 1_000_000:
        print("  (workable, but 1000000 is recommended)")
    else:
        print("  (good)")


def main() -> int:
    print("MLX90640 check")
    print("=" * 52)

    print("\n1. I2C bus")
    adapters = sorted(glob.glob("/dev/i2c-*"))
    print(f"  adapters  : {', '.join(adapters) if adapters else 'none found'}")
    speed = read_bus_speed()
    describe_speed(speed)

    print("\n2. Sensor")
    try:
        import adafruit_mlx90640
        import board
        import busio
    except ImportError as error:
        print(f"  cannot import the driver: {error}")
        print("  install it with:  pip install adafruit-blinka adafruit-circuitpython-mlx90640")
        return 1

    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        mlx = adafruit_mlx90640.MLX90640(i2c)
        serial = "-".join(format(word, "04x") for word in mlx.serial_number)
        print(f"  found at 0x33, serial {serial}")
    except Exception as error:                   # noqa: BLE001
        print(f"  not responding: {error}")
        print("  check the four wires, and that 'i2cdetect -y 1' shows 33")
        return 1

    print("\n3. Reading frames")
    print(f"  {'rate':>6}  {'ok':>5}  {'failed':>7}  {'avg time':>9}")
    print("  " + "-" * 32)

    frame = [0.0] * 768
    results: dict[int, tuple[int, int, float]] = {}

    for rate in RATES_TO_TRY:
        try:
            mlx.refresh_rate = getattr(adafruit_mlx90640.RefreshRate, f"REFRESH_{rate}_HZ")
        except AttributeError:
            continue

        time.sleep(0.5)                          # let the new rate settle
        ok = failed = 0
        elapsed = 0.0

        for _ in range(FRAMES_PER_TEST):
            start = time.monotonic()
            try:
                mlx.getFrame(frame)
                ok += 1
                elapsed += time.monotonic() - start
            except (RuntimeError, OSError):
                failed += 1

        average = (elapsed / ok * 1000.0) if ok else 0.0
        results[rate] = (ok, failed, average)
        print(f"  {rate:>4} Hz  {ok:>5}  {failed:>7}  {average:>7.0f} ms")

    print("\n4. Verdict")
    working = [rate for rate, (ok, failed, _) in results.items() if ok and not failed]

    if not working:
        print("  No refresh rate worked.")
        print("  The bus speed is almost certainly the cause - see section 1.")
        print("  Check the wiring too: long or loose jumper wires on SDA/SCL")
        print("  cause exactly this failure.")
        return 1

    best = max(working)
    print(f"  These rates work: {', '.join(str(r) + ' Hz' for r in working)}")
    print(f"  Set REFRESH_HZ={best} to use the fastest one that is reliable:")
    print(f"      REFRESH_HZ={best} .venv/bin/python3 v1_thermal_camera.py")
    print("  or put it in the systemd unit as  Environment=REFRESH_HZ="
          f"{best}")
    if speed and speed < 400_000 and best < 16:
        print("\n  You would get a faster, cleaner image by fixing the bus")
        print("  speed as described in section 1.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
