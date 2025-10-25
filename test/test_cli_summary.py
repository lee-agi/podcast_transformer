"""Tests covering Azure GPT-5 translation summary workflow."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "podcast_transformer"
sys.path.insert(0, str(PACKAGE_ROOT))

from podcast_transformer import cli


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

    summary = cli.generate_translation_summary(segments)

    assert summary.startswith("# ")
    assert "## 时间轴" in summary
    assert "翻译摘要" in summary
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
    fake_markdown = (
        "# 封面\n\n"
        "## 时间轴\n"
        "| 序号 | 起始 | 结束 | 时长 | 说话人 | 文本 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| 1 | 00:00:00.000 | 00:00:01.000 | 00:00:01.000 | Speaker 1 | Hello |"
    )

    monkeypatch.setattr(cli, "generate_translation_summary", lambda _segments: fake_markdown)

    exit_code = cli.run([
        "--url",
        "https://youtu.be/testid",
        "--azure-summary",
    ])

    assert exit_code == 0
    output = capsys.readouterr().out.strip()
    data = json.loads(output)
    assert data["summary"] == fake_markdown
    assert data["segments"][0]["text"] == "Hello"
    summary_path = data.get("summary_path")
    assert summary_path

    path_obj = Path(summary_path)
    assert path_obj.exists()
    assert path_obj.read_text(encoding="utf-8") == fake_markdown
