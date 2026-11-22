"""Compact JPEG thumbnails stored on the consumable row.

Railway already has Postgres, so the bytes live in ``consumables.image_bytes``
instead of a new object store. The column is deferred: list pages do not pull
the blob unless a request asks for one image.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageOps

MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_EDGE = 480
JPEG_QUALITY = 72


def initials(name: str) -> str:
    parts = [part for part in (name or "").replace("-", " ").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def card_webp(raw: bytes) -> bytes:
    """Small WebP for a kiosk card. Falls back to the stored bytes if encode fails."""
    try:
        image = Image.open(BytesIO(raw))
        image = ImageOps.exif_transpose(image).convert("RGB")
        image.thumbnail((160, 160))
        out = BytesIO()
        image.save(out, format="WEBP", quality=60, method=0)
        return out.getvalue()
    except Exception:
        return raw


def jpeg_thumbnail(raw: bytes) -> bytes:
    if not raw:
        raise ValueError("empty image")
    if len(raw) > MAX_UPLOAD_BYTES:
        raise ValueError("image too large")
    try:
        image = Image.open(BytesIO(raw))
        image = ImageOps.exif_transpose(image)
        image = image.convert("RGB")
    except Exception as exc:
        raise ValueError("not an image") from exc
    image.thumbnail((MAX_EDGE, MAX_EDGE))
    out = BytesIO()
    image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return out.getvalue()
