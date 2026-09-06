"""Extract the approved English shop logo for the monochrome HPRT printer.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ASSET_NAME = "company-logo-sklavounos-english.png"


def prepare(source: Path) -> str:
    """Rasterize page one, remove white paper margins, retain the original art."""
    with tempfile.TemporaryDirectory(prefix="warehouse-logo-") as temporary:
        prefix = Path(temporary) / "source"
        subprocess.run(
            ["pdftoppm", "-f", "1", "-singlefile", "-scale-to", "1600", "-png",
             str(source), str(prefix)],
            check=True,
            capture_output=True,
        )
        with Image.open(prefix.with_suffix(".png")) as raster:
            grayscale = raster.convert("L")
            ink = grayscale.point(lambda value: 255 if value < 200 else 0)
            bounds = ink.getbbox()
            if not bounds:
                raise ValueError("The supplied PDF has no printable logo artwork.")
            artwork = grayscale.crop(bounds)
            artwork.thumbnail((496, 496), Image.Resampling.LANCZOS)
            canvas = Image.new("L", (512, 512), 255)
            canvas.paste(artwork, ((512 - artwork.width) // 2, (512 - artwork.height) // 2))
            # The HPRT is monochrome. No new design, lettering, or claims are added.
            canvas = canvas.point(lambda value: 0 if value < 200 else 255).convert("1")
            destinations = (
                ROOT / "app" / "static" / ASSET_NAME,
                ROOT / "scripts" / "windows" / "hprt-warehouse-agent" / ASSET_NAME,
            )
            for destination in destinations:
                canvas.save(destination, format="PNG", optimize=True)
    return hashlib.sha256(destinations[0].read_bytes()).hexdigest()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    print(f"Source SHA256: {hashlib.sha256(args.source.read_bytes()).hexdigest()}")
    print(f"Asset SHA256: {prepare(args.source)}")
