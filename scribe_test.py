"""One-shot record + transcribe, used by `scribe test`.

Press Enter to start recording. A live waveform pulses in the terminal while
audio is captured; press any key to stop. The transcript prints to stdout —
nothing touches the clipboard, ydotool, or the systemd service.
"""

from __future__ import annotations

import io
import math
import os
import select
import sys
import termios
import threading
import time
import wave
from collections import deque
from typing import NoReturn

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

SAMPLE_RATE = 16000
MODEL = os.environ.get("SCRIBE_MODEL", "whisper-large-v3-turbo").strip()
PROMPT = os.environ.get("SCRIBE_PROMPT", "").strip()
MAX_DURATION_S = 60.0  # safety cap so the script can't hang forever

if sys.stdout.isatty():
    R = "\033[31m"
    G = "\033[32m"
    Y = "\033[33m"
    C = "\033[36m"
    D = "\033[2m"
    N = "\033[0m"
else:
    R = G = Y = C = D = N = ""

# Waveform display
BARS = "▁▂▃▄▅▆▇█"
WAVE_WIDTH = 32
# Logarithmic (dBFS) scale matches how ears perceive loudness — quiet speech
# isn't a fingernail-thin bar like it would be on a linear scale. -60 dB is
# the floor (bottom bar); 0 dB is full-scale (top bar, near clipping).
DBFS_FLOOR = -60.0
INT16_MAX = 32767.0

_amp_lock = threading.Lock()
_amplitudes: deque[float] = deque([0.0] * WAVE_WIDTH, maxlen=WAVE_WIDTH)


def rms_to_norm(rms: float) -> float:
    """Convert int16 RMS to [0, 1] bar height on a dBFS log scale."""
    if rms < 1.0:
        return 0.0
    dbfs = 20.0 * math.log10(rms / INT16_MAX)
    return max(0.0, min(1.0, (dbfs - DBFS_FLOOR) / -DBFS_FLOOR))


def die(msg: str, code: int = 1) -> NoReturn:
    print(f"  {R}✗{N} {msg}", file=sys.stderr)
    sys.exit(code)


def render_wave() -> str:
    """Render the current amplitudes as a colored bar string."""
    out: list[str] = []
    with _amp_lock:
        snapshot = list(_amplitudes)
    for a in snapshot:
        idx = min(len(BARS) - 1, int(a * len(BARS)))
        ch = BARS[idx]
        if a > 0.92:
            out.append(f"{R}{ch}{N}")  # >-5 dBFS — clipping risk
        elif a > 0.80:
            out.append(f"{Y}{ch}{N}")  # >-12 dBFS — loud
        else:
            out.append(f"{C}{ch}{N}")
    return "".join(out)


def record_interactive() -> tuple[float, list[np.ndarray]]:
    """Open the mic, render a live waveform until any key, return audio + duration."""
    chunks: list[np.ndarray] = []

    def callback(indata, _frames, _time_info, status) -> None:  # pyright: ignore[reportMissingParameterType]
        if status:
            # Drop status warnings on stderr would clobber the waveform line.
            pass
        chunks.append(indata.copy())
        if indata.size > 0:
            rms = float(np.sqrt(np.mean(indata.astype(np.float64) ** 2)))
            with _amp_lock:
                _amplitudes.append(rms_to_norm(rms))

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=512,  # ~32ms per callback for a smooth waveform
            callback=callback,
        )
        stream.start()
    except Exception as e:
        die(f"failed to open mic: {e}")

    use_anim = sys.stdout.isatty() and sys.stdin.isatty()
    fd = sys.stdin.fileno() if use_anim else -1
    old_term = None

    if use_anim:
        try:
            old_term = termios.tcgetattr(fd)
            new_term = termios.tcgetattr(fd)
            # Disable ECHO and ICANON so keystrokes don't echo or buffer to EOL.
            new_term[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(fd, termios.TCSANOW, new_term)
            sys.stdout.write("\033[?25l")  # hide cursor
            sys.stdout.flush()
        except (termios.error, OSError):
            use_anim = False
            old_term = None

    t_start = time.monotonic()
    try:
        if use_anim:
            while True:
                ready, _, _ = select.select([sys.stdin], [], [], 0.04)
                if ready:
                    _ = sys.stdin.read(1)
                    break
                elapsed = time.monotonic() - t_start
                if elapsed > MAX_DURATION_S:
                    break
                sys.stdout.write(
                    f"\r\033[K  {G}●{N} {D}recording{N}  "
                    f"{D}{elapsed:5.1f}s{N}  {render_wave()}  "
                    f"{D}(any key to stop){N}"
                )
                sys.stdout.flush()
        else:
            try:
                _ = input()
            except (EOFError, KeyboardInterrupt):
                pass
    finally:
        if old_term is not None:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old_term)
            except (termios.error, OSError):
                pass
        if use_anim:
            sys.stdout.write("\r\033[K\033[?25h")  # clear line + show cursor
            sys.stdout.flush()
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    duration = time.monotonic() - t_start
    return duration, chunks


def main() -> int:
    if not os.environ.get("GROQ_API_KEY"):
        die("GROQ_API_KEY not set — copy .env.example to .env and paste your key")

    print(f"  {D}Press Enter to start recording…{N}", end="", flush=True)
    try:
        _ = input()
    except EOFError:
        # Piped input ended without a newline — treat as cancel.
        return 130

    duration, chunks = record_interactive()

    if not chunks:
        die("no audio captured — check that your mic is unmuted in pavucontrol / wpctl")

    audio = np.concatenate(chunks, axis=0)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(audio.tobytes())

    print(f"  {D}→ transcribing {duration:.2f}s of audio…{N}", flush=True)

    try:
        client = Groq()
        kwargs: dict = {
            "file": ("audio.wav", buf.getvalue(), "audio/wav"),
            "model": MODEL,
            "response_format": "text",
        }
        if PROMPT:
            kwargs["prompt"] = PROMPT
        result = client.audio.transcriptions.create(**kwargs)
        text = (result if isinstance(result, str) else getattr(result, "text", "")).strip()
    except Exception as e:
        die(f"transcription failed: {e}")

    if not text:
        print(f"  {Y}⚠ empty transcript{N} — try speaking more clearly or check mic levels")
        return 0

    print(f'  {G}✓{N} "{text}"')
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        # record_interactive's finally has already restored termios and the
        # cursor; we just need to print a clean message and exit.
        print(f"\n  {D}Cancelled.{N}", file=sys.stderr)
        sys.exit(130)
