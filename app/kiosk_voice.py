"""Spoken kiosk phrases with the native Greek neural voice Christos chose
(Microsoft Edge read-aloud voice ``el-GR-AthinaNeural`` via the ``edge-tts``
package). Every clip is generated once and cached in ``kiosk_voice_clips``
(greetings are pre-generated when the switch is turned on); if synthesis is
unavailable the kiosk simply stays silent. Only server-built phrases are spoken (greeting with
a worker's name, a take confirmation); the page can never send free text.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from app.models import KioskVoiceClip, KioskWorkerVoice, Worker
except ImportError:  # pragma: no cover - flat module layout
    from models import KioskVoiceClip, KioskWorkerVoice, Worker

logger = logging.getLogger(__name__)
ATHENS = ZoneInfo("Europe/Athens")
VOICE = "el-GR-AthinaNeural"
SYNTH_TIMEOUT_S = 12.0
MAX_TEXT = 200

# Rough Latin -> Greek spelling so the Greek voice reads Latin names naturally.
_TRANSLIT = (
    ("th", "θ"), ("ch", "χ"), ("ph", "φ"), ("ps", "ψ"), ("ou", "ου"), ("oo", "ου"),
    ("sh", "σ"), ("ts", "τσ"), ("nd", "ντ"), ("mb", "μπ"), ("ng", "γκ"), ("ck", "κ"), ("ll", "λ"), ("mm", "μ"), ("nn", "ν"), ("ss", "σ"),
    ("y", "ι"), ("j", "τζ"), ("w", "β"), ("c", "κ"), ("q", "κ"),
    ("a", "α"), ("b", "μπ"), ("d", "ντ"), ("e", "ε"), ("f", "φ"), ("g", "γκ"),
    ("h", ""), ("i", "ι"), ("k", "κ"), ("l", "λ"), ("m", "μ"), ("n", "ν"), ("o", "ο"),
    ("p", "π"), ("r", "ρ"), ("s", "σ"), ("t", "τ"), ("u", "ου"), ("v", "β"), ("x", "ξ"), ("z", "ζ"),
)


def latin_to_greek(word: str) -> str:
    text = (word or "").lower()
    if not re.search(r"[a-z]", text):
        return word
    for source, target in _TRANSLIT:
        text = text.replace(source, target)
    text = re.sub(r"σ\b", "ς", text)
    return text[:1].upper() + text[1:]


_GREEK_VOWEL_GROUP = re.compile(r"(αι|ει|οι|ου|αυ|ευ|υι|[αεηιουωάέήίόύώϊϋΐΰ])")


def vocative(name: str) -> str:
    """Greek vocative for a greeting: Βασιλάκης -> Βασιλάκη, Νίκος -> Νίκο,
    Αλέξανδρος -> Αλέξανδρε, Λουκάς -> Λουκά. Other endings stay as they are."""
    parts = name.split("-")
    out = []
    for part in parts:
        low = part.lower()
        if low.endswith(("ης", "ής", "ας", "άς")):
            part = part[:-1]
        elif low.endswith(("ος", "ός")):
            syllables = len(_GREEK_VOWEL_GROUP.findall(re.sub(r"γι(?=[αεηοωυάέήόώύ])", "γ", low)))
            part = part[:-2] + (part[-2] if syllables <= 2 else "ε")
        out.append(part)
    return "-".join(out)


def default_spoken_name(display_name: str) -> str:
    """First name only (\"Volodymyr K.\" -> \"Βολοντιμιρ\", \"Μαρία-Μάρθα Λ.\" -> \"Μαρία-Μάρθα\")."""
    parts = [part for part in (display_name or "").split() if not re.fullmatch(r"\w\.?", part)]
    first = parts[0] if parts else (display_name or "").strip()
    return latin_to_greek(first) if re.search(r"[A-Za-z]", first) else vocative(first)


def spoken_name(db: Session, worker: Worker) -> str:
    row = db.get(KioskWorkerVoice, worker.id)
    if row is not None and (row.spoken_name or "").strip():
        return row.spoken_name.strip()
    return default_spoken_name(worker.display_name)


def greets(db: Session, worker_id: int) -> bool:
    row = db.get(KioskWorkerVoice, worker_id)
    return True if row is None else bool(row.greet)


def set_worker_voice(db: Session, worker_id: int, name: str, greet: bool) -> None:
    clean = re.sub(r"[^\w\s\-'.]", "", (name or "").strip(), flags=re.UNICODE)[:80]
    row = db.get(KioskWorkerVoice, worker_id)
    if row is None:
        db.add(KioskWorkerVoice(worker_id=worker_id, spoken_name=clean, greet=bool(greet), updated_at=datetime.now(timezone.utc)))
    else:
        row.spoken_name = clean
        row.greet = bool(greet)
        row.updated_at = datetime.now(timezone.utc)
    db.commit()


def greeting_text(name: str, *, now: datetime | None = None) -> str:
    local = (now or datetime.now(timezone.utc)).astimezone(ATHENS)
    hello = "Καλημέρα" if 4 <= local.hour < 13 else "Καλησπέρα"
    name = (name or "").strip()
    return f"{hello}, {name}!" if name else f"{hello}!"


def clip_key(text: str, voice: str = VOICE) -> str:
    return hashlib.sha256(f"{voice}\n{text}".encode("utf-8")).hexdigest()


def _edge_tts_synthesize(text: str, voice: str) -> bytes:
    import edge_tts  # optional dependency, imported only when a clip is missing

    async def _run() -> bytes:
        chunks: list[bytes] = []
        communicate = edge_tts.Communicate(text, voice)
        async for chunk in communicate.stream():
            if chunk.get("type") == "audio":
                chunks.append(chunk["data"])
        return b"".join(chunks)

    result: dict = {}

    def _thread() -> None:
        try:
            result["audio"] = asyncio.run(asyncio.wait_for(_run(), SYNTH_TIMEOUT_S))
        except BaseException as exc:  # noqa: BLE001 - reported below
            result["error"] = exc

    worker = threading.Thread(target=_thread, daemon=True)
    worker.start()
    worker.join(SYNTH_TIMEOUT_S + 2)
    if "audio" not in result:
        raise RuntimeError(f"speech synthesis failed: {type(result.get('error')).__name__}")
    return result["audio"]


_synth = _edge_tts_synthesize


def set_synthesizer(func) -> None:
    """Tests replace the network synthesizer."""
    global _synth
    _synth = func or _edge_tts_synthesize


def get_clip(db: Session, text: str, *, generate: bool = True, voice: str = VOICE, attempts: int = 1) -> bytes | None:
    text = (text or "").strip()[:MAX_TEXT]
    if not text:
        return None
    key = clip_key(text, voice)
    row = db.get(KioskVoiceClip, key)
    if row is not None:
        return bytes(row.audio)
    if not generate:
        return None
    audio = b""
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(1.5 * attempt)  # the free read-aloud endpoint sometimes answers empty
        try:
            audio = _synth(text, voice)
        except Exception as exc:
            logger.warning("voice clip not generated (try %s): %s", attempt + 1, exc)
            continue
        if audio:
            break
    if not audio:
        return None
    db.add(KioskVoiceClip(clip_key=key, voice=voice, spoken_text=text, audio=audio, content_type="audio/mpeg", created_at=datetime.now(timezone.utc)))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return audio


def prewarm_greetings(db: Session, *, now: datetime | None = None, attempts: int = 4) -> int:
    """Generate both greetings for every active worker with a PIN. Returns clips ready."""
    ready = 0
    workers = db.query(Worker).filter(Worker.is_active.is_(True)).filter(Worker.pin_hash.isnot(None)).all()
    for worker in workers:
        if not greets(db, worker.id):
            continue
        name = spoken_name(db, worker)
        for hour in (9, 18):
            moment = (now or datetime.now(timezone.utc)).astimezone(ATHENS).replace(hour=hour)
            if get_clip(db, greeting_text(name, now=moment), attempts=attempts) is not None:
                ready += 1
    return ready


def confirm_text(name: str, qty_text: str, mode: str = "take") -> str:
    verb = "Έβαλες" if mode == "add" else "Πήρες"
    return f"{verb} {qty_text} {name}; Πάτα το κουμπί για να καταχωρηθεί."[:MAX_TEXT]
