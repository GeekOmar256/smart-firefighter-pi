"""Make the spoken alert sounds in sounds/ with the OpenAI text-to-speech API.

Run once on a computer with an OpenAI API key, then commit the WAV files.
The Pi only plays the finished files, so it needs no key and no internet.

    set OPENAI_API_KEY first (it is read from the environment, never saved)
    python tools/generate_sounds.py                 # every alert
    python tools/generate_sounds.py --only gas      # just one
    python tools/generate_sounds.py --voice ash

Each file is a warning tone followed by the spoken message. The tone tells
the firefighter how serious it is before the words arrive:

    critical   three fast high-low beeps
    warning    two beeps
    info       one soft chime

Needs:  pip install openai numpy
"""

from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from alerts import EVENTS, SOUND_DIR  # noqa: E402

# Short and direct: the firefighter hears this through a mask, under stress.
MESSAGES = {
    "flame": "Warning! Flame detected. Move away from the flame.",
    "gas": "Warning! Gas or smoke detected. Check your breathing apparatus.",
    "fire_thermal": "Warning! Fire detected by the thermal camera.",
    "high_heat": "Danger! Extreme heat around you. Withdraw to a safer area.",
    "person": "Attention. Person detected ahead. Check for a casualty.",
    "camera_fault": "Caution. The thermal camera is not responding.",
    "device_overheat": "Caution. The device is overheating.",
    "all_clear": "Hazard cleared.",
    "system_ready": "Firefighter assistance system ready.",
}

STYLE = {
    "critical": "Urgent, loud and firm, like an emergency alarm announcement. "
                "Fast but every word clear. Not panicked.",
    "warning": "Alert and clear, like a radio dispatcher giving an important update.",
    "info": "Calm, steady and reassuring.",
}

SAMPLE_RATE = 24_000          # what the OpenAI API returns for WAV


def tone(level: str) -> np.ndarray:
    """The warning tone in front of the speech, as float samples."""
    def beep(freq: float, seconds: float, volume: float = 0.45) -> np.ndarray:
        t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
        wave_ = np.sin(2 * np.pi * freq * t) + 0.3 * np.sin(2 * np.pi * 2 * freq * t)
        fade = np.minimum(1.0, np.minimum(t, t[::-1]) / 0.01)   # no clicks
        return volume * wave_ / 1.3 * fade

    def gap(seconds: float) -> np.ndarray:
        return np.zeros(int(SAMPLE_RATE * seconds))

    if level == "critical":
        parts = []
        for _ in range(3):
            parts += [beep(1400, 0.11), beep(1000, 0.11), gap(0.06)]
        return np.concatenate(parts + [gap(0.15)])
    if level == "warning":
        return np.concatenate([beep(880, 0.15), gap(0.08), beep(880, 0.15), gap(0.2)])
    t = np.arange(int(SAMPLE_RATE * 0.35)) / SAMPLE_RATE
    chime = 0.35 * np.sin(2 * np.pi * 660 * t) * np.exp(-t * 9)
    return np.concatenate([chime, gap(0.1)])


def wav_samples(data: bytes) -> tuple[np.ndarray, int]:
    """Read 16-bit PCM WAV bytes. Parsed by hand because the API's WAV header
    can carry a placeholder length that the wave module rejects."""
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    position, rate, channels = 12, SAMPLE_RATE, 1
    while position + 8 <= len(data):
        chunk, size = data[position:position + 4], struct.unpack("<I", data[position + 4:position + 8])[0]
        body = position + 8
        if chunk == b"fmt ":
            _, channels, rate = struct.unpack("<HHI", data[body:body + 8])
        elif chunk == b"data":
            end = len(data) if size in (0, 0xFFFFFFFF) or body + size > len(data) else body + size
            pcm = np.frombuffer(data[body:end - (end - body) % 2], dtype="<i2")
            if channels > 1:
                pcm = pcm[: len(pcm) - len(pcm) % channels].reshape(-1, channels).mean(axis=1)
            return pcm.astype(np.float64) / 32768.0, rate
        position = body + size + (size % 2)
    raise ValueError("no audio data in WAV")


def write_wav(path: Path, samples: np.ndarray, rate: int) -> None:
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm.tobytes())
    path.write_bytes(buffer.getvalue())


def speak(client, text: str, level: str, voice: str, model: str) -> bytes:
    options = {"model": model, "voice": voice, "input": text, "response_format": "wav"}
    if model != "tts-1" and model != "tts-1-hd":
        options["instructions"] = STYLE[level]   # only the newer models take a speaking style
    return client.audio.speech.create(**options).read()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the alert sounds")
    parser.add_argument("--only", choices=sorted(MESSAGES), action="append",
                        help="generate just this alert (can repeat)")
    parser.add_argument("--voice", default="onyx")
    parser.add_argument("--model", default="gpt-4o-mini-tts")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set.", file=sys.stderr)
        return 1

    from openai import OpenAI
    client = OpenAI()

    SOUND_DIR.mkdir(exist_ok=True)
    manifest_path = SOUND_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    model = args.model
    for event in args.only or list(MESSAGES):
        level = EVENTS[event][0]
        text = MESSAGES[event]
        try:
            audio = speak(client, text, level, args.voice, model)
        except Exception as error:               # noqa: BLE001
            if model == "tts-1":
                raise
            print(f"  {model} failed ({error}); falling back to tts-1")
            model = "tts-1"
            audio = speak(client, text, level, args.voice, model)

        speech, rate = wav_samples(audio)
        peak = float(np.abs(speech).max()) or 1.0
        speech = speech * (0.9 / peak)                       # same loudness for every alert
        if rate != SAMPLE_RATE:
            raise ValueError(f"unexpected sample rate {rate}")
        samples = np.concatenate([tone(level), speech, np.zeros(int(rate * 0.2))])

        path = SOUND_DIR / f"{event}.wav"
        write_wav(path, samples, rate)
        manifest[event] = {
            "level": level,
            "text": text,
            "voice": args.voice,
            "model": model,
            "seconds": round(len(samples) / rate, 2),
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        }
        print(f"  {event:16s} {len(samples) / rate:4.1f} s  \"{text}\"")

    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Saved to {SOUND_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
