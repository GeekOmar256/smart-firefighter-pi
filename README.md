# Thermal Camera Web Interface

AI-Based Firefighter Assistance System (FYP2) — Raspberry Pi 5 (8 GB).

| File | What it does |
|---|---|
| `v1_thermal_camera.py` | Thermal camera only. Shows the live thermal image on a web page. |
| `v2_thermal_and_sensors.py` | Version 1 plus the flame sensors and the gas sensor, with an indicator for each. |
| `requirements.txt` | The Python packages needed. Python 3.12 / 3.13. |

## Wiring

**MLX90640 thermal camera (SparkFun Qwiic breakout)** — 3.3 V, safe to connect directly.

| Camera | Raspberry Pi |
|---|---|
| GND | GND (pin 6) |
| 3.3V | 3V3 (pin 1) |
| SDA | GPIO 2 / SDA (pin 3) |
| SCL | GPIO 3 / SCL (pin 5) |

**Flame sensors** — power them from **3.3 V** so the D0 output is safe for the Pi.

| Sensor | Raspberry Pi |
|---|---|
| Flame 1 D0 | GPIO 17 |
| Flame 2 D0 | GPIO 27 |

**MQ-2 gas sensor** — needs 5 V for its heater.

| Sensor | Raspberry Pi |
|---|---|
| VCC | 5V (pin 2) |
| GND | GND |
| D0 | GPIO 22 **through a level shifter or resistor divider** |

> The MQ-2 D0 output is 5 V. The Pi GPIO pins accept only 3.3 V and can be
> permanently damaged. Do not connect D0 straight to the Pi.

## Raspberry Pi setup

Enable I2C and raise the bus speed. The MLX90640 sends 768 temperatures per
frame, and at the default 100 kHz the bus is too slow — frames keep timing out.

Add to `/boot/firmware/config.txt`:

```
dtparam=i2c_arm=on
dtparam=i2c_arm_baudrate=1000000
```

Reboot, then confirm the camera answers at address `0x33`:

```bash
sudo apt install -y i2c-tools python3-lgpio
i2cdetect -y 1
```

## Install and run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python3 v1_thermal_camera.py          # or v2_thermal_and_sensors.py
```

Open `http://<raspberry-pi-ip>:5000` in a browser on the same network.

## Running without the hardware

Both scripts take `--simulate`, which replaces the camera and the sensors with
fake ones. This lets you work on the web page from a laptop:

```bash
pip install Flask numpy Pillow
python3 v1_thermal_camera.py --simulate
```

## Settings worth changing

Near the top of each file:

| Setting | Meaning |
|---|---|
| `REFRESH_HZ` | 8 gives a clean image, 16 is faster but noisier. A full frame arrives at about half this rate. |
| `FLIP_HORIZONTAL` / `FLIP_VERTICAL` | Match the image to how the board is mounted. |
| `MIN_SPAN_C` | Smallest temperature range the colour scale may use, so a flat scene does not become amplified noise. |
| `FLAME_SENSOR_PINS`, `GAS_SENSOR_PIN` | Which GPIO pins the sensors use (v2). |
| `SENSORS_ACTIVE_LOW` | `True` if the modules pull D0 low when they detect something, which is the usual behaviour. |

## Note on the Pi 5 and GPIO

`RPi.GPIO` does **not** work on the Raspberry Pi 5, because the GPIO pins sit
behind the RP1 chip instead of on the main processor. Version 2 uses `gpiozero`
driven by `lgpio`, which does work. If `pip install lgpio` cannot build, use the
system package instead: `sudo apt install python3-lgpio`.

---

# Remote control through Firebase (version 3)

`v3_remote.py` is version 2 plus a Firebase link, so the Pi can be watched and
controlled from anywhere — it does not have to be on your network.

| File | What it does |
|---|---|
| `v3_remote.py` | Version 2 plus the Firebase link. This is what runs on the Pi. |
| `cloud_link.py` | Reports status to Firebase and runs the commands it receives. |
| `remote_control.py` | Run this on **your own computer** to see and control the Pi. |
| `database.rules.json` | Who is allowed to read and write which part of the database. |
| `deploy/` | systemd service and sudoers file, needed for restart and update to work. |

## What the Pi reports

Every 10 seconds the Pi writes to
`/smart_firefighter/devices/<device_id>/status`:

- whether it is alive, its IP address and hostname
- **which commit of this repository it is running**, and whether it has
  uncommitted changes
- CPU temperature, uptime, Python version
- camera frame rate and temperature range
- the flame and gas sensor readings

## Commands you can send

From your own computer:

```bash
python3 remote_control.py status     # is it alive, what code is it running
python3 remote_control.py update     # git pull from GitHub, then restart
python3 remote_control.py restart    # restart the program
python3 remote_control.py reboot     # reboot the Pi
python3 remote_control.py ping       # check it answers
python3 remote_control.py history    # recent commands and their results
python3 remote_control.py watch      # live status, refreshed every 5s

python3 remote_control.py config REFRESH_HZ=16
```

`update` is the important one: it runs `git fetch`, `git reset --hard
origin/main`, reinstalls requirements if they changed, and restarts the
service. So you push to GitHub from here, send one command, and the Pi is
running the new code.

Only these five actions exist. There is deliberately **no "run any command"
action** — that would turn the database into a way for anyone who reached it
to run anything on the Pi.

## Setting it up

**1. Get the service account key.** In the
[Firebase console](https://console.firebase.google.com/project/water-analysis-2dy4ar/settings/serviceaccounts/adminsdk)
→ Project settings → Service accounts → *Generate new private key*.

Save it on the Pi, and on your own computer, at:

```
~/.config/smart-firefighter/service-account.json
```

> Never put this file in the repository. The repository is public and the key
> gives full access to the database. `.gitignore` already blocks the usual
> filenames, but the safest habit is to keep it outside the folder entirely.

**2. Clone the repository on the Pi.**

```bash
git clone https://github.com/GeekOmar256/smart-firefighter-pi.git
cd smart-firefighter-pi
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**3. Install the service**, so the Pi starts on boot and can restart itself.

```bash
sudo cp deploy/smart-firefighter.service /etc/systemd/system/
sudo cp deploy/smart-firefighter-sudoers /etc/sudoers.d/smart-firefighter
sudo chmod 0440 /etc/sudoers.d/smart-firefighter
sudo systemctl daemon-reload
sudo systemctl enable --now smart-firefighter
journalctl -u smart-firefighter -f
```

**4. Check it from your computer.**

```bash
pip install firebase-admin
python3 remote_control.py status
```

## Database layout

```
/smart_firefighter/devices/<device_id>/
    status/              the Pi writes this, you read it
    commands/<key>/      you write these, the Pi runs them
    config/              settings the Pi reads back
```

Everything lives under `smart_firefighter/`, so it does not touch anything
else already in the Firebase project.

## Security

The rules in `database.rules.json` are already deployed. They say:

- nothing in the database is readable or writable without signing in
- a device may only write **its own** status node, enforced by its uid
- a command's `action` must be one of the five allowed names

Both the Pi and `remote_control.py` connect with `databaseAuthVariableOverride`,
so they act as `device:<device_id>` and `controller` and the rules apply to
them. Without that override, a service account connects as a full
administrator and **bypasses the rules completely** — which is worth knowing if
you write your own script against this database.

That bypass is why the five allowed actions are also checked a second time on
the Pi itself, in `COMMAND_HANDLERS`. Anything else is answered with
`unknown action` and never runs, whatever managed to get written into the
queue. The database rules are the outer fence; the handler allowlist is the
one that actually decides what executes.

All of this has been tested against the live database:

| Attempt | Result |
|---|---|
| Read or write with no credentials | rejected |
| Command with `action` of `ping`, `update`, `set_config` | accepted |
| Command with `action` of `exec` or `rm -rf /` | rejected |
| Device writing its own status node | accepted |
| Device writing **another** device's status node | rejected |
| Status missing the required fields | rejected |
