"""One-shot record + transcribe, used by `scribe test`.

Press Enter to start recording, Enter again to stop. The transcript prints to
stdout — nothing touches the clipboard, ydotool, or the systemd service.
"""

from __future__ import annotations

import io
import os
import sys
import time
import wave

import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

SAMPLE_RATE = 16000
MODEL = os.environ.get("SCRIBE_MODEL", "whisper-large-v3-turbo").strip()
PROMPT = os.environ.get("SCRIBE_PROMPT", "").strip()

if sys.stdout.isatty():
    R = "\033[31m"
    G = "\033[32m"
    Y = "\033[33m"
    D = "\033[2m"
    N = "\033[0m"
else:
    R = G = Y = D = N = ""


def die(msg: str, code: int = 1) -> None:
    print(f"  {R}✗{N} {msg}", file=sys.stderr)
    sys.exit(code)


def main() -> int:
    if not os.environ.get("GROQ_API_KEY"):
        die("GROQ_API_KEY not set — copy .env.example to .env and paste your key")

    print(f"  {D}Press Enter to start recording…{N}", end="", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print()
        return 130

    chunks: list[np.ndarray] = []

    def callback(indata, frames, time_info, status) -> None:
        if status:
            print(f"  {Y}audio status: {status}{N}", file=sys.stderr)
        chunks.append(indata.copy())

    try:
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE, channels=1, dtype="int16", callback=callback,
        )
        stream.start()
    except Exception as e:
        die(f"failed to open mic: {e}")

    t_start = time.monotonic()
    print(f"  {G}● recording…{N} {D}(Enter to stop){N}", flush=True)
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        print()
    finally:
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    duration = time.monotonic() - t_start

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
    sys.exit(main())
