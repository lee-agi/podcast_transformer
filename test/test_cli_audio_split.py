"""针对音频切分工具函数的测试。"""

from __future__ import annotations

import wave
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from any2summary import cli


def _write_silent_wav(path: Path, duration_seconds: float, sample_rate: int = 8000) -> None:
    """生成指定时长的静音 WAV 文件。"""

    total_frames = int(duration_seconds * sample_rate)
    if total_frames <= 0:
        total_frames = sample_rate
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(b"\x00\x00" * total_frames)


def test_ensure_audio_segments_splits_large_wav(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """超出阈值的音频应被自动切分成多个片段。"""

    original = tmp_path / "audio.wav"
    _write_silent_wav(original, duration_seconds=1.5, sample_rate=8000)

    monkeypatch.setattr(cli, "MAX_WAV_DURATION_SECONDS", 1.0)
    monkeypatch.setattr(cli, "MAX_WAV_SIZE_BYTES", 1024 * 1024)
    monkeypatch.setattr(cli, "AUDIO_SEGMENT_SECONDS", 0.6)

    segments = cli._ensure_audio_segments(str(original))

    assert len(segments) == 3
    for index, segment in enumerate(segments, start=1):
        path = Path(segment)
        assert path.exists()
        assert path.name == f"audio_part{index:03d}.wav"


def test_ensure_audio_segments_respects_azure_limit(tmp_path: Path) -> None:
    """超过 Azure 限制的音频应当按默认阈值切分。"""

    azure_safe_duration = cli.AUDIO_SEGMENT_SECONDS + 200.0
    wav_path = tmp_path / "long_audio.wav"
    _write_silent_wav(wav_path, duration_seconds=azure_safe_duration, sample_rate=10)

    segments = cli._ensure_audio_segments(str(wav_path))

    assert len(segments) >= 2
    for segment in segments:
        duration = cli._get_wav_duration(segment)
        assert duration <= cli.AUDIO_SEGMENT_SECONDS + 1.0
