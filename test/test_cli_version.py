"""Tests for CLI version handling."""

from __future__ import annotations

import importlib
from importlib import metadata as importlib_metadata
from pathlib import Path
import sys
from types import ModuleType
from typing import Callable

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _reload_cli_with_version_stub(
    monkeypatch: pytest.MonkeyPatch, stub: Callable[[str], str]
) -> ModuleType:
    """Reload ``any2summary.cli`` with a patched ``metadata.version``."""

    monkeypatch.setattr(importlib_metadata, "version", stub, raising=False)
    sys.modules.pop("any2summary.cli", None)
    return importlib.import_module("any2summary.cli")


def teardown_module(_module: ModuleType) -> None:
    """Restore ``any2summary.cli`` once this module's tests finish."""

    sys.modules.pop("any2summary.cli", None)
    importlib.import_module("any2summary.cli")


def test_version_flag_prefers_any2summary_distribution(monkeypatch, capsys):
    """The CLI should read version metadata from ``any2summary`` only."""

    calls: list[str] = []

    def fake_version(name: str) -> str:
        calls.append(name)
        if name == "any2summary":
            return "9.9.9"
        raise importlib_metadata.PackageNotFoundError(name)

    cli = _reload_cli_with_version_stub(monkeypatch, fake_version)

    with pytest.raises(SystemExit) as excinfo:
        cli.run(["--version"])

    assert excinfo.value.code == 0
    assert calls == ["any2summary"]
    assert capsys.readouterr().out.strip() == "any2summary 9.9.9"


def test_version_flag_falls_back_to_placeholder_when_package_missing(monkeypatch, capsys):
    """When metadata is missing, the CLI should emit 0.0.0 without fallback."""

    calls: list[str] = []

    def fake_version(name: str) -> str:
        calls.append(name)
        raise importlib_metadata.PackageNotFoundError(name)

    cli = _reload_cli_with_version_stub(monkeypatch, fake_version)

    with pytest.raises(SystemExit) as excinfo:
        cli.run(["--version"])

    assert excinfo.value.code == 0
    assert calls == ["any2summary"]
    assert capsys.readouterr().out.strip() == "any2summary 0.0.0"
