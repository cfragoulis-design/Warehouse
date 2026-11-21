from io import BytesIO

from PIL import Image

from app.consumable_images import card_webp
from app.kiosk_session import greeklish_fold


def test_greeklish_matches_greek_without_accents() -> None:
    assert greeklish_fold("alati") == greeklish_fold("Αλάτι")
    assert greeklish_fold("sapouni") == greeklish_fold("σαπούνι")


def test_card_image_is_small_webp() -> None:
    image = Image.new("RGB", (400, 300), (10, 20, 30))
    raw = BytesIO()
    image.save(raw, format="JPEG")
    webp = card_webp(raw.getvalue())
    assert webp[:4] == b"RIFF"
    with Image.open(BytesIO(webp)) as decoded:
        assert max(decoded.size) <= 160
