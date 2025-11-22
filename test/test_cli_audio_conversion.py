"""测试音频转换命令参数。"""

from __future__ import annotations

import subprocess
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "any2summary"
sys.path.insert(0, str(PACKAGE_ROOT))

from any2summary import cli


def test_convert_audio_to_wav_requests_pcm(monkeypatch: pytest.MonkeyPatch) -> None:
    """convert_audio_to_wav 应该强制使用 16-bit PCM WAV。"""

    captured: dict[str, list[str]] = {}

    def fake_run(
        command: list[str],
        check: bool,
        stdout: int,
        stderr: int,
    ) -> SimpleNamespace:
        captured["command"] = command
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", fake_run)

    cli.convert_audio_to_wav("input.mp3", "output.wav")

    command = captured["command"]
    assert command[:3] == ["ffmpeg", "-y", "-i"]
    assert "-acodec" in command
    assert "pcm_s16le" in command
    assert command.count("-f") == 1
    assert command[command.index("-f") + 1] == "wav"
