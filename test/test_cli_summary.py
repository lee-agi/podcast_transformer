"""Tests covering Azure GPT-5 translation summary workflow."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

sys.modules.pop("podcast_transformer", None)
sys.modules.pop("podcast_transformer.cli", None)

from podcast_transformer import cli

cli = importlib.reload(cli)


@pytest.fixture(autouse=True)
def _ensure_openai_removed() -> None:
    sys.modules.pop("openai", None)


def test_generate_translation_summary_calls_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.delenv("AZURE_OPENAI_SUMMARY_API_VERSION", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT", raising=False)

    segments = [
        {"start": 0.0, "end": 3.2, "speaker": "Speaker 1", "text": "Hello world"}
    ]

    captured: Dict[str, Any] = {}

    def fake_create(**kwargs: Any) -> Dict[str, Any]:
        captured.update(kwargs)
        return {
            "choices": [
                {"message": {"content": "翻译摘要"}},
            ]
        }

    class FakeCompletions:
        def create(self, **kwargs: Any) -> Dict[str, Any]:
            return fake_create(**kwargs)

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeAzureClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.chat = FakeChat()

    fake_openai = ModuleType("openai")
    fake_openai.AzureOpenAI = FakeAzureClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    monkeypatch.setattr(
        cli,
        "_fetch_video_metadata",
        lambda _url: {
            "title": "Demo Title",
            "webpage_url": "https://example.invalid/watch?v=demo",
            "upload_date": "20240102",
        },
    )

    result = cli.generate_translation_summary(
        segments, "https://example.invalid/watch?v=demo"
    )

    summary = result["summary_markdown"]
    timeline = result["timeline_markdown"]

    assert summary.startswith("# ")
    assert "翻译摘要" in summary
    assert "标题：Demo Title" in summary
    assert "预估阅读时长" in summary
    assert "## 时间轴" in timeline
    assert "Demo Title" in timeline
    assert result["metadata"]["publish_date"] == "2024-01-02"
    assert result["total_words"] > 0
    assert result["estimated_minutes"] >= 1
    assert captured["model"] == "llab-gpt-5"
    messages = captured["messages"]
    assert isinstance(messages, list)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == cli.SUMMARY_PROMPT
    assert messages[1]["role"] == "user"
    assert "Hello world" in messages[1]["content"]
    assert "00:00:00.000" in messages[1]["content"]


def test_run_with_azure_summary_outputs_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    segments = [
        {"start": 0.0, "end": 1.0, "text": "Hello", "speaker": "Speaker 1"}
    ]

    monkeypatch.setenv("PODCAST_TRANSFORMER_CACHE_DIR", str(tmp_path))

    monkeypatch.setattr(
        cli,
        "fetch_transcript_with_metadata",
        lambda *args, **kwargs: [dict(segment) for segment in segments],
    )
    fake_bundle = {
        "summary_markdown": "# Summary\n\n内容",
        "timeline_markdown": (
            "# Timeline\n\n| 序号 | 起始 | 结束 | 时长 | 说话人 | 文本 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| 1 | 00:00:00.000 | 00:00:01.000 | 00:00:01.000 | Speaker 1 | Hello |"
        ),
        "metadata": {"title": "Demo"},
        "total_words": 2,
        "estimated_minutes": 1,
    }

    monkeypatch.setattr(
        cli,
        "generate_translation_summary",
        lambda _segments, _url: fake_bundle,
    )

    exit_code = cli.run([
        "--url",
        "https://youtu.be/testid",
        "--azure-summary",
    ])

    assert exit_code == 0
    output = capsys.readouterr().out.strip()
    data = json.loads(output)
    assert data["summary"] == fake_bundle["summary_markdown"]
    assert data["timeline"] == fake_bundle["timeline_markdown"]
    assert data["segments"][0]["text"] == "Hello"
    summary_path = Path(data["summary_path"])
    timeline_path = Path(data["timeline_path"])
    assert summary_path.exists()
    assert timeline_path.exists()
    assert summary_path.read_text(encoding="utf-8") == fake_bundle["summary_markdown"]
    assert timeline_path.read_text(encoding="utf-8") == fake_bundle["timeline_markdown"]
    assert data["total_words"] == fake_bundle["total_words"]
    assert data["estimated_minutes"] == fake_bundle["estimated_minutes"]
