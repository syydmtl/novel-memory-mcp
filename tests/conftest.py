from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def isolate_novel_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never inherit a real story directory or credential path."""
    for name in os.environ:
        if name.startswith("NOVEL_MEMORY_"):
            monkeypatch.delenv(name)
