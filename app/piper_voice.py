"""Local Greek Piper TTS for Telegram voice replies (no AI, no credits).

The kiosk take-page greeting uses Microsoft Edge ``el-GR-AthinaNeural``.
The kiosk-apothiki door reminder already uses Piper ``el_GR-rapunzelina-medium``.
There is no Piper voice literally named Αθηνά, so this module uses the same
Piper model already on that machine. Output is OGG/Opus for Telegram sendVoice.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "el_GR-rapunzelina-medium"
_UNITS = (
    "μηδέν", "ένα", "δύο", "τρία", "τέσσερα", "πέντε", "έξι", "επτά", "οκτώ", "εννέα",
    "δέκα", "έντεκα", "δώδεκα", "δεκατρία", "δεκατέσσερα", "δεκαπέντε", "δεκαέξι",
    "δεκαεπτά", "δεκαοκτώ", "δεκαεννέα",
)
_TENS = ("", "", "είκοσι", "τριάντα", "σαράντα", "πενήντα", "εξήντα", "εβδομήντα", "ογδόντα", "ενενήντα")
_HUNDREDS = (
    "", "εκατό", "διακόσια", "τριακόσια", "τετρακόσια", "πεντακόσια",
    "εξακόσια", "επτακόσια", "οκτακόσια", "εννιακόσια",
)

# «10,62 €», «€10.62», «8,78€»
_EURO_RE = re.compile(
    r"(?:€\s*)(\d+(?:[.,]\d{1,2})?)|(?<!\d)(\d+(?:[.,]\d{1,2})?)\s*€",
    re.UNICODE,
)
_KG_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:kg|κιλ[αάοί]?)\b", re.IGNORECASE)


def _join(parts: list[str]) -> str:
    return " ".join(p for p in parts if p)


def greek_int(value: int) -> str:
    """Cardinal number in Greek (0 .. 999_999)."""
    if value < 0:
        return "μείον " + greek_int(-value)
    if value < 20:
        return _UNITS[value]
    if value < 100:
        tens, rem = divmod(value, 10)
        return _join([_TENS[tens], _UNITS[rem] if rem else ""])
    if value < 200:
        rem = value - 100
        return _join(["εκατόν" if rem else "εκατό", greek_int(rem) if rem else ""])
    if value < 1000:
        hundreds, rem = divmod(value, 100)
        return _join([_HUNDREDS[hundreds], greek_int(rem) if rem else ""])
    if value < 1_000_000:
        thousands, rem = divmod(value, 1000)
        if thousands == 1:
            head = "χίλια"
        else:
            head = greek_int(thousands) + " χιλιάδες"
        return _join([head, greek_int(rem) if rem else ""])
    return str(value)


def greek_euros(amount: Decimal) -> str:
    money = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    euros = int(money)
    cents = int((money - Decimal(euros)) * 100)
    if euros == 0 and cents == 0:
        return "μηδέν ευρώ"
    parts: list[str] = []
    if euros:
        if euros == 1:
            parts.append("ένα ευρώ")
        else:
            parts.append(f"{greek_int(euros)} ευρώ")
    if cents:
        if cents == 1:
            parts.append("ένα λεπτό")
        else:
            parts.append(f"{greek_int(cents)} λεπτά")
        if euros:
            return f"{parts[0]} και {parts[1]}"
    return parts[0] if parts else "μηδέν ευρώ"


def speech_text(text: str) -> str:
    """Turn a written price answer into something Piper can read naturally."""

    def euro_sub(match: re.Match[str]) -> str:
        raw = match.group(1) or match.group(2) or "0"
        return greek_euros(Decimal(raw.replace(",", ".")))

    spoken = _EURO_RE.sub(euro_sub, text or "")

    def kg_sub(match: re.Match[str]) -> str:
        raw = match.group(1).replace(",", ".")
        number = Decimal(raw)
        if number == number.to_integral_value():
            n = int(number)
            word = "κιλό" if n == 1 else "κιλά"
            return f"{greek_int(n)} {word}"
        # 1,3 kg
        whole = int(number)
        frac = str(number).split(".")[-1]
        return f"{greek_int(whole)} κόμμα {greek_int(int(frac))} κιλά"

    spoken = _KG_RE.sub(kg_sub, spoken)
    spoken = spoken.replace("€", " ευρώ ")
    spoken = re.sub(r"[•*]", "", spoken)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    return spoken


def _search_roots() -> list[Path]:
    env = os.getenv("PIPER_HOME", "").strip()
    roots = []
    if env:
        roots.append(Path(env))
    roots.extend(
        [
            Path("/opt/piper"),
            Path(__file__).resolve().parent.parent / "third_party" / "piper",
            Path("/usr/local/piper"),
        ]
    )
    return roots


def piper_binary() -> str | None:
    found = shutil.which("piper")
    if found:
        return found
    for root in _search_roots():
        candidate = root / "piper"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def model_path(name: str = DEFAULT_MODEL) -> Path | None:
    env = os.getenv("PIPER_MODEL", "").strip()
    candidates = [Path(env)] if env else []
    filename = name if name.endswith(".onnx") else f"{name}.onnx"
    for root in _search_roots():
        candidates.append(root / "models" / filename)
        candidates.append(root / filename)
    for path in candidates:
        if path and path.is_file():
            return path
    return None


def available() -> bool:
    return bool(piper_binary() and model_path() and shutil.which("ffmpeg"))


def synthesize_ogg(text: str, *, timeout: int = 30) -> bytes | None:
    """Return OGG/Opus bytes, or None if Piper/ffmpeg cannot run."""
    spoken = speech_text(text)
    if not spoken:
        return None
    binary = piper_binary()
    model = model_path()
    ffmpeg = shutil.which("ffmpeg")
    if not binary or not model or not ffmpeg:
        logger.warning("piper voice skipped: piper=%s model=%s ffmpeg=%s", bool(binary), bool(model), bool(ffmpeg))
        return None
    with tempfile.TemporaryDirectory(prefix="piper-") as tmp:
        wav = Path(tmp) / "speech.wav"
        ogg = Path(tmp) / "speech.ogg"
        try:
            subprocess.run(
                [binary, "--model", str(model), "--output_file", str(wav)],
                input=spoken.encode("utf-8"),
                check=True,
                timeout=timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            subprocess.run(
                [ffmpeg, "-y", "-i", str(wav), "-c:a", "libopus", "-b:a", "24k", "-vbr", "on", str(ogg)],
                check=True,
                timeout=timeout,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError) as exc:
            logger.warning("piper/ffmpeg failed: %s", exc)
            return None
        data = ogg.read_bytes() if ogg.is_file() else b""
        if len(data) < 64:
            logger.warning("piper produced no audio")
            return None
        return data
