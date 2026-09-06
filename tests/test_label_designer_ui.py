"""Focused browser-logic checks using Node's standard-library VM.

RAW LOGIC. REAL SYSTEMS.
Created by Christos Fragoulis
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from label_designer_preview_server import fixtures


def test_designer_profile_logic() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the focused Designer browser-logic checks")
    script = Path(__file__).with_name("label_designer_ui.test.cjs")
    result = subprocess.run(
        [node, str(script)], input=json.dumps(fixtures(), ensure_ascii=False),
        text=True, capture_output=True, encoding="utf-8", check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
