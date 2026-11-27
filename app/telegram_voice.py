"""Telegram Kiosk voice replies: Edge AthinaNeural first, bundled Piper fallback.

The kiosk take-page greeting already uses Microsoft Edge ``el-GR-AthinaNeural``
via ``edge-tts`` (free, no credits). Telegram sendVoice needs OGG/Opus, so this
module asks Edge for MP3 and converts it with ffmpeg. A short timeout applies;
if Edge fails, times out, or returns nothing, the same Greek Piper model as the
kiosk-apothiki reminder is used. No AI and no credits.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

try:
    from app import piper_voice
except ImportError:  # pragma: no cover - flat module layout
    import piper_voice

logger = logging.getLogger(__name__)

VOICE = "el-GR-AthinaNeural"
EDGE_TIMEOUT_S = 8.0


def _mp3_to_ogg(mp3: bytes, *, timeout: float = 15) -> bytes | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not mp3:
        return None
    with tempfile.TemporaryDirectory(prefix="edge-ogg-") as tmp:
        src = Path(tmp) / "speech.mp3"
        dst = Path(tmp) / "speech.ogg"
        src.write_bytes(mp3)
        try:
            subprocess.run(
                [ffmpeg, "-y", "-i", str(src), "-c:a", "libopus", "-b:a", "24k", "-vbr", "on", str(dst)],
                check=True,
                timeout=timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("edge-tts ffmpeg failed: %s", type(exc).__name__)
            return None
        data = dst.read_bytes() if dst.is_file() else b""
        if len(data) < 64 or data[:4] != b"OggS":
            return None
        return data


def _edge_mp3(text: str, *, timeout: float) -> bytes | None:
    """Fetch MP3 from Microsoft Edge TTS. Never raises; returns None on failure."""
    try:
        import edge_tts
    except ImportError:
        logger.warning("edge-tts is not installed")
        return None

    result: dict = {}

    async def _run() -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            path = Path(handle.name)
        try:
            await edge_tts.Communicate(text, VOICE).save(str(path))
            return path.read_bytes() if path.is_file() else b""
        finally:
            path.unlink(missing_ok=True)

    def _thread() -> None:
        try:
            result["audio"] = asyncio.run(asyncio.wait_for(_run(), timeout))
        except BaseException as exc:  # noqa: BLE001 - converted to a fallback
            result["error"] = exc

    worker = threading.Thread(target=_thread, daemon=True)
    worker.start()
    worker.join(timeout + 2)
    audio = result.get("audio") or b""
    if len(audio) < 64:
        err = result.get("error")
        logger.warning("edge-tts failed type=%s", type(err).__name__ if err is not None else "empty")
        return None
    return audio


def synthesize_edge_ogg(text: str, *, timeout: float = EDGE_TIMEOUT_S) -> bytes | None:
    """Speak with AthinaNeural and return OGG/Opus, or None if Edge/ffmpeg cannot."""
    spoken = piper_voice.speech_text(text)
    if not spoken:
        return None
    mp3 = _edge_mp3(spoken, timeout=timeout)
    if not mp3:
        return None
    return _mp3_to_ogg(mp3)


def synthesize_reply_ogg(text: str) -> bytes | None:
    """Prefer Edge AthinaNeural; fall back to bundled Piper on failure or timeout."""
    audio = synthesize_edge_ogg(text)
    if audio:
        return audio
    logger.warning("edge-tts unavailable or timed out; falling back to Piper")
    return piper_voice.synthesize_ogg(text)


def probe_edge_tts() -> str:
    """Tiny reachability check for Edge TTS. Returns 'ok' or a short reason."""
    audio = synthesize_edge_ogg("ένα", timeout=6)
    if audio:
        return "ok"
    return "failed"


def schedule_live_probe() -> None:
    """In the Railway production container only, log whether Edge TTS is reachable.

    Tests and local shells are skipped. The probe is a daemon thread so /ready
    is not delayed. It does not send anything to Telegram.
    """
    import sys

    if "pytest" in sys.modules:
        return
    if (os.getenv("RAILWAY_ENVIRONMENT_ID") or "").strip() != "99388a85-6dd8-4658-9841-8c41232aef49":
        return

    def _run() -> None:
        try:
            status = probe_edge_tts()
            logger.info(
                "telegram voice edge-tts probe: %s piper_available=%s",
                status,
                piper_voice.available(),
            )
        except Exception:
            logger.warning("telegram voice edge-tts probe crashed", exc_info=False)

    threading.Thread(target=_run, daemon=True, name="telegram-voice-probe").start()
