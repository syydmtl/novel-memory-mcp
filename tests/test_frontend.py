from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


def test_frontend_regressions() -> None:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for frontend state regression tests")
    script = Path(__file__).with_name("frontend.test.cjs")
    result = subprocess.run(
        [node, "--test", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=40, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
