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
import pyudev
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

# Paths we're currently watching. Owned by try_watch (adds) and watch_device's
# finally (removes). Lock protects the set itself, not the spawned threads.
watched_paths: set[str] = set()
watched_lock = threading.Lock()


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

    def _on_audio(self, indata, _frames, _time_info, status) -> None:
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


def try_watch(path: str, announce: bool = True) -> bool:
    """Open the input device at `path` and spawn a watcher if it has HOTKEY.

    Returns True if a watcher was started. Idempotent — calling twice with the
    same path returns False the second time (the first call already claimed it).
    """
    with watched_lock:
        if path in watched_paths:
            return False
        watched_paths.add(path)

    def _release() -> None:
        with watched_lock:
            watched_paths.discard(path)

    try:
        dev = InputDevice(path)
    except PermissionError:
        log.error("permission denied on %s — are you in the 'input' group?", path)
        _release()
        return False
    except OSError as e:
        log.debug("skipping %s: %s", path, e)
        _release()
        return False

    if HOTKEY not in dev.capabilities().get(ecodes.EV_KEY, []):
        try:
            dev.close()
        except Exception:
            pass
        _release()
        return False

    if announce:
        log.info("new keyboard: %r (%s)", dev.name, dev.path)
    threading.Thread(target=watch_device, args=(dev,), daemon=True).start()
    return True


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
    finally:
        with watched_lock:
            watched_paths.discard(dev.path)
        try:
            dev.close()
        except Exception:
            pass


def udev_watcher() -> None:
    """Listen for new input devices being added and bind to any with HOTKEY."""
    try:
        context = pyudev.Context()
        monitor = pyudev.Monitor.from_netlink(context)
        monitor.filter_by(subsystem="input")
        monitor.start()
    except Exception as e:
        log.error("udev watcher failed to start: %s — hotplug disabled", e)
        return

    while not stop_event.is_set():
        try:
            device = monitor.poll(timeout=1.0)
        except Exception as e:
            log.error("udev poll error: %s", e)
            time.sleep(1.0)
            continue
        if device is None:
            continue
        if device.action != "add":
            continue
        path = device.device_node
        if not path or not path.startswith("/dev/input/event"):
            continue
        # Brief settle so the kernel finishes setting up the node + perms.
        time.sleep(0.1)
        try_watch(path)


def shutdown(_signum, _frame) -> None:
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

    for path in evdev.list_devices():
        try_watch(path, announce=False)

    if not watched_paths:
        log.error(
            "no keyboards with %s found. "
            "is your user in the 'input' group? "
            "(sudo usermod -aG input $USER, then log out and back in)",
            HOTKEY_NAME,
        )
        return 1

    log.info("ready. watching %d device(s)", len(watched_paths))

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    threading.Thread(target=udev_watcher, daemon=True).start()

    # Park the main thread; signal handlers will sys.exit.
    while not stop_event.is_set():
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
