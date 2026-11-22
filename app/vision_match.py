"""Product recognition for the kiosk webcam, without paid APIs.

The kiosk browser only notices that something was held up in front of the
camera and sends one small cropped JPEG. The server turns it into a 1280-d
MobileNetV2 image embedding (numpy only, ~20 ms, see ``vision_cnn``) and
compares it with the catalog photos of the active items. The worker taps the
right suggestion; that frame is then stored as an extra reference for the item,
so recognition learns what the products look like in real light at this kiosk.

Embeddings are cached in ``consumable_visual_refs`` (catalog photos are
re-read only when the photo changes), as int8 base64 (1.7 kB each).

Synthetic check on the 57 catalog photos (crop, tilt, light, blur, JPEG):
correct photo in the top 3 for 97 % of frames, first for 87 %; with two
learned frames per item 100 % / 94 %. Items that share one catalog photo
(glove sizes, vacuum-bag sizes) are told apart by the worker's tap.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
from datetime import datetime, timezone

import numpy as np
from PIL import Image, ImageChops, ImageOps
from sqlalchemy.orm import Session, undefer

try:
    from app import vision_cnn
    from app.models import Consumable, ConsumableVisualRef
except ImportError:  # pragma: no cover - flat module layout
    import vision_cnn
    from models import Consumable, ConsumableVisualRef

MODEL_TAG = "mnv2-160"
MAX_KIOSK_REFS = 12
MAX_UPLOAD_BYTES = 600_000
CONFIDENT_SCORE = 0.65
CONFIDENT_MARGIN = 0.05
KIOSK_BONUS = 0.02


def _open(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.draft("RGB", (320, 320))
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        canvas = Image.new("RGBA", image.size, (255, 255, 255, 255))
        canvas.alpha_composite(image)
        image = canvas
    return image.convert("RGB")


def _trim_background(image: Image.Image) -> Image.Image:
    """Crop a plain border (catalog photos sit on one flat colour)."""
    corner = image.getpixel((0, 0))
    background = Image.new("RGB", image.size, corner)
    diff = ImageChops.difference(image, background).convert("L").point(lambda v: 255 if v > 24 else 0)
    box = diff.getbbox()
    if not box:
        return image
    width, height = image.size
    if (box[2] - box[0]) * (box[3] - box[1]) < 0.05 * width * height:
        return image
    return image.crop(box)


def describe(data: bytes, *, trim: bool = True) -> dict:
    image = _open(data)
    if trim:
        image = _trim_background(image)
    return {"m": MODEL_TAG, "v": vision_cnn.pack(vision_cnn.embed_image(image))}


def _vector(features: dict) -> np.ndarray | None:
    if not isinstance(features, dict) or features.get("m") != MODEL_TAG:
        return None
    try:
        return vision_cnn.unpack(features["v"])
    except Exception:
        return None


def similarity(a: dict, b: dict) -> float:
    va, vb = _vector(a), _vector(b)
    if va is None or vb is None:
        return 0.0
    return float(va @ vb)


# --------------------------------------------------------------------- reference cache

_cache_lock = threading.Lock()
_cache: dict = {"stamp": None, "refs": []}


def _stamp(db: Session) -> tuple:
    from sqlalchemy import func

    count, top = db.query(func.count(ConsumableVisualRef.id), func.max(ConsumableVisualRef.id)).one()
    return (int(count or 0), int(top or 0))


def sync_catalog_refs(db: Session) -> int:
    """(Re)compute descriptors for catalog photos that are new or changed. Returns updates."""
    existing = {
        ref.consumable_id: ref
        for ref in db.query(ConsumableVisualRef).filter(ConsumableVisualRef.source == "catalog").all()
    }
    changed = 0
    items = (
        db.query(Consumable)
        .options(undefer(Consumable.image_bytes))
        .filter(Consumable.is_active.is_(True))
        .filter(Consumable.image_bytes.isnot(None))
        .all()
    )
    for item in items:
        data = bytes(item.image_bytes or b"")
        if not data:
            continue
        digest = hashlib.sha256(data).hexdigest()
        ref = existing.get(item.id)
        if ref is not None and ref.image_sha256 == digest and f'"m":"{MODEL_TAG}"' in (ref.features or ""):
            continue
        try:
            features = json.dumps(describe(data), separators=(",", ":"))
        except Exception:
            continue
        if ref is None:
            db.add(ConsumableVisualRef(consumable_id=item.id, source="catalog", image_sha256=digest, features=features, created_at=datetime.now(timezone.utc)))
        else:
            ref.image_sha256 = digest
            ref.features = features
            ref.created_at = datetime.now(timezone.utc)
        changed += 1
    if changed:
        db.commit()
    return changed


def _load_refs(db: Session) -> list[tuple[int, str, np.ndarray]]:
    stamp = _stamp(db)
    with _cache_lock:
        if _cache["stamp"] == stamp:
            return _cache["refs"]
    rows = (
        db.query(ConsumableVisualRef.consumable_id, ConsumableVisualRef.source, ConsumableVisualRef.features)
        .join(Consumable, Consumable.id == ConsumableVisualRef.consumable_id)
        .filter(Consumable.is_active.is_(True))
        .all()
    )
    refs = []
    for cid, source, features in rows:
        try:
            vector = _vector(json.loads(features))
        except ValueError:
            vector = None
        if vector is not None:
            refs.append((int(cid), source, vector))
    with _cache_lock:
        _cache["stamp"] = stamp
        _cache["refs"] = refs
    return refs


def clear_cache() -> None:
    with _cache_lock:
        _cache["stamp"] = None
        _cache["refs"] = []


def match(db: Session, data: bytes, *, limit: int = 3) -> dict:
    if not data or len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("image missing or too large")
    sync_catalog_refs(db)
    query = _vector(describe(data, trim=False))
    best: dict[int, float] = {}
    for cid, source, vector in _load_refs(db):
        score = float(query @ vector)
        if source == "kiosk":
            score += KIOSK_BONUS  # real frames from this kiosk are the better witness
        if score > best.get(cid, -1.0):
            best[cid] = score
    ranked = sorted(best.items(), key=lambda pair: -pair[1])[:limit]
    names = {
        item.id: item
        for item in db.query(Consumable).filter(Consumable.id.in_([cid for cid, _ in ranked])).all()
    } if ranked else {}
    suggestions = [
        {"id": cid, "name": names[cid].name, "unit": names[cid].unit or "τεμ", "score": round(score, 3)}
        for cid, score in ranked
        if cid in names
    ]
    confident = bool(
        suggestions
        and suggestions[0]["score"] >= CONFIDENT_SCORE
        and (len(suggestions) == 1 or suggestions[0]["score"] - suggestions[1]["score"] >= CONFIDENT_MARGIN)
    )
    return {"ok": True, "suggestions": suggestions, "confident": confident}


def learn(db: Session, consumable_id: int, data: bytes, *, worker_id: int | None = None) -> bool:
    """Keep the confirmed kiosk frame as a reference (newest ``MAX_KIOSK_REFS`` per item)."""
    if not data or len(data) > MAX_UPLOAD_BYTES:
        return False
    item = db.get(Consumable, consumable_id)
    if item is None or not item.is_active:
        return False
    features = json.dumps(describe(data, trim=False), separators=(",", ":"))
    db.add(ConsumableVisualRef(
        consumable_id=consumable_id, source="kiosk", image_sha256=hashlib.sha256(data).hexdigest(),
        features=features, worker_id=worker_id, created_at=datetime.now(timezone.utc),
    ))
    db.flush()
    old = (
        db.query(ConsumableVisualRef)
        .filter(ConsumableVisualRef.consumable_id == consumable_id, ConsumableVisualRef.source == "kiosk")
        .order_by(ConsumableVisualRef.id.desc())
        .offset(MAX_KIOSK_REFS)
        .all()
    )
    for row in old:
        db.delete(row)
    db.commit()
    return True


def coverage(db: Session) -> dict:
    total = db.query(Consumable).filter(Consumable.is_active.is_(True)).count()
    with_photo = (
        db.query(Consumable)
        .filter(Consumable.is_active.is_(True))
        .filter(Consumable.image_bytes.isnot(None))
        .count()
    )
    learned = db.query(ConsumableVisualRef).filter(ConsumableVisualRef.source == "kiosk").count()
    return {"items": total, "with_photo": with_photo, "learned_frames": learned}
