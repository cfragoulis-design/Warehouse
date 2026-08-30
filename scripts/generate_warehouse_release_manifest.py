from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.dont_write_bytecode = True

from app.release_manifest import (  # noqa: E402
    RELEASE_MANIFEST_FILENAME,
    build_release_manifest,
    canonical_manifest_bytes,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate a canonical Warehouse CLI release manifest"
    )
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--candidate-commit", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root.resolve(strict=True)
    manifest = build_release_manifest(root, candidate_commit=args.candidate_commit)
    manifest_bytes = canonical_manifest_bytes(manifest)
    manifest_path = root / RELEASE_MANIFEST_FILENAME
    manifest_path.write_bytes(manifest_bytes)
    print(
        json.dumps(
            {
                "candidate_commit": manifest["candidate_commit"],
                "file_count": len(manifest["files"]),
                "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                "tree_sha256": manifest["tree_sha256"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
