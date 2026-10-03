"""Firebase link for the AI-Based Firefighter Assistance System.

Gives the Raspberry Pi two things:

  * it reports its status to Firebase, so you can see from anywhere whether it
    is alive, what code it is running and what the sensors are reading;
  * it watches a command queue in Firebase, so you can tell it to pull the
    latest code from GitHub, restart, or reboot, without being on the same
    network as the Pi.

Database layout (all under one root so it does not collide with anything else
already in the project):

    /smart_firefighter/devices/<device_id>/status            the Pi writes
    /smart_firefighter/devices/<device_id>/commands/<key>    you write
    /smart_firefighter/devices/<device_id>/config            you write

Only the actions in COMMAND_HANDLERS can ever run. There is deliberately no
"run any shell command" action: the database would then be a way for anyone
who reached it to run anything on the Pi.

Tested against Python 3.12 and 3.13.
"""

from __future__ import annotations

import os
import platform
import socket
import subprocess
import threading
import time
import traceback
from pathlib import Path

import firebase_admin
from firebase_admin import credentials, db

# --------------------------------------------------------------------------
# Configuration (environment variables win, so nothing secret lives in code)
# --------------------------------------------------------------------------

DATABASE_URL = os.environ.get(
    "FIREBASE_DATABASE_URL",
    "https://water-analysis-2dy4ar-default-rtdb.firebaseio.com",
)

# Path to the service account JSON. Never put this file inside the repository.
CREDENTIALS_PATH = os.environ.get(
    "FIREBASE_CREDENTIALS",
    str(Path.home() / ".config" / "smart-firefighter" / "service-account.json"),
)

DEVICE_ID = os.environ.get("DEVICE_ID", socket.gethostname())

# Where the code lives on the Pi, used by the "update" command.
REPO_DIR = os.environ.get("REPO_DIR", str(Path(__file__).resolve().parent))

# systemd unit used by the "restart" command.
SERVICE_NAME = os.environ.get("SERVICE_NAME", "smart-firefighter")

ROOT_PATH = "/smart_firefighter/devices"

HEARTBEAT_SECONDS = 10
COMMAND_TIMEOUT_SECONDS = 180


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def run(command: list[str], cwd: str | None = None, timeout: int = 120) -> dict:
    """Run a command and capture what happened, without raising."""
    try:
        finished = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "command": " ".join(command),
            "returncode": finished.returncode,
            "stdout": finished.stdout[-2000:],
            "stderr": finished.stderr[-2000:],
        }
    except FileNotFoundError:
        return {"command": " ".join(command), "returncode": 127,
                "stdout": "", "stderr": "command not found"}
    except subprocess.TimeoutExpired:
        return {"command": " ".join(command), "returncode": 124,
                "stdout": "", "stderr": f"timed out after {timeout}s"}


def git_description(repo_dir: str) -> dict:
    """Which commit the Pi is currently running."""
    def git(*args: str) -> str:
        result = run(["git", *args], cwd=repo_dir, timeout=20)
        return result["stdout"].strip() if result["returncode"] == 0 else ""

    return {
        "commit": git("rev-parse", "--short", "HEAD"),
        "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
        "message": git("log", "-1", "--pretty=%s"),
        "dirty": bool(git("status", "--porcelain")),
    }


def cpu_temperature_c() -> float | None:
    """Raspberry Pi CPU temperature, or None when it cannot be read."""
    try:
        raw = Path("/sys/class/thermal/thermal_zone0/temp").read_text().strip()
        return round(int(raw) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def uptime_seconds() -> float | None:
    try:
        return round(float(Path("/proc/uptime").read_text().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return None


def local_ip() -> str:
    """Best guess at the address the Pi is reachable on."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))     # no packet is actually sent
        return probe.getsockname()[0]
    except OSError:
        return "unknown"
    finally:
        probe.close()


# --------------------------------------------------------------------------
# Command handlers
# --------------------------------------------------------------------------

def cmd_ping(link: "CloudLink", params: dict) -> dict:
    return {"message": "pong", "device": DEVICE_ID}


def cmd_update(link: "CloudLink", params: dict) -> dict:
    """Pull the newest code from GitHub and install any new requirements."""
    branch = params.get("branch", "main")
    steps = [
        run(["git", "fetch", "--all", "--prune"], cwd=REPO_DIR, timeout=120),
        run(["git", "reset", "--hard", f"origin/{branch}"], cwd=REPO_DIR, timeout=60),
    ]

    if params.get("install_requirements", True):
        requirements = Path(REPO_DIR) / "requirements.txt"
        if requirements.exists():
            steps.append(run(
                ["python3", "-m", "pip", "install", "-r", str(requirements)],
                cwd=REPO_DIR,
                timeout=COMMAND_TIMEOUT_SECONDS,
            ))

    failed = [s for s in steps if s["returncode"] != 0]
    result = {
        "steps": steps,
        "git": git_description(REPO_DIR),
        "ok": not failed,
    }

    # Restarting kills this process, so the result is written out first and the
    # restart is left to a short-lived background thread.
    if not failed and params.get("restart", True):
        result["restarting"] = True
        link.schedule_restart(delay=3.0)

    return result


def cmd_restart(link: "CloudLink", params: dict) -> dict:
    link.schedule_restart(delay=3.0)
    return {"message": f"restarting {SERVICE_NAME} in 3s"}


def cmd_reboot(link: "CloudLink", params: dict) -> dict:
    link.schedule(lambda: run(["sudo", "reboot"], timeout=20), delay=3.0)
    return {"message": "rebooting in 3s"}


def cmd_set_config(link: "CloudLink", params: dict) -> dict:
    """Store settings the running program can read back from /config."""
    settings = params.get("settings") or {}
    if not isinstance(settings, dict):
        return {"ok": False, "error": "settings must be an object"}
    link.config_ref.update(settings)
    return {"ok": True, "settings": settings}


COMMAND_HANDLERS = {
    "ping": cmd_ping,
    "update": cmd_update,
    "restart": cmd_restart,
    "reboot": cmd_reboot,
    "set_config": cmd_set_config,
}


# --------------------------------------------------------------------------
# The link itself
# --------------------------------------------------------------------------

class CloudLink:
    """Keeps the Pi's status in Firebase and runs the commands it is given."""

    def __init__(
        self,
        device_id: str = DEVICE_ID,
        database_url: str = DATABASE_URL,
        credentials_path: str = CREDENTIALS_PATH,
    ) -> None:
        self.device_id = device_id
        self.started_at = time.time()
        self._status_extra: dict = {}
        self._lock = threading.Lock()
        self._seen_commands: set[str] = set()

        key_file = Path(credentials_path)
        if not key_file.exists():
            raise FileNotFoundError(
                f"Firebase service account key not found at {key_file}.\n"
                "Download it from the Firebase console "
                "(Project settings > Service accounts > Generate new private key) "
                "and either save it there or point FIREBASE_CREDENTIALS at it."
            )

        # The Pi acts as this uid rather than as a full administrator, so the
        # database rules in database.rules.json still apply to it.
        firebase_admin.initialize_app(
            credentials.Certificate(str(key_file)),
            {
                "databaseURL": database_url,
                "databaseAuthVariableOverride": {"uid": f"device:{device_id}"},
            },
        )

        base = f"{ROOT_PATH}/{device_id}"
        self.status_ref = db.reference(f"{base}/status")
        self.commands_ref = db.reference(f"{base}/commands")
        self.config_ref = db.reference(f"{base}/config")

        print(f"[cloud] connected to {database_url} as device '{device_id}'")

    # -- status ----------------------------------------------------------

    def set_status_source(self, callback) -> None:
        """Register a function returning extra status fields (sensors, fps)."""
        self._status_source = callback

    def publish_status(self) -> None:
        payload = {
            "device_id": self.device_id,
            "online": True,
            "last_seen": int(time.time() * 1000),
            "ip": local_ip(),
            "hostname": socket.gethostname(),
            "uptime_s": uptime_seconds(),
            "cpu_temp_c": cpu_temperature_c(),
            "python": platform.python_version(),
            "app_uptime_s": round(time.time() - self.started_at, 1),
            "git": git_description(REPO_DIR),
        }

        source = getattr(self, "_status_source", None)
        if source is not None:
            try:
                payload.update(source() or {})
            except Exception as error:            # noqa: BLE001
                payload["status_source_error"] = str(error)

        with self._lock:
            payload.update(self._status_extra)

        self.status_ref.set(payload)

    def heartbeat_loop(self) -> None:
        while True:
            try:
                self.publish_status()
            except Exception as error:            # noqa: BLE001
                print(f"[cloud] status update failed: {error}")
            time.sleep(HEARTBEAT_SECONDS)

    # -- commands --------------------------------------------------------

    def _handle(self, key: str, command: dict) -> None:
        action = command.get("action")
        params = command.get("params") or {}
        entry = self.commands_ref.child(key)

        handler = COMMAND_HANDLERS.get(action)
        if handler is None:
            entry.update({
                "status": "error",
                "error": f"unknown action '{action}'",
                "allowed": sorted(COMMAND_HANDLERS),
                "finished_at": int(time.time() * 1000),
            })
            return

        print(f"[cloud] running command '{action}' ({key})")
        entry.update({"status": "running", "started_at": int(time.time() * 1000)})

        try:
            result = handler(self, params)
            entry.update({
                "status": "done",
                "result": result,
                "finished_at": int(time.time() * 1000),
            })
            print(f"[cloud] command '{action}' finished")
        except Exception as error:                # noqa: BLE001
            entry.update({
                "status": "error",
                "error": str(error),
                "traceback": traceback.format_exc()[-2000:],
                "finished_at": int(time.time() * 1000),
            })
            print(f"[cloud] command '{action}' failed: {error}")

    def poll_commands(self) -> None:
        """Pick up any command still marked pending and run it."""
        pending = self.commands_ref.order_by_child("status").equal_to("pending").get()
        if not pending:
            return
        for key, command in sorted(pending.items(), key=lambda kv: kv[0]):
            if key in self._seen_commands:
                continue
            self._seen_commands.add(key)
            self._handle(key, command or {})

    def command_loop(self, interval: float = 2.0) -> None:
        while True:
            try:
                self.poll_commands()
            except Exception as error:            # noqa: BLE001
                print(f"[cloud] command poll failed: {error}")
            time.sleep(interval)

    # -- deferred actions ------------------------------------------------

    def schedule(self, action, delay: float) -> None:
        """Run something after a delay, on a throwaway thread."""
        def later() -> None:
            time.sleep(delay)
            action()
        threading.Thread(target=later, daemon=True).start()

    def schedule_restart(self, delay: float) -> None:
        def restart() -> None:
            result = run(["sudo", "systemctl", "restart", SERVICE_NAME], timeout=30)
            if result["returncode"] != 0:
                # Not running under systemd: just exit and let whatever
                # supervises this process start it again.
                print(f"[cloud] systemctl restart failed: {result['stderr']}")
                os._exit(0)
        self.schedule(restart, delay)

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the heartbeat and the command listener in the background."""
        self.publish_status()
        threading.Thread(target=self.heartbeat_loop, daemon=True).start()
        threading.Thread(target=self.command_loop, daemon=True).start()
        print("[cloud] heartbeat and command listener running")
