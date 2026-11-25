"""Telegram questions → answers from the warehouse data (read-only, no LLM).

«πόσα γάντια έχουμε;», «τι καταναλώσαμε τον Νοέμβριο;», «τι πήρε ο Βασιλάκης αυτή
τη βδομάδα;», «τι τελειώνει;», «τι να παραγγείλω;», «πότε τελειώνει το χαρτί μεγάλο;»

* Parsing is deterministic: every word is reduced to a «skeleton» (no accents, no
  case, Greek → Latin, Greeklish spellings folded together: θ/th/8, χ/ch/h/x, ξ/ks,
  ου/ou/u, η/ι/υ/ει/οι/i, αι/ε, ω/ο, μπ/b, ντ/d, β/v/b …). Keywords are written in
  Greek below and reduced the same way, so Greek, Greeklish and mixed text match.
* Products are matched by word stems / typos (difflib) and sizes (S, M, L, XL,
  «μίντιουμ», 750, 1250 …); workers by their display name.
* Read-only: it only SELECTs. It never changes stock and never sends an order.
* Price questions («πόσο αγοράζουμε το Χ», «τιμή Χ», «πόσο κοστίζει/κάνει/πληρώνουμε
  το Χ») take priority over the stock meaning of «πόσο».
* A leading «πες μου» or «Αθηνά» (accents / Greeklish folded the same way) is
  stripped before parsing; the text answer is still sent, and a Piper OGG/Opus
  voice message follows (same Greek Piper model as the kiosk-apothiki reminder).
  Without that trigger, behaviour is text-only.
* Webhook ``POST /telegram/ask/webhook`` (feature ``nextgen_telegram_ask``):
  secret header ``X-Telegram-Bot-Api-Secret-Token`` = ``TELEGRAM_WEBHOOK_SECRET``;
  only chats listed in ``TELEGRAM_CONSUMABLES_CHAT_ID`` (else ``TELEGRAM_CHAT_ID``; comma-separated) get an answer; every
  update_id is answered once; 20 questions / 10 minutes per chat. The text answer
  goes back inside the webhook response (``sendMessage``). Voice, when requested,
  is a single extra ``sendVoice`` call — never to a chat that is not already allowed.
* Optional AI fallback for questions it does not understand: only when the
  ``nextgen_telegram_ask_llm`` switch is on AND ``TELEGRAM_ASK_LLM_API_KEY`` is set
  (neither is, by default). It never sees more than the question text.
"""

from __future__ import annotations

import difflib
import hmac
import logging
import os
import re
import unicodedata
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

try:
    from app import kiosk_nextgen as ng
    from app import piper_voice
    from app.db import get_db
    from app.models import Consumable, ConsumableMovement, Supplier, TelegramAskLog, Worker
except ImportError:  # pragma: no cover - flat module layout
    import kiosk_nextgen as ng
    import piper_voice
    from db import get_db
    from models import Consumable, ConsumableMovement, Supplier, TelegramAskLog, Worker

logger = logging.getLogger(__name__)
router = APIRouter()
ATHENS = ZoneInfo("Europe/Athens")
UNDO_NOTE = "Αναίρεση"
RATE_LIMIT = 20
RATE_WINDOW = timedelta(minutes=10)
MAX_LINES = 15
WEBHOOK_PATH = "/telegram/ask/webhook"

# --------------------------------------------------------------------- skeleton

_GREEK = {
    "α": "a", "β": "v", "γ": "g", "δ": "d", "ε": "e", "ζ": "z", "η": "i", "θ": "8", "ι": "i",
    "κ": "k", "λ": "l", "μ": "m", "ν": "n", "ξ": "x", "ο": "o", "π": "p", "ρ": "r", "σ": "s",
    "ς": "s", "τ": "t", "υ": "y", "φ": "f", "χ": "x", "ψ": "ps", "ω": "o",
}
_VOWELS = set("aeiouy")


def _strip(text: str) -> str:
    text = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in text if unicodedata.category(ch) != "Mn").lower()


def _latin(word: str) -> str:
    out = []
    for index, ch in enumerate(word):
        if ch == "h":
            prev = word[index - 1] if index else ""
            nxt = word[index + 1] if index + 1 < len(word) else ""
            if prev in ("c", "k"):
                out.append("h")  # handled by ch/kh below
            elif prev == "t":
                out.append("h")  # th
            elif prev == "p":
                out.append("i")  # «phre» = πήρε (φ is written f)
            elif (not prev or prev in _VOWELS) and nxt in _VOWELS:
                out.append("x")  # «harti», «ehoume»
            else:
                out.append("i")  # η written as h
        else:
            out.append(_GREEK.get(ch, ch))
    return "".join(out)


_RULES = (
    ("ch", "x"), ("kh", "x"), ("th", "8"), ("ks", "x"), ("cs", "x"),
    ("ou", "u"), ("oy", "u"), ("ay", "af"), ("ey", "ef"),
    ("mp", "b"), ("nt", "d"), ("gk", "g"), ("gg", "g"), ("nk", "g"),
    ("ai", "e"), ("ei", "i"), ("oi", "i"), ("yi", "i"),
    ("y", "i"), ("w", "o"), ("c", "k"), ("q", "k"), ("v", "f"), ("b", "f"),
)


def skeleton(word: str) -> str:
    """Spelling-independent key of one word (Greek, Greeklish or English)."""
    base = _strip(word)
    if not base:
        return ""
    if base.isdigit():
        return base
    out = _latin(base)
    for old, new in _RULES:
        out = out.replace(old, new)
    out = re.sub(r"(.)\1+", r"\1", out)
    if len(out) > 3 and out.endswith("s"):
        out = out[:-1]
    return out


_TOKEN = re.compile(r"\d+(?:[.,]\d+)?|[^\W\d_]+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return [skeleton(tok) for tok in _TOKEN.findall(text or "") if skeleton(tok)]


def _stems(*words: str) -> tuple[str, ...]:
    return tuple(sorted({skeleton(w) for w in words}, key=len, reverse=True))


STOCK = _stems("πόσα", "πόσο", "πόσες", "πόσοι", "πόσα", "απόθεμα", "αποθέματος", "στοκ", "stock", "υπάρχουν", "υπάρχει",
               "έχουμε", "έχομε", "έμειναν", "έμεινε", "μένουν", "μένει", "διαθέσιμα")
# «πόσο» alone remains STOCK; these words (or «πόσο» + a verb below) mean purchase price.
PRICE = _stems("τιμή", "τιμής", "τιμές", "price", "κόστος", "κοστίζει", "κοστίζουν", "κοστίζ", "πληρώνουμε", "πληρώνετε",
               "πληρώνω", "πληρωμή")
PRICE_VERBS = _stems("κοστίζει", "κοστίζουν", "κάνει", "κάνουν", "αγοράζουμε", "αγοράζετε", "αγοράζω",
                     "πληρώνουμε", "πληρώνετε", "πληρώνω")
CONSUME = _stems("καταναλώσαμε", "κατανάλωση", "καταναλώθηκ", "ξοδέψαμε", "ξόδεψαν", "φύγανε", "φύγαν", "βγήκαν", "βγήκε",
                 "πήραμε", "χρησιμοποιήσαμε", "χρησιμοποιήθηκ", "κινήσεις", "πάρθηκαν", "πάρθηκε", "πήγαν")
TOOK = _stems("πήρε", "πήραν", "έβγαλε", "έβγαλαν", "πάρει")
RUNOUT = _stems("τελειώνει", "τελειώνουν", "τελειώνει", "τελειώσει", "τελείωσε", "τελειώνουμε", "λείπει", "λείπουν", "λιγοστεύ",
                "εξαντλ", "σώνεται", "σώνονται", "τελειωμ")
ORDER = _stems("παραγγείλω", "παραγγείλουμε", "παραγγελία", "παραγγελίες", "παραγγελ", "παραγγ", "αγοράσω", "αγοράσουμε", "order")
HELP = _stems("βοήθεια", "help", "εντολές", "start", "οδηγίες")
GREET = _stems("γεια", "γειά", "καλημέρα", "καλησπέρα", "χαίρετε", "hello", "hi", "ela", "έλα", "ευχαριστώ", "thanks")
WHEN = _stems("πότε", "σε πόσες")
FEW_PHRASE = _stems("λίγα", "λίγο", "χαμηλά", "χαμηλό")
STOP = set(_stems(
    "τι", "τί", "ο", "η", "οι", "το", "τα", "του", "της", "των", "τον", "την", "τη", "στο", "στη", "στην", "στον", "στα", "στις",
    "στους", "σε", "και", "με", "μας", "μου", "από", "για", "να", "θα", "αυτή", "αυτήν", "αυτόν", "αυτό", "αυτά", "αυτές",
    "έχει", "είναι", "ποια", "ποιο", "ποιος", "ποιες", "όλα", "όλες", "μέχρι", "έως", "πολύ", "κάτι", "πες", "πείτε", "δείξε",
    "θέλω", "θέλουμε", "μπορείς", "ξέρεις", "πρέπει", "ακόμα", "ακόμη", "τώρα", "κουτιά", "κούτες", "κουτες", "τεμάχια",
    "πακέτα", "συνολικά", "σύνολο", "please", "pls", "bot", "εμείς", "έχω", "είδη", "είδος", "κομμάτια", "πόσα",
    "the", "how", "many", "much", "what", "σου", "σας",
))
MONTHS = {
    1: _stems("ιανουάριο", "ιανουαρίου", "ιανουάριος", "january"),
    2: _stems("φεβρουάριο", "φεβρουαρίου", "φεβρουάριος", "february"),
    3: _stems("μάρτιο", "μαρτίου", "μάρτιος", "march"),
    4: _stems("απρίλιο", "απριλίου", "απρίλιος", "april"),
    5: _stems("μάιο", "μαΐου", "μάιος", "μάη", "may"),
    6: _stems("ιούνιο", "ιουνίου", "ιούνιος", "june"),
    7: _stems("ιούλιο", "ιουλίου", "ιούλιος", "july"),
    8: _stems("αύγουστο", "αυγούστου", "αύγουστος", "august"),
    9: _stems("σεπτέμβριο", "σεπτεμβρίου", "σεπτέμβριος", "september"),
    10: _stems("οκτώβριο", "οκτωβρίου", "οκτώβριος", "october"),
    11: _stems("νοέμβριο", "νοεμβρίου", "νοέμβριος", "november"),
    12: _stems("δεκέμβριο", "δεκεμβρίου", "δεκέμβριος", "december"),
}
MONTH_NAMES = ("", "Ιανουάριος", "Φεβρουάριος", "Μάρτιος", "Απρίλιος", "Μάιος", "Ιούνιος", "Ιούλιος", "Αύγουστος",
               "Σεπτέμβριος", "Οκτώβριος", "Νοέμβριος", "Δεκέμβριος")
P_TODAY = _stems("σήμερα", "simera", "today")
P_YESTERDAY = _stems("χθες", "εχθές", "χτες", "yesterday")
P_BEFORE_YESTERDAY = _stems("προχθές", "προχτές")
P_WEEK = _stems("εβδομάδα", "βδομάδα", "εβδομάδας", "βδομάδας", "week")
P_MONTH = _stems("μήνα", "μήνας", "μήνες", "μηνός", "month")
P_PREVIOUS = _stems("προηγούμενη", "προηγούμενο", "προηγούμενος", "περασμένη", "περασμένο", "περασμένος", "last", "previous")
P_YEAR = _stems("φέτος", "χρονιά", "year")
P_LAST_N = _stems("τελευταίες", "τελευταίους", "τελευταίο", "τελευταία")
P_DAYS = _stems("μέρες", "ημέρες", "μέρα", "ημέρα", "days")
P_THIS = _stems("αυτή", "αυτόν", "αυτό", "τρέχουσα", "τρέχοντα", "this")
SYNONYMS = {skeleton(a): skeleton(b) for a, b in (
    ("gloves", "γάντια"), ("glove", "γάντια"), ("paper", "χαρτί"), ("bags", "σακούλα"), ("bag", "σακούλα"),
    ("βακούμ", "vacuum"), ("βάκουμ", "vacuum"), ("σακούλες", "σακούλα"), ("ταψάκια", "σκαφάκια"), ("σκαφάκι", "σκαφάκια"),
    ("μίντιουμ", "m"), ("μέντιουμ", "m"), ("μεσαία", "m"), ("μεσαίο", "m"), ("λαρτζ", "l"), ("λάρτζ", "l"), ("large", "l"),
    ("medium", "m"), ("small", "s"), ("σμολ", "s"), ("μικρά", "s"), ("ξλ", "xl"), ("εξλ", "xl"), ("extra", "xl"),
)}
SIZES = {"s", "m", "l", "xl", "xxl"}


def _has(token: str, stems: tuple[str, ...], *, exact: bool = False) -> bool:
    for stem in stems:
        if token == stem:
            return True
        if not exact and len(stem) >= 4 and token.startswith(stem):
            return True
    return False


def _word_match(query: str, target: str) -> bool:
    if query == target:
        return True
    if query.isdigit() or target.isdigit() or query in SIZES or target in SIZES:
        return False
    if len(query) >= 3 and len(target) >= 3:
        shorter = min(len(query), len(target))
        common = len(os.path.commonprefix([query, target]))
        if common == shorter or common >= max(4, shorter - 2):
            return True
    if len(query) >= 5 and len(target) >= 5:
        return difflib.SequenceMatcher(None, query, target).ratio() >= 0.8
    return False



_VOICE_PREFIX = re.compile(
    r"^\s*(?:πες\s+μου|πεσμου|pesmu|pes\s+mo[uy]?|pesmou|αθην[άα]|athina|athena)\s*[,:.\-–;]?\s*",
    re.IGNORECASE | re.UNICODE,
)


def strip_voice_trigger(text: str) -> tuple[str, bool]:
    """If the question starts with «πες μου» or «Αθηνά», strip it and ask for a voice reply."""
    raw = text or ""
    folded = re.sub(r"\s+", " ", _strip(raw)).strip()
    if not re.match(r"^(πες μου|πεσμου|pesmu|pes mou|pes mu|pesmou|αθηνα|athina|athena)(\s|$|[,:.\-;])", folded):
        if folded not in {"πες μου", "πεσμου", "pesmu", "pes mou", "pes mu", "pesmou", "αθηνα", "athina", "athena"}:
            return raw, False
    stripped = _VOICE_PREFIX.sub("", raw, count=1)
    if stripped == raw:
        return raw, False
    return stripped, True


def _is_price_question(toks: list[str]) -> bool:
    """True when the question is about purchase price, including «πόσο κοστίζει/κάνει/…»."""
    if any(_has(tok, PRICE) or tok in PRICE for tok in toks):
        return True
    has_poso = any(tok in _stems("πόσο", "poso") or _has(tok, _stems("πόσο"), exact=True) for tok in toks)
    has_verb = any(_has(tok, PRICE_VERBS) or tok in PRICE_VERBS for tok in toks)
    return has_poso and has_verb


# --------------------------------------------------------------------- parsing


@dataclass
class Period:
    start: date
    end: date  # exclusive
    label: str

    def utc_bounds(self) -> tuple[datetime, datetime]:
        def _utc(day: date) -> datetime:
            return datetime.combine(day, time.min, tzinfo=ATHENS).astimezone(timezone.utc)
        return _utc(self.start), _utc(self.end)


@dataclass
class Parsed:
    intent: str
    words: list[str] = field(default_factory=list)  # product words (skeletons)
    period: Period | None = None
    worker_ids: list[int] = field(default_factory=list)
    worker_label: str = ""


def _fmt_day(day: date) -> str:
    return day.strftime("%d/%m")


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _month_period(year: int, month: int, today: date) -> Period:
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1]) + timedelta(days=1)
    label = f"{MONTH_NAMES[month]} {year}"
    if start <= today < end:
        end = today + timedelta(days=1)
        label += f" (έως {_fmt_day(today)})"
    return Period(start, end, label)


def parse_period(toks: list[str], today: date) -> tuple[Period | None, set[int]]:
    """Return the period and the token indexes it used."""
    used: set[int] = set()
    previous = any(_has(t, P_PREVIOUS) for t in toks)
    for i, tok in enumerate(toks):
        if _has(tok, P_BEFORE_YESTERDAY, exact=True):
            day = today - timedelta(days=2)
            return Period(day, day + timedelta(days=1), f"προχθές ({_fmt_day(day)})"), {i}
        if _has(tok, P_YESTERDAY, exact=True):
            day = today - timedelta(days=1)
            return Period(day, day + timedelta(days=1), f"χθες ({_fmt_day(day)})"), {i}
        if _has(tok, P_TODAY, exact=True):
            return Period(today, today + timedelta(days=1), f"σήμερα ({_fmt_day(today)})"), {i}
    for i, tok in enumerate(toks):
        if tok.isdigit() and i + 1 < len(toks) and _has(toks[i + 1], P_DAYS):
            n = max(1, min(366, int(tok)))
            used = {i, i + 1} | {j for j, t in enumerate(toks) if _has(t, P_LAST_N)}
            start = today - timedelta(days=n - 1)
            return Period(start, today + timedelta(days=1), f"τελευταίες {n} μέρες ({_fmt_day(start)}–{_fmt_day(today)})"), used
    marks = {j for j, t in enumerate(toks) if _has(t, P_PREVIOUS) or _has(t, P_THIS, exact=True)}
    for i, tok in enumerate(toks):
        if _has(tok, P_WEEK):
            start = _week_start(today)
            if previous:
                start -= timedelta(days=7)
                return Period(start, start + timedelta(days=7), f"προηγούμενη εβδομάδα ({_fmt_day(start)}–{_fmt_day(start + timedelta(days=6))})"), {i} | marks
            return Period(start, today + timedelta(days=1), f"αυτή την εβδομάδα ({_fmt_day(start)}–{_fmt_day(today)})"), {i} | marks
        if _has(tok, P_MONTH, exact=True) or (len(tok) >= 4 and _has(tok, P_MONTH)):
            if previous:
                last = today.replace(day=1) - timedelta(days=1)
                return _month_period(last.year, last.month, today), {i} | marks
            return _month_period(today.year, today.month, today), {i} | marks
        if _has(tok, P_YEAR, exact=True):
            start = date(today.year, 1, 1)
            return Period(start, today + timedelta(days=1), f"φέτος ({_fmt_day(start)}–{_fmt_day(today)})"), {i} | marks
    for i, tok in enumerate(toks):
        for month, stems in MONTHS.items():
            if tok in stems or (len(tok) >= 5 and any(_word_match(tok, s) for s in stems if len(s) >= 5)):
                year = today.year if month <= today.month else today.year - 1
                for j, other in enumerate(toks):
                    if other.isdigit() and len(other) == 4 and 2000 <= int(other) <= today.year:
                        year = int(other)
                        used.add(j)
                return _month_period(year, month, today), {i} | used | marks
    return None, set()


def _worker_index(db: Session) -> list[tuple[Worker, list[str]]]:
    out = []
    for worker in db.query(Worker).order_by(Worker.is_active.desc(), Worker.id).all():
        words = [w for w in tokens(worker.display_name or "") if len(w) >= 3]
        if words:
            out.append((worker, words))
    return out


def parse(text: str, db: Session, *, today: date) -> Parsed:
    raw = (text or "").strip()
    if raw.startswith("/"):
        command, _, rest = raw[1:].partition(" ")
        command = command.split("@", 1)[0].lower()
        mapping = {"stock": "πόσα", "apothema": "πόσα", "runout": "τι τελειώνει", "telionei": "τι τελειώνει",
                   "order": "τι να παραγγείλω", "paraggelia": "τι να παραγγείλω", "consumption": "τι καταναλώσαμε",
                   "help": "βοήθεια", "start": "βοήθεια"}
        raw = f"{mapping.get(command, '')} {rest}".strip()
    toks = tokens(raw)
    period, used = parse_period(toks, today)
    flags = {"stock": False, "consume": False, "took": False, "runout": False, "order": False, "help": False, "when": False, "price": False}
    content: list[str] = []
    worker_hits: dict[int, Worker] = {}
    workers = _worker_index(db)
    price_question = _is_price_question(toks)
    if price_question:
        flags["price"] = True
    for i, tok in enumerate(toks):
        if i in used:
            continue
        if _has(tok, HELP) or _has(tok, GREET, exact=True):
            flags["help"] = True
        elif price_question and (_has(tok, PRICE) or tok in PRICE or _has(tok, PRICE_VERBS) or tok in PRICE_VERBS
                                 or tok in _stems("πόσο", "poso")):
            continue
        elif (not price_question) and _has(tok, ORDER):
            flags["order"] = True
        elif _has(tok, RUNOUT):
            flags["runout"] = True
        elif _has(tok, WHEN, exact=True):
            flags["when"] = True
        elif _has(tok, CONSUME):
            flags["consume"] = True
        elif _has(tok, TOOK, exact=True):
            flags["took"] = True
        elif _has(tok, STOCK, exact=True) or tok in STOCK or (len(tok) >= 5 and _has(tok, STOCK)):
            flags["stock"] = True
        elif tok in STOP or _has(tok, P_THIS, exact=True) or _has(tok, P_PREVIOUS):
            continue
        else:
            matched = False
            if not tok.isdigit() and tok not in SIZES and len(tok) >= 4:
                for worker, words in workers:
                    if any(_word_match(tok, w) for w in words):
                        worker_hits.setdefault(worker.id, worker)
                        matched = True
            if not matched:
                content.append(SYNONYMS.get(tok, tok))
    if worker_hits:
        active = [w for w in worker_hits.values() if w.is_active]
        chosen = active or list(worker_hits.values())
        label = ", ".join(w.display_name for w in chosen)
        return Parsed("worker", content, period, [w.id for w in chosen], label)
    if flags["help"] and not content:
        return Parsed("help")
    if flags["price"]:
        return Parsed("price", content, period)
    if flags["order"]:
        return Parsed("reorder", content, period)
    if flags["runout"] or (flags["when"] and content) or (any(_has(t, FEW_PHRASE, exact=True) for t in toks) and not content):
        return Parsed("when" if content else "runout", content, period)
    if flags["consume"] or (period is not None and not flags["stock"]):
        return Parsed("consumption", content, period)
    if flags["stock"]:
        return Parsed("stock", content, period)
    if content and match_items(content, db.query(Consumable).all())[0]:
        return Parsed("stock", content, period)
    return Parsed("unknown", content, period)


# --------------------------------------------------------------------- data


def _qty(value) -> str:
    number = Decimal(str(value or 0))
    if number == number.to_integral_value():
        return str(int(number))
    return f"{number.normalize():f}".replace(".", ",")


_UNIT = {"box": "κούτ.", "pcs": "τεμ.", "τεμάχια": "τεμ.", "τεμ": "τεμ.", "kg": "kg", "pack": "συσκ."}


def _unit(value: str | None) -> str:
    text = (value or "").strip()
    return _UNIT.get(text.lower(), text or "τεμ.")


def _item_words(item: Consumable) -> set[str]:
    words = set(tokens(item.name or ""))
    words.update(tokens(item.category or ""))
    return {SYNONYMS.get(w, w) for w in words}


def match_items(words: list[str], items: list[Consumable]) -> tuple[list[Consumable], list[str]]:
    """Best-matching items for the product words; also returns words that matched nothing."""
    if not words:
        return [], []
    words = [SYNONYMS.get(w, w) for w in words]
    strict = [w for w in words if w.isdigit() or w in SIZES]
    loose = [w for w in words if w not in strict]
    scored = []
    unmatched: set[str] = set(words)
    for item in items:
        item_words = _item_words(item)
        hits = [w for w in loose if any(_word_match(w, t) for t in item_words)]
        if loose and not hits:
            continue
        if any(s not in item_words for s in strict):
            continue
        if not loose and not strict:
            continue
        unmatched -= set(hits) | set(strict)
        name_hits = [w for w in hits if any(_word_match(w, t) for t in tokens(item.name or ""))]
        scored.append((len(hits) + len(strict), len(name_hits), item))
    if not scored:
        return [], list(words)
    best = max(s[0] for s in scored)
    top = [s for s in scored if s[0] == best]
    best_name = max(s[1] for s in top)
    chosen = [s[2] for s in top if s[1] == best_name]
    return sorted(chosen, key=lambda c: (not c.is_active, (c.name or "").casefold())), sorted(unmatched)


def _rows_by_id(db: Session) -> dict[int, dict]:
    try:
        from app.kiosk_insights import _active_rows
    except ImportError:  # pragma: no cover
        from kiosk_insights import _active_rows
    return {int(row["id"]): row for row in _active_rows(db)}


def _forecast_flags(db: Session, now: datetime) -> dict:
    smart = ng.smart_forecast_flags(db, now=now)
    if smart is not None:
        return smart
    try:
        from app.consumption_forecast import forecast_flags
        from app.kiosk_insights import _active_rows
    except ImportError:  # pragma: no cover
        from consumption_forecast import forecast_flags
        from kiosk_insights import _active_rows
    return forecast_flags(db, [int(r["id"]) for r in _active_rows(db)], now=now)


def _days_text(info: dict | None) -> str:
    if not info:
        return ""
    try:
        from app.consumption_forecast import days_phrase, runout_label
    except ImportError:  # pragma: no cover
        from consumption_forecast import days_phrase, runout_label
    days = info.get("days_left")
    if days is None:
        return ""
    text = days_phrase(int(days))
    when = runout_label(info.get("runout_date"))
    return f"{text} ({when})" if when else text


def _consumption(db: Session, period: Period, *, item_ids: list[int] | None = None, worker_ids: list[int] | None = None) -> list[tuple[Consumable, Decimal, int]]:
    start, end = period.utc_bounds()
    query = (
        db.query(ConsumableMovement.consumable_id, ConsumableMovement.movement_type, ConsumableMovement.note,
                 ConsumableMovement.qty)
        .filter(ConsumableMovement.created_at >= start, ConsumableMovement.created_at < end)
        .filter(ConsumableMovement.movement_type.in_(("OUT", "IN")))
    )
    if item_ids is not None:
        query = query.filter(ConsumableMovement.consumable_id.in_(item_ids or [0]))
    if worker_ids is not None:
        query = query.filter(ConsumableMovement.worker_id.in_(worker_ids or [0]))
    totals: dict[int, Decimal] = {}
    counts: dict[int, int] = {}
    for cid, kind, note, qty in query.all():
        amount = Decimal(str(qty or 0))
        if kind == "IN":
            if not (note or "").strip().startswith(UNDO_NOTE):
                continue
            amount = -amount
        else:
            counts[cid] = counts.get(cid, 0) + 1
        totals[cid] = totals.get(cid, Decimal("0")) + amount
    items = {c.id: c for c in db.query(Consumable).filter(Consumable.id.in_(list(totals) or [0])).all()}
    out = [(items[cid], qty, counts.get(cid, 0)) for cid, qty in totals.items() if qty > 0 and cid in items]
    out.sort(key=lambda row: (-row[1], (row[0].name or "").casefold()))
    return out


# --------------------------------------------------------------------- answers

HELP_TEXT = (
    "Ρώτα με για την αποθήκη, π.χ.:\n"
    "• πόσα γάντια έχουμε;\n"
    "• τι καταναλώσαμε τον Νοέμβριο;\n"
    "• τι πήρε ο Βασιλάκης αυτή τη βδομάδα;\n"
    "• τι τελειώνει;\n"
    "• πότε τελειώνει το χαρτί μεγάλο;\n"
    "• τι να παραγγείλω;\n"
    "• τιμή σάλτσα ντομάτας; / πόσο αγοράζουμε το Χ;\n"
    "Για φωνή, ξεκίνα με «πες μου» ή «Αθηνά».\n"
    "Περίοδοι: σήμερα, χθες, αυτή/προηγούμενη εβδομάδα, αυτόν/προηγούμενο μήνα, Νοέμβριο, τελευταίες 7 μέρες.\n"
    "Μόνο ανάγνωση — δεν αλλάζω απόθεμα και δεν στέλνω παραγγελίες."
)


def _not_found(words: list[str], raw_words: str) -> str:
    return f"Δεν βρήκα είδος για «{raw_words}». Δοκίμασε με άλλη λέξη, π.χ. «πόσα γάντια L έχουμε;»."


def _content_text(text: str, parsed: Parsed) -> str:
    keep = []
    for tok in _TOKEN.findall(text or ""):
        sk = SYNONYMS.get(skeleton(tok), skeleton(tok))
        if sk and sk in parsed.words:
            keep.append(tok)
    return " ".join(keep) or (text or "").strip()


def _last_take(db: Session, item_id: int) -> str:
    moment = (
        db.query(func.max(ConsumableMovement.created_at))
        .filter(ConsumableMovement.consumable_id == item_id, ConsumableMovement.movement_type == "OUT")
        .scalar()
    )
    if moment is None:
        return ""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(ATHENS).strftime("%d/%m/%Y")



def _pack_meaning(item: Consumable) -> str:
    """Human pack wording, e.g. «ανά συσκευασία 3kg»."""
    unit = (item.unit or "").strip()
    name = item.name or ""
    notes = item.notes or ""
    weight = None
    found = re.search(r"(\d+(?:[.,]\d+)?\s*(?:kg|κιλ[αάοί]?))\b", name, re.IGNORECASE)
    if not found:
        found = re.search(r"(\d+(?:[.,]\d+)?\s*(?:kg|κιλ[αάοί]?))\b", notes, re.IGNORECASE)
    if found:
        weight = re.sub(r"\s+", "", found.group(1), flags=re.I).replace("ΚΙΛ", "κιλ").replace("Kg", "kg")
        weight = re.sub(r"(?i)κιλα$", "kg", weight)
        weight = re.sub(r"(?i)κιλο$", "kg", weight)
    key = unit.casefold()
    if key in {"kg", "κιλό", "κιλά", "kilo"}:
        return "ανά κιλό"
    if key in {"δέμα"}:
        return f"ανά δέμα {weight}" if weight else "ανά δέμα"
    if key in {"κιβώτιο", "κιβώτια", "box", "κούτ.", "κουτί"}:
        return f"ανά κιβώτιο {weight}" if weight else "ανά κιβώτιο"
    if key in {"πακέτο", "πακέτα", "pack"}:
        return f"ανά πακέτο {weight}" if weight else "ανά πακέτο"
    if key in {"τεμάχιο", "τεμάχια", "τεμ.", "τεμ", "pcs"}:
        return f"ανά συσκευασία {weight}" if weight else "ανά συσκευασία"
    if unit:
        return f"ανά {unit}" + (f" {weight}" if weight else "")
    return f"ανά συσκευασία {weight}" if weight else "ανά συσκευασία"


def _fmt_eur(value) -> str:
    number = Decimal(str(value or 0)).quantize(Decimal("0.01"))
    if number == number.to_integral_value():
        return f"{int(number)},00 €"
    text = f"{number:.2f}".replace(".", ",")
    return f"{text} €"


def _last_purchase_source(db: Session, item: Consumable) -> str | None:
    movement = (
        db.query(ConsumableMovement)
        .filter(ConsumableMovement.consumable_id == item.id, ConsumableMovement.movement_type == "IN")
        .order_by(ConsumableMovement.created_at.desc(), ConsumableMovement.id.desc())
        .first()
    )
    bits: list[str] = []
    if movement and (movement.note or "").strip():
        bits.append(movement.note.strip())
    elif movement and movement.created_at is not None:
        bits.append(movement.created_at.astimezone(ATHENS).strftime("%d/%m/%Y"))
    elif (item.notes or "").strip():
        bits.append(item.notes.strip())
    return bits[0] if bits else None


def answer_price(db: Session, parsed: Parsed, text: str) -> str:
    items, leftover = match_items(parsed.words, db.query(Consumable).all())
    if not items:
        return _not_found(leftover or parsed.words, _content_text(text, parsed))
    suppliers = {}
    ids = {item.supplier_id for item in items if item.supplier_id}
    if ids:
        for row in db.query(Supplier).filter(Supplier.id.in_(ids)).all():
            suppliers[row.id] = row.name
    lines: list[str] = []
    for item in items[:MAX_LINES]:
        cost = Decimal(str(item.cost_per_pack or 0))
        block = [item.name]
        if cost == 0:
            block.append("δεν έχει καταχωρηθεί τιμή")
        else:
            block.append(f"{_fmt_eur(cost)} {_pack_meaning(item)}")
        supplier = suppliers.get(item.supplier_id) if item.supplier_id else None
        if supplier:
            block.append(f"Προμηθευτής: {supplier}")
        source = _last_purchase_source(db, item)
        if source:
            block.append(f"Τελευταία αγορά: {source}")
        lines.append("\n".join(block))
    extra = len(items) - MAX_LINES
    if extra > 0:
        lines.append(f"…και άλλα {extra}.")
    return "\n\n".join(lines)


def answer_stock(db: Session, parsed: Parsed, text: str, now: datetime) -> str:
    items = db.query(Consumable).filter(Consumable.is_active.is_(True)).all()
    if not parsed.words:
        return "Για ποιο είδος; π.χ. «πόσα γάντια έχουμε;» ή «πόσα σκαφάκια 750 έχουμε;». Για όσα τελειώνουν: «τι τελειώνει;»."
    matched, _rest = match_items(parsed.words, items)
    if not matched:
        return _not_found(parsed.words, _content_text(text, parsed))
    rows = _rows_by_id(db)
    flags = _forecast_flags(db, now)
    lines = ["📦 Απόθεμα τώρα:"]
    totals: dict[str, Decimal] = {}
    for item in matched[:MAX_LINES]:
        row = rows.get(int(item.id))
        on_hand = Decimal(str(row["on_hand"])) if row else Decimal("0")
        unit = _unit(item.unit)
        totals[unit] = totals.get(unit, Decimal("0")) + on_hand
        line = f"• {item.name}: {_qty(on_hand)} {unit}"
        extra = []
        if row and Decimal(str(row.get("on_order") or 0)) > 0:
            extra.append(f"+{_qty(row['on_order'])} σε παραγγελία")
        if row and Decimal(str(row.get("min_qty") or 0)) > 0 and on_hand <= Decimal(str(row["min_qty"])):
            extra.append("στο ελάχιστο")
        days = _days_text(flags.get(int(item.id)))
        if days:
            extra.append(days)
        elif len(matched) <= 3:
            last = _last_take(db, int(item.id))
            if last:
                extra.append(f"τελευταία λήψη {last}")
        if extra:
            line += " — " + ", ".join(extra)
        lines.append(line)
    if len(matched) > MAX_LINES:
        lines.append(f"… και άλλα {len(matched) - MAX_LINES}")
    if len(matched) > 1 and len(totals) == 1:
        unit, total = next(iter(totals.items()))
        lines.append(f"Σύνολο: {_qty(total)} {unit}")
    return "\n".join(lines)


def _default_period(parsed: Parsed, today: date, *, week: bool) -> Period:
    if parsed.period is not None:
        return parsed.period
    if week:
        start = _week_start(today)
        return Period(start, today + timedelta(days=1), f"αυτή την εβδομάδα ({_fmt_day(start)}–{_fmt_day(today)})")
    return _month_period(today.year, today.month, today)


def answer_consumption(db: Session, parsed: Parsed, text: str, today: date) -> str:
    period = _default_period(parsed, today, week=False)
    item_ids = None
    if parsed.words:
        matched, _rest = match_items(parsed.words, db.query(Consumable).all())
        if not matched:
            return _not_found(parsed.words, _content_text(text, parsed))
        item_ids = [int(c.id) for c in matched]
    rows = _consumption(db, period, item_ids=item_ids)
    if not rows:
        return f"📊 Κατανάλωση — {period.label}: καμία λήψη καταχωρημένη."
    lines = [f"📊 Κατανάλωση — {period.label}:"]
    for item, qty, count in rows[:MAX_LINES]:
        lines.append(f"• {item.name}: {_qty(qty)} {_unit(item.unit)} ({count} {'λήψη' if count == 1 else 'λήψεις'})")
    if len(rows) > MAX_LINES:
        lines.append(f"… και άλλα {len(rows) - MAX_LINES} είδη")
    lines.append(f"Σύνολο: {sum(r[2] for r in rows)} λήψεις σε {len(rows)} είδη.")
    return "\n".join(lines)


def answer_worker(db: Session, parsed: Parsed, text: str, today: date) -> str:
    period = _default_period(parsed, today, week=True)
    item_ids = None
    if parsed.words:
        matched, _rest = match_items(parsed.words, db.query(Consumable).all())
        item_ids = [int(c.id) for c in matched] if matched else None
    rows = _consumption(db, period, item_ids=item_ids, worker_ids=parsed.worker_ids)
    who = parsed.worker_label
    if not rows:
        reply = f"👤 {who} — {period.label}: καμία λήψη με το όνομά του/της."
        start, end = period.utc_bounds()
        anonymous = (
            db.query(func.count(ConsumableMovement.id))
            .filter(ConsumableMovement.movement_type == "OUT", ConsumableMovement.worker_id.is_(None))
            .filter(ConsumableMovement.created_at >= start, ConsumableMovement.created_at < end)
            .scalar()
        )
        if anonymous:
            reply += f"\n({anonymous} λήψεις της περιόδου δεν έχουν όνομα εργαζόμενου — γράφτηκαν χωρίς PIN.)"
        return reply
    lines = [f"👤 {who} — {period.label}:"]
    for item, qty, count in rows[:MAX_LINES]:
        lines.append(f"• {item.name}: {_qty(qty)} {_unit(item.unit)} ({count} {'φορά' if count == 1 else 'φορές'})")
    if len(rows) > MAX_LINES:
        lines.append(f"… και άλλα {len(rows) - MAX_LINES} είδη")
    return "\n".join(lines)


def answer_runout(db: Session, now: datetime) -> str:
    try:
        from app.kiosk_insights import runout_ranking
    except ImportError:  # pragma: no cover
        from kiosk_insights import runout_ranking
    flags = ng.smart_forecast_flags(db, now=now)
    ranking = [row for row in runout_ranking(db, now=now, flags=flags) if row["level"] in ("now", "soon")]
    if not ranking:
        return "✅ Τίποτα δεν τελειώνει τις επόμενες μέρες."
    lines = ["⏳ Τελειώνουν:"]
    for row in ranking[:MAX_LINES]:
        when = f" ({row['runout_label']})" if row.get("runout_label") else ""
        info = (flags or {}).get(int(row["id"])) or {}
        hint = f" · {info['note']}" if info.get("note") else ""
        lines.append(f"• {row['name']}: {row['on_hand_display']} {_unit(row['unit'])} — {row['days_phrase']}{when}{hint}")
    if len(ranking) > MAX_LINES:
        lines.append(f"… και άλλα {len(ranking) - MAX_LINES}")
    return "\n".join(lines)


def answer_when(db: Session, parsed: Parsed, text: str, now: datetime) -> str:
    matched, _rest = match_items(parsed.words, db.query(Consumable).filter(Consumable.is_active.is_(True)).all())
    if not matched:
        return _not_found(parsed.words, _content_text(text, parsed))
    rows = _rows_by_id(db)
    flags = _forecast_flags(db, now)
    lines = ["⏳ Πότε τελειώνει:"]
    for item in matched[:MAX_LINES]:
        row = rows.get(int(item.id))
        info = flags.get(int(item.id))
        stock = f"{row['on_hand_display']} {_unit(item.unit)}" if row else "—"
        days = _days_text(info)
        if days:
            weekly = str(info.get("weekly_qty") or "").replace(".", ",")
            extra = f", ~{weekly}/εβδ." if weekly else ""
            note = f" · {info['note']}" if info.get("note") else ""
            lines.append(f"• {item.name}: {stock} — {days}{extra}{note}")
        else:
            lines.append(f"• {item.name}: {stock} — δεν φαίνεται να τελειώνει σύντομα (λίγες λήψεις)")
    return "\n".join(lines)


def answer_reorder(db: Session, now: datetime) -> str:
    try:
        from app.kiosk_insights import reorder_plan
    except ImportError:  # pragma: no cover
        from kiosk_insights import reorder_plan
    plans = reorder_plan(db, now=now, flags=ng.smart_forecast_flags(db, now=now))
    if not plans:
        return "✅ Δεν χρειάζεται παραγγελία αυτή τη στιγμή."
    lines = ["🛒 Πρόταση παραγγελίας (μόνο πρόταση — δεν στέλνεται τίποτα):"]
    shown = 0
    for plan in plans:
        lines.append(f"\n{plan['supplier_name']}")
        for line in plan["lines"]:
            if shown >= MAX_LINES:
                break
            lines.append(f"• {line['name']}: {line['packs']} συσκ. ({line['units_display']} {_unit(line['unit'])}) — {line['reason']}")
            shown += 1
    lines.append("\nΈγκριση: από την εφαρμογή (Νέες λειτουργίες → Παραγγελίες για έγκριση).")
    return "\n".join(lines)


def _llm_fallback(db: Session, text: str) -> str | None:
    """Only with the switch on AND an API key in the environment (none by default)."""
    if not ng.flag_on(db, ng.TELEGRAM_ASK_LLM):
        return None
    if not os.getenv("TELEGRAM_ASK_LLM_API_KEY", "").strip():
        return None
    # Deliberately not implemented: no paid provider is configured or billed.
    logger.info("telegram ask: AI fallback switched on but no provider is wired; skipped")
    return None


def answer(db: Session, text: str, *, now: datetime | None = None) -> tuple[str, str]:
    """``(intent, reply text)``. Pure reads."""
    moment = now or datetime.now(timezone.utc)
    today = moment.astimezone(ATHENS).date()
    parsed = parse(text, db, today=today)
    if parsed.intent == "price":
        return parsed.intent, answer_price(db, parsed, text)
    if parsed.intent == "stock":
        return parsed.intent, answer_stock(db, parsed, text, moment)
    if parsed.intent == "consumption":
        return parsed.intent, answer_consumption(db, parsed, text, today)
    if parsed.intent == "worker":
        return parsed.intent, answer_worker(db, parsed, text, today)
    if parsed.intent == "runout":
        return parsed.intent, answer_runout(db, moment)
    if parsed.intent == "when":
        return parsed.intent, answer_when(db, parsed, text, moment)
    if parsed.intent == "reorder":
        return parsed.intent, answer_reorder(db, moment)
    if parsed.intent == "help":
        return parsed.intent, HELP_TEXT
    fallback = _llm_fallback(db, text)
    if fallback:
        return "llm", fallback
    return "unknown", "Δεν κατάλαβα την ερώτηση.\n\n" + HELP_TEXT


# --------------------------------------------------------------------- webhook


def allowed_chat_ids() -> set[str]:
    try:
        from app.digest_service import consumables_chat_ids
    except ImportError:  # pragma: no cover
        from digest_service import consumables_chat_ids
    return set(consumables_chat_ids())


def webhook_secret() -> str:
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
    if len(secret) < 24 or not re.fullmatch(r"[A-Za-z0-9_-]{24,256}", secret):
        return ""
    return secret


def _rate_limited(db: Session, chat_id: str, now: datetime) -> bool:
    count = (
        db.query(func.count(TelegramAskLog.id))
        .filter(TelegramAskLog.chat_id == chat_id, TelegramAskLog.allowed.is_(True))
        .filter(TelegramAskLog.created_at >= now - RATE_WINDOW)
        .scalar()
    )
    return int(count or 0) >= RATE_LIMIT


def _log(db: Session, *, update_id, chat_id: str, allowed: bool, intent: str, question: str, now: datetime) -> bool:
    """Insert the log row; False when this update_id was already handled."""
    db.add(TelegramAskLog(update_id=update_id, chat_id=chat_id[:40], allowed=allowed, intent=intent[:30],
                          question=(question or "")[:300], created_at=now))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return False
    return True


OK = {"ok": True}


def _send_telegram_voice(chat_id, audio: bytes, reply_to=None) -> None:
    """POST sendVoice to Telegram. Never logs the bot token; skips empty audio."""
    import json
    import urllib.error
    import urllib.request

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or not audio:
        return
    boundary = "----whPiperVoice"
    parts: list[bytes] = []

    def field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode()
        )

    field("chat_id", str(chat_id))
    if reply_to is not None:
        field("reply_to_message_id", str(reply_to))
    field("disable_notification", "true")
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"voice\"; filename=\"answer.ogg\"\r\n"
        f"Content-Type: audio/ogg\r\n\r\n".encode()
        + audio
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    body = b"".join(parts)
    url = f"https://api.telegram.org/bot{token}/sendVoice"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="replace") or "{}")
        if not payload.get("ok"):
            logger.warning("sendVoice was not accepted")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        # Do not log the exception (the request URL contains the bot token).
        logger.warning("sendVoice failed status=%s type=%s", getattr(exc, "code", None), type(exc).__name__)


def _speak_reply(chat_id, reply: str, reply_to=None) -> None:
    audio = piper_voice.synthesize_ogg(reply)
    if audio:
        _send_telegram_voice(chat_id, audio, reply_to=reply_to)


@router.post(WEBHOOK_PATH)
async def telegram_ask_webhook(request: Request, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    if not ng.flag_on(db, ng.TELEGRAM_ASK):
        return JSONResponse({"ok": False}, status_code=404)
    secret = webhook_secret()
    supplied = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not secret or not hmac.compare_digest(secret.encode(), supplied.encode()):
        return JSONResponse({"ok": False}, status_code=403)
    try:
        update = await request.json()
    except ValueError:
        return JSONResponse(OK)
    if not isinstance(update, dict):
        return JSONResponse(OK)
    message = update.get("message") or update.get("edited_message") or {}
    chat = message.get("chat") or {}
    sender = message.get("from") or {}
    text = message.get("text")
    update_id = update.get("update_id")
    update_id = int(update_id) if isinstance(update_id, int) else None
    if not isinstance(text, str) or not text.strip() or sender.get("is_bot") or "id" not in chat:
        return JSONResponse(OK)
    chat_id = str(chat.get("id"))
    now = datetime.now(timezone.utc)
    if chat_id not in allowed_chat_ids():
        _log(db, update_id=update_id, chat_id=chat_id, allowed=False, intent="denied", question="", now=now)
        return JSONResponse(OK)  # silent: strangers learn nothing
    if _rate_limited(db, chat_id, now):
        if not _log(db, update_id=update_id, chat_id=chat_id, allowed=False, intent="rate_limited", question=text, now=now):
            return JSONResponse(OK)
        reply = "Πολλές ερωτήσεις μαζί — δοκίμασε ξανά σε λίγα λεπτά."
        intent = "rate_limited"
    else:
        try:
            question, speak = strip_voice_trigger(text[:300])
            intent, reply = answer(db, question, now=now)
        except Exception:
            logger.exception("telegram ask failed")
            db.rollback()
            intent, reply = "error", "Κάτι πήγε στραβά με την απάντηση. Δοκίμασε ξανά."
            speak = False
        if not _log(db, update_id=update_id, chat_id=chat_id, allowed=True, intent=intent, question=text, now=now):
            return JSONResponse(OK)  # Telegram re-delivered an update we already answered
        if speak and intent not in {"denied"} and background_tasks is not None:
            background_tasks.add_task(_speak_reply, chat.get("id"), reply, message.get("message_id"))
    return JSONResponse({
        "method": "sendMessage",
        "chat_id": chat.get("id"),
        "text": reply[:4000],
        "reply_to_message_id": message.get("message_id"),
        "disable_web_page_preview": True,
    })


def status(db: Session) -> dict:
    """For the admin page: what is configured (never the values)."""
    since = datetime.now(timezone.utc) - timedelta(days=7)
    rows = db.query(TelegramAskLog).filter(TelegramAskLog.created_at >= since).all()
    return {
        "on": ng.flag_on(db, ng.TELEGRAM_ASK),
        "token_set": bool(os.getenv("TELEGRAM_BOT_TOKEN", "").strip()),
        "chats": len(allowed_chat_ids()),
        "secret_set": bool(webhook_secret()),
        "llm_on": ng.flag_on(db, ng.TELEGRAM_ASK_LLM),
        "llm_key": bool(os.getenv("TELEGRAM_ASK_LLM_API_KEY", "").strip()),
        "week_answered": sum(1 for r in rows if r.allowed),
        "week_denied": sum(1 for r in rows if not r.allowed),
        "path": WEBHOOK_PATH,
    }


__all__ = ["skeleton", "tokens", "parse", "parse_period", "match_items", "answer", "router", "status", "HELP_TEXT", "WEBHOOK_PATH", "strip_voice_trigger"]
