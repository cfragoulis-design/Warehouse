"""Voice take: Greek speech -> a draft take the worker confirms on screen.

Speech-to-text runs in the kiosk browser (Web Speech API, when the browser has
it) or on the server through an OpenAI-compatible ``/audio/transcriptions``
endpoint configured with ``KIOSK_STT_API_KEY`` (+ optional ``KIOSK_STT_BASE_URL``,
``KIOSK_STT_MODEL``). Nothing is configured by default, so nothing is billed.

The parser understands phrases such as «πήρα δύο κούτες γάντια», «έβαλα 3
πακέτα χαρτί», «δύο γάντια και ένα ρολό χαρτί». It never writes a movement:
the page shows the draft and the worker presses «Ναι» to run the normal take.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
import uuid
from decimal import Decimal

try:
    from app.kiosk_session import fold_search
except ImportError:  # pragma: no cover - flat module layout
    from kiosk_session import fold_search

STT_KEY_ENV = "KIOSK_STT_API_KEY"
STT_BASE_ENV = "KIOSK_STT_BASE_URL"
STT_MODEL_ENV = "KIOSK_STT_MODEL"
DEFAULT_STT_BASE = "https://api.openai.com/v1"
DEFAULT_STT_MODEL = "gpt-4o-mini-transcribe"
MAX_AUDIO_BYTES = 2_000_000

_NUMBERS = {
    "ενα": 1, "ενασ": 1, "μια": 1, "μιαν": 1, "ενοσ": 1,
    "δυο": 2, "δυοι": 2, "τρια": 3, "τρεισ": 3, "τεσσερα": 4, "τεσσερισ": 4,
    "πεντε": 5, "εξι": 6, "εφτα": 7, "επτα": 7, "οχτω": 8, "οκτω": 8,
    "εννια": 9, "εννεα": 9, "δεκα": 10, "εντεκα": 11, "δωδεκα": 12,
    "δεκατρια": 13, "δεκατεσσερα": 14, "δεκαπεντε": 15, "δεκαεξι": 16,
    "δεκαεφτα": 17, "δεκαοχτω": 18, "δεκαεννια": 19, "εικοσι": 20,
    "τριαντα": 30, "σαραντα": 40, "πενηντα": 50, "εξηντα": 60, "εβδομηντα": 70,
    "ογδοντα": 80, "ενενηντα": 90, "εκατο": 100, "διακοσια": 200,
    "μισο": Decimal("0.5"), "μιση": Decimal("0.5"),
}
_PACK_WORDS = {
    "κουτα", "κουτεσ", "κουτι", "κουτια", "πακετο", "πακετα", "συσκευασια", "συσκευασιεσ",
    "κιβωτιο", "κιβωτια", "κασονι", "κασονια", "δεμα", "δεματα",
}
_BOX_WORDS = {"κουτα", "κουτεσ", "κουτι", "κουτια"}
_BOX_UNITS = {"box", "βοξ", "κουτα", "κουτι", "κουτια", "κουτεσ", "κιβωτιο"}
# Glove sizes etc. are single letters in the catalog ("Γάντια Νιτριλίου Μαύρα M").
_ALIASES = {
    "μιντιουμ": ("m",), "μεντιουμ": ("m",), "μεσαια": ("m",), "μεσαιο": ("m",), "εμ": ("m",), "εμμ": ("m",),
    "λαρτζ": ("l",), "λαρζ": ("l",), "λαρτζι": ("l",), "ελ": ("l",),
    "σμολ": ("s",), "εσ": ("s",),
    "εξτραλαρτζ": ("xl",), "ιξελ": ("xl",), "χλ": ("xl",),
    "μαυρα": ("black",), "μαυρο": ("black",), "βακουμ": ("vacuum",), "βακιουμ": ("vacuum",),
}
_UNIT_WORDS = {"τεμαχιο", "τεμαχια", "τεμ", "κομματι", "κομματια", "τεμαχιοσ"}
_TAKE_VERBS = {"πηρα", "πηραμε", "παιρνω", "παιρνουμε", "πηρε", "θελω", "βγαζω", "εβγαλα"}
_ADD_VERBS = {"εβαλα", "βαζω", "εβαλαμε", "εφερα", "φερνω", "προσθεσε", "προσθετω"}
_FILLER = {"και", "απο", "τα", "τισ", "το", "τη", "την", "τον", "οι", "ο", "η", "με", "για", "ρε", "λοιπον", "ναι", "εγω"}
_SPLIT = re.compile(r"\s*(?:,|;|\.|\bκαι\b|\bκι\b)\s*")


def _tokens(text: str) -> list[str]:
    return [tok for tok in re.split(r"[^\w]+", fold_search(text), flags=re.UNICODE) if tok]


def _stem(token: str) -> str:
    if len(token) <= 4:
        return token
    return token[: max(4, len(token) - 2)]


def parse_number(token: str):
    if token.isdigit():
        return int(token)
    if re.fullmatch(r"\d+[.,]\d+", token):
        return Decimal(token.replace(",", "."))
    return _NUMBERS.get(token)


def parse_phrase(text: str) -> dict:
    """``{"mode", "parts": [{"qty", "packs": bool, "words": [...]}]}``."""
    tokens = _tokens(text)
    mode = "take"
    for tok in tokens:
        if tok in _ADD_VERBS:
            mode = "add"
            break
        if tok in _TAKE_VERBS:
            break
    folded = fold_search(text)
    chunks = [chunk for chunk in _SPLIT.split(folded) if chunk.strip()]
    parts = []
    for chunk in chunks:
        toks = [t for t in re.split(r"[^\w]+", chunk, flags=re.UNICODE) if t]
        qty = None
        packs = False
        pack_word = ""
        words: list[str] = []
        for index, tok in enumerate(toks):
            if tok in _TAKE_VERBS or tok in _ADD_VERBS:
                continue
            number = parse_number(tok)
            if number is not None and qty is None and not words:
                qty = number
                continue
            if tok in _PACK_WORDS and not words and index + 1 < len(toks):
                packs = True
                pack_word = tok
                continue
            if tok in _UNIT_WORDS and not words:
                continue
            if tok in _FILLER:
                continue
            words.append(tok)
        if not words:
            continue
        parts.append({"qty": qty if qty is not None else 1, "qty_said": qty is not None, "packs": packs, "pack_word": pack_word, "words": words})
    return {"mode": mode, "parts": parts}


def _alternatives(word: str) -> set[str]:
    alts = {word}
    for alias in _ALIASES.get(word, ()):
        alts.add(fold_search(alias))
    return alts


def _word_hits(word: str, name_tokens: list[str]) -> bool:
    for alt in _alternatives(word):
        if alt in name_tokens:
            return True
        stem = _stem(alt)
        if len(stem) >= 3 and any(len(tok) >= 3 and (_stem(tok).startswith(stem) or stem.startswith(_stem(tok))) for tok in name_tokens):
            return True
    return False


def score_item(words: list[str], item_name: str) -> float:
    """Share of spoken words found (by stem) in the item name, small bonus when
    the item's first word was said, small penalty for long names."""
    name_tokens = _tokens(item_name)
    if not name_tokens or not words:
        return 0.0
    hits = sum(1.0 for word in words if _word_hits(word, name_tokens))
    if hits == 0:
        return 0.0
    score = hits / len(words)
    if any(_word_hits(word, name_tokens[:1]) for word in words):
        score += 0.15
    score -= 0.01 * max(0, len(name_tokens) - len(words))
    return round(score, 4)


def match_items(words: list[str], items: list[dict], *, favorites: list[int] | None = None, limit: int = 4) -> list[dict]:
    favorite_rank = {int(cid): index for index, cid in enumerate(favorites or [])}
    scored = []
    for item in items:
        score = score_item(words, item["name"])
        if score <= 0.3:
            continue
        bonus = 0.05 * (1 - favorite_rank[int(item["id"])] / 10) if int(item["id"]) in favorite_rank else 0.0
        scored.append((score + bonus, item))
    scored.sort(key=lambda pair: (-pair[0], fold_search(pair[1]["name"])))
    return [dict(item, score=round(score, 3)) for score, item in scored[:limit]]


def _fmt(value) -> str:
    number = Decimal(str(value))
    return str(int(number)) if number == number.to_integral_value() else format(number.normalize(), "f")


def draft_from_text(text: str, items: list[dict], *, favorites: list[int] | None = None) -> dict:
    """Turn a transcript into confirmable lines. ``items``: id, name, unit, pack_size, on_hand."""
    parsed = parse_phrase(text)
    lines = []
    for part in parsed["parts"]:
        candidates = match_items(part["words"], items, favorites=favorites)
        line = {"heard": " ".join(part["words"]), "qty_said": _fmt(part["qty"]), "packs": part["packs"], "candidates": []}
        for cand in candidates:
            pack = Decimal(str(cand.get("pack_size") or 1)) or Decimal("1")
            unit_key = fold_search(cand.get("unit") or "")
            # «κούτες» of an item counted in boxes are the unit itself, not cartons.
            as_packs = part["packs"] and not (part["pack_word"] in _BOX_WORDS and unit_key in _BOX_UNITS)
            units = Decimal(str(part["qty"])) * (pack if as_packs else Decimal("1"))
            units = units.quantize(Decimal("1")) if units >= 1 else Decimal("1")
            unit = cand.get("unit") or "τεμ"
            said = f"{_fmt(part['qty'])} {'συσκευασίες' if part['qty'] != 1 else 'συσκευασία'}"
            line["candidates"].append({
                "id": int(cand["id"]),
                "name": cand["name"],
                "unit": unit,
                "qty": int(units),
                "qty_label": f"{int(units)} {unit}" + (f" ({said} των {_fmt(pack)})" if as_packs else ""),
                "score": cand["score"],
                "enough": Decimal(str(cand.get("on_hand") or 0)) >= units,
            })
        line["confident"] = bool(
            line["candidates"]
            and (len(line["candidates"]) == 1 or line["candidates"][0]["score"] - line["candidates"][1]["score"] >= 0.1)
        )
        lines.append(line)
    return {"ok": bool(lines), "text": text, "mode": parsed["mode"], "lines": lines}


# --------------------------------------------------------------------- speech to text


def stt_available() -> bool:
    return len(os.getenv(STT_KEY_ENV, "").strip()) >= 20


def stt_prompt(item_names: list[str]) -> str:
    """Vocabulary hint for the recognizer (item names only, no personal data)."""
    words = []
    seen = set()
    for name in item_names:
        for word in re.split(r"\s+", name):
            key = word.casefold()
            if len(word) >= 3 and key not in seen:
                seen.add(key)
                words.append(word)
    return ("Αποθήκη αναλωσίμων. Πήρα δύο κούτες γάντια. " + ", ".join(words))[:900]


def transcribe(audio: bytes, content_type: str, *, prompt: str = "", opener=None) -> str:
    """Send one short clip to the configured OpenAI-compatible endpoint."""
    key = os.getenv(STT_KEY_ENV, "").strip()
    if len(key) < 20:
        raise RuntimeError("speech-to-text is not configured")
    if not audio or len(audio) > MAX_AUDIO_BYTES:
        raise ValueError("audio clip is empty or too long")
    base = (os.getenv(STT_BASE_ENV, "").strip() or DEFAULT_STT_BASE).rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("KIOSK_STT_BASE_URL must be https")
    model = os.getenv(STT_MODEL_ENV, "").strip() or DEFAULT_STT_MODEL
    ext = "webm" if "webm" in content_type else ("ogg" if "ogg" in content_type else ("wav" if "wav" in content_type else "mp4"))
    boundary = uuid.uuid4().hex
    fields = {"model": model, "language": "el", "response_format": "json"}
    if prompt:
        fields["prompt"] = prompt
    body = bytearray()
    for name, value in fields.items():
        body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode("utf-8")
    body += (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"take.{ext}\"\r\n"
        f"Content-Type: {content_type or 'application/octet-stream'}\r\n\r\n"
    ).encode("utf-8")
    body += audio + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        f"{base}/audio/transcriptions",
        data=bytes(body),
        method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    open_url = opener or urllib.request.urlopen
    with open_url(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8") or "{}")
    return str(payload.get("text") or "").strip()
