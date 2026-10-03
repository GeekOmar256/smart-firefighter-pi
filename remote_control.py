"""Control the Raspberry Pi from your own computer, through Firebase.

    python3 remote_control.py status            # is the Pi alive, what is it running
    python3 remote_control.py update            # pull the latest code from GitHub
    python3 remote_control.py restart           # restart the program
    python3 remote_control.py reboot            # reboot the Pi
    python3 remote_control.py ping              # check the Pi answers
    python3 remote_control.py config REFRESH_HZ=16
    python3 remote_control.py history           # recent commands and their results
    python3 remote_control.py watch             # live status, refreshed every 5s

Add --device <name> if you have more than one Pi.

Tested against Python 3.12 and 3.13.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, db

DATABASE_URL = os.environ.get(
    "FIREBASE_DATABASE_URL",
    "https://water-analysis-2dy4ar-default-rtdb.firebaseio.com",
)
CREDENTIALS_PATH = os.environ.get(
    "FIREBASE_CREDENTIALS",
    str(Path.home() / ".config" / "smart-firefighter" / "service-account.json"),
)
DEFAULT_DEVICE = os.environ.get("DEVICE_ID", "raspberrypi")

ROOT_PATH = "/smart_firefighter/devices"
ONLINE_WITHIN_SECONDS = 30
WAIT_TIMEOUT_SECONDS = 240


def connect() -> None:
    key_file = Path(CREDENTIALS_PATH)
    if not key_file.exists():
        sys.exit(
            f"Firebase service account key not found at {key_file}.\n"
            "Download it from the Firebase console "
            "(Project settings > Service accounts > Generate new private key), "
            "or point FIREBASE_CREDENTIALS at it."
        )
    firebase_admin.initialize_app(
        credentials.Certificate(str(key_file)),
        {"databaseURL": DATABASE_URL},
    )


def ago(milliseconds: int | None) -> str:
    if not milliseconds:
        return "never"
    seconds = time.time() - milliseconds / 1000.0
    if seconds < 60:
        return f"{seconds:.0f}s ago"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m ago"
    return f"{seconds / 3600:.1f}h ago"


def clock(milliseconds: int | None) -> str:
    if not milliseconds:
        return "-"
    moment = datetime.fromtimestamp(milliseconds / 1000.0, tz=timezone.utc)
    return moment.astimezone().strftime("%H:%M:%S")


def show_status(device: str) -> None:
    status = db.reference(f"{ROOT_PATH}/{device}/status").get()
    if not status:
        print(f"No status for device '{device}'. Has it ever connected?")
        return

    last_seen = status.get("last_seen")
    online = last_seen and (time.time() - last_seen / 1000.0) < ONLINE_WITHIN_SECONDS

    git = status.get("git") or {}
    camera = status.get("camera") or {}
    readings = status.get("sensors") or {}

    print(f"Device     : {status.get('device_id', device)}")
    print(f"State      : {'ONLINE' if online else 'OFFLINE'}  (last seen {ago(last_seen)})")
    print(f"Address    : {status.get('ip', '-')}  ({status.get('hostname', '-')})")
    print(f"Code       : {git.get('branch', '-')} @ {git.get('commit', '-')}"
          f"{'  [uncommitted changes]' if git.get('dirty') else ''}")
    if git.get("message"):
        print(f"             \"{git['message']}\"")
    print(f"CPU temp   : {status.get('cpu_temp_c', '-')} C")
    print(f"Python     : {status.get('python', '-')}")
    print(f"App uptime : {status.get('app_uptime_s', '-')} s")

    if camera:
        print(f"Camera     : {camera.get('fps', '-')} fps, "
              f"{camera.get('min_c', '-')} to {camera.get('max_c', '-')} C")
    if readings:
        flame = readings.get("flame") or []
        marks = ", ".join("DETECTED" if f else "clear" for f in flame) or "-"
        print(f"Flame      : {marks}")
        print(f"Gas        : {'DETECTED' if readings.get('gas') else 'clear'}")
        if readings.get("alarm"):
            print("ALARM      : hazard detected")


def send_command(device: str, action: str, params: dict | None, wait: bool) -> int:
    commands = db.reference(f"{ROOT_PATH}/{device}/commands")
    entry = commands.push({
        "action": action,
        "params": params or {},
        "status": "pending",
        "issued_at": int(time.time() * 1000),
        "issued_by": os.environ.get("USERNAME") or os.environ.get("USER") or "controller",
    })
    print(f"Sent '{action}' to {device}  (id {entry.key})")

    if not wait:
        return 0

    print("Waiting for the Pi to pick it up ...")
    deadline = time.time() + WAIT_TIMEOUT_SECONDS
    last_state = None

    while time.time() < deadline:
        record = entry.get() or {}
        state = record.get("status")

        if state != last_state:
            print(f"  status: {state}")
            last_state = state

        if state == "done":
            result = record.get("result") or {}
            print("\nResult:")
            print(json.dumps(result, indent=2)[:4000])
            return 0 if result.get("ok", True) else 1

        if state == "error":
            print(f"\nFailed: {record.get('error')}")
            if record.get("traceback"):
                print(record["traceback"])
            return 1

        time.sleep(2)

    print("Timed out waiting for a reply. Is the Pi online?")
    return 1


def show_history(device: str, limit: int) -> None:
    commands = db.reference(f"{ROOT_PATH}/{device}/commands").order_by_key().limit_to_last(limit).get()
    if not commands:
        print("No commands have been sent yet.")
        return

    print(f"{'issued':<10} {'action':<12} {'status':<9} {'finished':<10}")
    print("-" * 45)
    for key, record in sorted(commands.items()):
        record = record or {}
        print(f"{clock(record.get('issued_at')):<10} "
              f"{str(record.get('action', '-')):<12} "
              f"{str(record.get('status', '-')):<9} "
              f"{clock(record.get('finished_at')):<10}")


def parse_settings(pairs: list[str]) -> dict:
    """Turn KEY=VALUE arguments into a dict, guessing the value type."""
    settings: dict = {}
    for pair in pairs:
        if "=" not in pair:
            sys.exit(f"Expected KEY=VALUE, got '{pair}'")
        key, _, raw = pair.partition("=")
        try:
            value = json.loads(raw)          # numbers, true/false, lists
        except json.JSONDecodeError:
            value = raw                      # plain string
        settings[key.strip()] = value
    return settings


def main() -> int:
    parser = argparse.ArgumentParser(description="Remote control for the Raspberry Pi")
    parser.add_argument("--device", default=DEFAULT_DEVICE,
                        help=f"device id (default: {DEFAULT_DEVICE})")
    parser.add_argument("--no-wait", action="store_true",
                        help="send the command and exit without waiting")

    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show what the Pi is doing")
    sub.add_parser("ping", help="check the Pi answers")
    sub.add_parser("restart", help="restart the program on the Pi")
    sub.add_parser("reboot", help="reboot the Pi")

    update = sub.add_parser("update", help="pull the latest code from GitHub")
    update.add_argument("--branch", default="main")
    update.add_argument("--no-restart", action="store_true",
                        help="update the files but keep the old code running")

    config = sub.add_parser("config", help="change settings, as KEY=VALUE")
    config.add_argument("settings", nargs="+")

    history = sub.add_parser("history", help="recent commands and their results")
    history.add_argument("--limit", type=int, default=10)

    watch = sub.add_parser("watch", help="keep showing the status")
    watch.add_argument("--interval", type=int, default=5)

    args = parser.parse_args()
    connect()
    wait = not args.no_wait

    if args.command == "status":
        show_status(args.device)
        return 0

    if args.command == "history":
        show_history(args.device, args.limit)
        return 0

    if args.command == "watch":
        try:
            while True:
                print("\033[2J\033[H", end="")          # clear the screen
                print(f"-- {datetime.now().strftime('%H:%M:%S')} --\n")
                show_status(args.device)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0

    if args.command == "update":
        return send_command(args.device, "update", {
            "branch": args.branch,
            "restart": not args.no_restart,
        }, wait)

    if args.command == "config":
        return send_command(args.device, "set_config",
                            {"settings": parse_settings(args.settings)}, wait)

    return send_command(args.device, args.command, {}, wait)


if __name__ == "__main__":
    raise SystemExit(main())
