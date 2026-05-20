"""Push-to-talk dictation daemon for Linux/Wayland.

Hold Right Ctrl to record, release to transcribe via Groq Whisper, then the
transcript lands on the Wayland clipboard. Paste with Ctrl+V.
"""

from __future__ import annotations

import io
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import wave

import evdev
import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from evdev import InputDevice, ecodes
from groq import Groq

load_dotenv()

HOTKEY_NAME = os.environ.get("SCRIBE_HOTKEY", "KEY_RIGHTCTRL").strip()
HOTKEY = getattr(ecodes, HOTKEY_NAME, None)
SAMPLE_RATE = 16000
CHANNELS = 1
DTYPE = "int16"
MIN_DURATION_S = float(os.environ.get("SCRIBE_MIN_DURATION_S", "0.3"))
MAX_DURATION_S = float(os.environ.get("SCRIBE_MAX_DURATION_S", "60.0"))
MODEL = os.environ.get("SCRIBE_MODEL", "whisper-large-v3-turbo").strip()
PROMPT = os.environ.get("SCRIBE_PROMPT", "").strip()

log = logging.getLogger("scribe")

stop_event = threading.Event()
state_lock = threading.Lock()

# Active recording session — None when idle.
session: "Recording | None" = None


class Recording:
    """An in-flight microphone capture."""

    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []
        self.t_start = time.monotonic()
        self.stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=DTYPE,
            callback=self._on_audio,
        )
        self.stream.start()
        self.watchdog = threading.Timer(MAX_DURATION_S, self._on_timeout)
        self.watchdog.daemon = True
        self.watchdog.start()

    def _on_audio(self, indata, frames, time_info, status) -> None:
        if status:
            log.warning("audio status: %s", status)
        self.chunks.append(indata.copy())

    def _on_timeout(self) -> None:
        log.info("max duration reached, stopping")
        on_release()

    def finish(self) -> tuple[float, np.ndarray | None]:
        self.watchdog.cancel()
        try:
            self.stream.stop()
            self.stream.close()
        except Exception as e:
            log.warning("stream close failed: %s", e)
        duration = time.monotonic() - self.t_start
        if not self.chunks:
            return duration, None
        return duration, np.concatenate(self.chunks, axis=0)


def find_keyboards() -> list[InputDevice]:
    devices = []
    for path in evdev.list_devices():
        try:
            dev = InputDevice(path)
        except PermissionError:
            log.error("permission denied on %s — are you in the 'input' group?", path)
            continue
        except OSError as e:
            log.debug("skipping %s: %s", path, e)
            continue
        keys = dev.capabilities().get(ecodes.EV_KEY, [])
        if HOTKEY in keys:
            devices.append(dev)
    return devices


def on_press() -> None:
    global session
    with state_lock:
        if session is not None:
            return
        try:
            session = Recording()
        except Exception as e:
            log.error("failed to start recording: %s", e)
            session = None
            return
    log.info("recording…")


def on_release() -> None:
    global session
    with state_lock:
        if session is None:
            return
        local = session
        session = None
    duration, audio = local.finish()
    if audio is None or duration < MIN_DURATION_S:
        log.info("skipped (too short: %.2fs)", duration)
        return
    threading.Thread(
        target=transcribe_and_copy,
        args=(audio, duration),
        daemon=True,
    ).start()


def transcribe_and_copy(audio: np.ndarray, duration: float) -> None:
    log.info("transcribing %.2fs of audio", duration)
    try:
        wav_bytes = encode_wav(audio)
        client = Groq()
        kwargs: dict = {
            "file": ("audio.wav", wav_bytes, "audio/wav"),
            "model": MODEL,
            "response_format": "text",
        }
        if PROMPT:
            kwargs["prompt"] = PROMPT
        text = client.audio.transcriptions.create(**kwargs)
        # SDK returns either a str or an object with .text depending on version.
        transcript = (text if isinstance(text, str) else getattr(text, "text", "")).strip()
        if not transcript:
            log.info("empty transcript")
            return
        subprocess.run(["wl-copy"], input=transcript, text=True, check=True)
        preview = transcript if len(transcript) <= 80 else transcript[:80] + "…"
        log.info('copied: "%s"', preview)
        if auto_paste_enabled():
            paste(transcript)
    except Exception as e:
        log.error("transcription failed: %s", e)


def auto_paste_enabled() -> bool:
    val = os.environ.get("SCRIBE_NO_AUTO_PASTE", "").strip().lower()
    return val not in ("1", "true", "yes", "on")


# Keycodes for paste shortcuts.
_KEY_LEFTCTRL = 29
_KEY_LEFTSHIFT = 42
_KEY_V = 47

_PASTE_SHORTCUTS = {
    # Default: works in terminals (Claude Code, gnome-terminal, kitty) and most
    # Electron apps. Won't fire in native GTK/GNOME apps like gnome-text-editor.
    "shortcut": [_KEY_LEFTCTRL, _KEY_LEFTSHIFT, _KEY_V],
    # GTK/GNOME apps: Ctrl+V. Do NOT use this when a terminal is focused —
    # Ctrl+V there inserts a literal ^V character instead of pasting.
    "ctrl_v": [_KEY_LEFTCTRL, _KEY_V],
}


def _ydotool_env() -> dict[str, str]:
    # ydotool's compiled default socket path (/run/user/$UID/.ydotool_socket)
    # doesn't exist at boot, so the matching ydotoold systemd unit can't bind
    # there. We point both daemon and client at /tmp/.ydotool_socket instead.
    env = os.environ.copy()
    env.setdefault("YDOTOOL_SOCKET", "/tmp/.ydotool_socket")
    return env


def paste(text: str) -> None:
    mode = os.environ.get("SCRIBE_PASTE_MODE", "shortcut").strip().lower()
    if mode == "type":
        return paste_via_type(text)
    keycodes = _PASTE_SHORTCUTS.get(mode)
    if keycodes is None:
        log.warning("unknown SCRIBE_PASTE_MODE=%r — falling back to default", mode)
        keycodes = _PASTE_SHORTCUTS["shortcut"]
    return paste_via_shortcut(keycodes)


def paste_via_shortcut(keycodes: list[int]) -> None:
    # Press modifiers + key, then release in reverse. Text is already in the
    # clipboard, so a single keystroke is effectively instant regardless of
    # transcript length.
    args = ["ydotool", "key"]
    args += [f"{k}:1" for k in keycodes]
    args += [f"{k}:0" for k in reversed(keycodes)]
    time.sleep(0.03)
    try:
        subprocess.run(args, check=True, capture_output=True, env=_ydotool_env())
        log.info("pasted")
    except FileNotFoundError:
        log.warning(
            "ydotool not installed — skipping auto-paste "
            "(install: sudo dnf install ydotool; sudo systemctl enable --now ydotool)"
        )
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="replace").strip()
        log.warning("auto-paste failed: %s", stderr or e)


def paste_via_type(text: str) -> None:
    # Fallback: type each character through ydotool. Slower but bypasses any
    # paste-shortcut quirks an app might have. ~10ms/char with these settings.
    time.sleep(0.1)
    try:
        subprocess.run(
            ["ydotool", "type", "--key-delay=10", "--key-hold=10", "--file", "-"],
            input=text,
            text=True,
            check=True,
            capture_output=True,
            env=_ydotool_env(),
        )
        log.info("pasted (typed)")
    except FileNotFoundError:
        log.warning("ydotool not installed — skipping auto-paste")
    except subprocess.CalledProcessError as e:
        stderr = (e.stderr or b"").decode(errors="replace").strip()
        log.warning("auto-paste failed: %s", stderr or e)


def encode_wav(audio: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)  # int16
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())
    return buf.getvalue()


def watch_device(dev: InputDevice) -> None:
    try:
        for event in dev.read_loop():
            if stop_event.is_set():
                return
            if event.type != ecodes.EV_KEY or event.code != HOTKEY:
                continue
            if event.value == 1:
                on_press()
            elif event.value == 0:
                on_release()
            # value == 2 is autorepeat; ignore.
    except OSError as e:
        log.warning("device %s disconnected: %s", dev.path, e)
    except Exception as e:
        log.error("device %s thread crashed: %s", dev.path, e)


def shutdown(signum, frame) -> None:
    log.info("shutting down")
    stop_event.set()
    with state_lock:
        global session
        if session is not None:
            try:
                session.stream.stop()
                session.stream.close()
                session.watchdog.cancel()
            except Exception:
                pass
            session = None
    sys.exit(0)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not isinstance(HOTKEY, int):
        log.error(
            "invalid SCRIBE_HOTKEY=%r — must be an evdev key name like KEY_RIGHTCTRL",
            HOTKEY_NAME,
        )
        return 1
    if not os.environ.get("GROQ_API_KEY"):
        log.error("GROQ_API_KEY not set — copy .env.example to .env and paste your key")
        return 1

    devices = find_keyboards()
    if not devices:
        log.error(
            "no keyboards with KEY_RIGHTCTRL found. "
            "is your user in the 'input' group? "
            "(sudo usermod -aG input $USER, then log out and back in)"
        )
        return 1

    names = [f"{d.name!r} ({d.path})" for d in devices]
    log.info("ready. watching %d device(s): %s", len(devices), ", ".join(names))

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threads = []
    for dev in devices:
        t = threading.Thread(target=watch_device, args=(dev,), daemon=True)
        t.start()
        threads.append(t)

    # Park the main thread; signal handlers will sys.exit.
    while not stop_event.is_set():
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
