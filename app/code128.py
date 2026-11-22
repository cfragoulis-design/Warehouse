"""Code 128 set B bars. The value is the consumable barcode the kiosk already scans."""

from __future__ import annotations

_PATTERNS = (
    "212222,222122,222221,121223,121322,131222,122213,122312,132212,221213,221312,231212,"
    "112232,122132,122231,113222,123122,123221,223211,221132,221231,213212,223112,312131,"
    "311222,321122,321221,312212,322112,322211,212123,212321,232121,111323,131123,131321,"
    "112313,132113,132311,211313,231113,231311,112133,112331,132131,113123,113321,133121,"
    "313121,211331,231131,213113,213311,213131,311123,311321,331121,312113,312311,332111,"
    "314111,221411,431111,111224,111422,121124,121421,141122,141221,112214,112412,122114,"
    "122411,142112,142211,241211,221114,413111,241112,134111,111242,121142,121241,114212,"
    "124112,124211,411212,421112,421211,212141,214121,412121,111143,111341,131141,114113,"
    "114311,411113,411311,113141,114131,311141,411131,211412,211214,211232,2331112"
).split(",")
START_B = 104


def code128_values(text: str) -> list[int]:
    if not text or any(ord(ch) < 32 or ord(ch) > 126 for ch in text):
        raise ValueError("Code 128 set B only")
    values = [ord(ch) - 32 for ch in text]
    checksum = START_B
    for index, value in enumerate(values, start=1):
        checksum += index * value
    return [START_B, *values, checksum % 103]


def code128_bars(text: str) -> str:
    values = code128_values(text)
    return "".join(_PATTERNS[value] for value in values) + _PATTERNS[-1]


def barcode_svg(text: str, *, height: int = 48) -> str:
    bars = code128_bars(text)
    x = 0
    parts = []
    for index, width in enumerate(bars):
        w = int(width)
        if index % 2 == 0:
            parts.append(f'<rect x="{x}" y="0" width="{w}" height="{height}" fill="#000"/>')
        x += w
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {x} {height}" width="{x}" height="{height}">{"".join(parts)}</svg>'
