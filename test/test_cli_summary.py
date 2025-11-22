"""Tests covering Azure GPT-5 translation summary workflow."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType
import types
from typing import Any, Dict

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

sys.modules.pop("any2summary", None)
sys.modules.pop("any2summary.cli", None)

from any2summary import cli

cli = importlib.reload(cli)


@pytest.fixture(autouse=True)
def _ensure_openai_removed() -> None:
    sys.modules.pop("openai", None)


def test_generate_translation_summary_calls_azure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT", "llab-gpt-5-pro")
    monkeypatch.delenv("AZURE_OPENAI_SUMMARY_API_VERSION", raising=False)
    monkeypatch.delenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT", raising=False)

    segments = [
        {"start": 0.0, "end": 3.2, "speaker": "Speaker 1", "text": "Hello world"}
    ]

    captured: Dict[str, Any] = {}
    requests: list[Dict[str, Any]] = []

    class FakeResponses:
        def create(self, **kwargs: Any):
            call_index = len(requests)
            requests.append(kwargs)
            if call_index == 0:
                return types.SimpleNamespace(
                    output=[
                        types.SimpleNamespace(
                            content=[types.SimpleNamespace(text="翻译摘要")]
                        )
                    ],
                    output_text="翻译摘要",
                )

            return types.SimpleNamespace(
                output=[
                    types.SimpleNamespace(
                        content=[types.SimpleNamespace(text="科技")]
                    )
                ],
                output_text="科技",
            )

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs
            self.responses = FakeResponses()

    fake_openai = ModuleType("openai")
    fake_openai.OpenAI = FakeClient  # type: ignore[attr-defined]
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

    expected_heading = "# 【科技】Demo Title-2024-M01"
    assert summary.splitlines()[0] == expected_heading
    assert "翻译摘要" in summary
    assert "标题：Demo Title" in summary
    assert "预估阅读时长" in summary
    assert timeline.splitlines()[0] == expected_heading
    assert "Demo Title" in timeline
    assert result["metadata"]["publish_date"] == "2024-01-02"
    assert result["metadata"]["domain"] == "科技"
    assert result["total_words"] > 0
    assert result["estimated_minutes"] >= 1
    assert result["file_base"] == "【科技】DemoTitle-2024-M01"
    assert captured["init_kwargs"]["base_url"] == "https://example.invalid/openai/v1"
    assert len(requests) == 2
    summary_request = requests[0]
    domain_request = requests[1]
    assert summary_request["model"] == "llab-gpt-5-pro"
    request_input = summary_request["input"]
    assert isinstance(request_input, list)
    system_msg = request_input[0]
    user_msg = request_input[1]
    assert system_msg["role"] == "system"
    assert system_msg["content"][0]["type"] == "input_text"
    assert system_msg["content"][0]["text"] == cli.SUMMARY_PROMPT
    assert user_msg["role"] == "user"
    assert user_msg["content"][0]["type"] == "input_text"
    assert "Hello world" in user_msg["content"][0]["text"]
    assert "00:00:00" in user_msg["content"][0]["text"]
    assert "## 欢迎交流与合作" in summary
    assert domain_request["model"] == "llab-gpt-5-pro"
    domain_input = domain_request["input"]
    assert isinstance(domain_input, list)
    assert domain_input[0]["content"][0]["text"] == cli.DOMAIN_PROMPT
    assert domain_input[1]["content"][0]["text"] == "翻译摘要"


def test_generate_translation_summary_infers_domain_via_azure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT", "llab-gpt-5-pro")
    segments = [
        {"start": 0.0, "end": 1.0, "speaker": "Speaker", "text": "Tech talk"}
    ]

    requests: list[Dict[str, Any]] = []

    class FakeResponses:
        def create(self, **kwargs: Any):
            call_index = len(requests)
            requests.append(kwargs)
            if call_index == 0:
                return types.SimpleNamespace(output_text="摘要内容")
            return types.SimpleNamespace(output_text="科技")

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.responses = FakeResponses()

    fake_openai = ModuleType("openai")
    fake_openai.OpenAI = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    monkeypatch.setattr(cli, "_fetch_video_metadata", lambda _url: {})

    result = cli.generate_translation_summary(
        segments, "https://example.invalid/watch?v=tech"
    )

    assert requests and len(requests) == 2
    domain_request = requests[-1]
    user_content = domain_request["input"][1]["content"][0]["text"]
    assert user_content == "摘要内容"
    assert domain_request["input"][0]["content"][0]["text"] == cli.DOMAIN_PROMPT
    assert "科技" in result["summary_markdown"]
    heading = result["summary_markdown"].splitlines()[0]
    assert heading.startswith("# 【科技】")
    assert result["metadata"]["domain"] == "科技"
    assert "raw_summary" not in domain_request


def test_generate_translation_summary_supports_legacy_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    monkeypatch.setenv("AZURE_OPENAI_SUMMARY_DEPLOYMENT", "llab-gpt-5")
    monkeypatch.delenv("AZURE_OPENAI_USE_RESPONSES", raising=False)

    segments = [
        {"start": 0.0, "end": 2.0, "speaker": "Speaker", "text": "Legacy"}
    ]

    captured: Dict[str, Any] = {}
    requests: list[Dict[str, Any]] = []

    class FakeCompletions:
        def create(self, **kwargs: Any) -> Dict[str, Any]:
            requests.append(kwargs)
            return {
                "choices": [
                    {"message": {"content": "旧版摘要"}},
                ]
            }

    class FakeChat:
        def __init__(self) -> None:
            self.completions = FakeCompletions()

    class FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            captured["init_kwargs"] = kwargs
            self.chat = FakeChat()

    fake_openai = ModuleType("openai")
    fake_openai.AzureOpenAI = FakeClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    result = cli.generate_translation_summary(
        segments, "https://example.invalid/watch?v=legacy"
    )

    assert len(requests) == 2
    summary_request = requests[0]
    domain_request = requests[1]
    assert summary_request["model"] == "llab-gpt-5"
    messages = summary_request["messages"]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert "Legacy" in messages[1]["content"]
    domain_messages = domain_request["messages"]
    assert domain_messages[0]["content"] == cli.DOMAIN_PROMPT
    assert domain_messages[1]["content"] == "旧版摘要"
    assert "旧版摘要" in result["summary_markdown"]


def test_run_with_azure_summary_outputs_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    segments = [
        {"start": 0.0, "end": 1.0, "text": "Hello", "speaker": "Speaker 1"}
    ]

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))

    monkeypatch.setattr(
        cli,
        "fetch_transcript_with_metadata",
        lambda *args, **kwargs: [dict(segment) for segment in segments],
    )
    fake_bundle = {
        "summary_markdown": "# 【Demo】Demo-2024-M01\n\n内容",
        "timeline_markdown": (
            "# 【Demo】Demo-2024-M01\n\n| 序号 | 起始 | 结束 | 时长 | 说话人 | 文本 |\n"
            "| --- | --- | --- | --- | --- | --- |\n"
            "| 1 | 00:00:00.000 | 00:00:01.000 | 00:00:01.000 | Speaker 1 | Hello |"
        ),
        "metadata": {"title": "Demo"},
        "total_words": 2,
        "estimated_minutes": 1,
        "file_base": "【Demo】Demo-2024-M01",
    }

    outbox_dir = tmp_path / "outbox"
    monkeypatch.setenv("ANY2SUMMARY_OUTBOX_DIR", str(outbox_dir))
    def fake_generate_summary(
        provided_segments: Any,
        url: str,
        prompt: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        return fake_bundle

    monkeypatch.setattr(
        cli,
        "generate_translation_summary",
        fake_generate_summary,
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
    assert summary_path.name.startswith("【Demo】Demo-2024-M01_summary")
    assert summary_path.suffix == ".md"
    assert timeline_path.name.startswith("【Demo】Demo-2024-M01_timeline")
    assert timeline_path.suffix == ".md"
    assert summary_path.read_text(encoding="utf-8") == fake_bundle["summary_markdown"]
    assert timeline_path.read_text(encoding="utf-8") == fake_bundle["timeline_markdown"]
    assert data["total_words"] == fake_bundle["total_words"]
    assert data["estimated_minutes"] == fake_bundle["estimated_minutes"]
    outbox_summary = data["summary_paths"].get("outbox_summary")
    assert outbox_summary
    outbox_path = Path(outbox_summary)
    assert outbox_path.exists()
    assert outbox_path.read_text(encoding="utf-8") == fake_bundle["summary_markdown"]
    assert "outbox_timeline" not in data["summary_paths"]


def test_run_with_custom_summary_prompt_file(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    segments = [
        {"start": 0.0, "end": 1.0, "text": "Hi", "speaker": "Speaker"}
    ]

    monkeypatch.setenv("ANY2SUMMARY_CACHE_DIR", str(tmp_path))

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("自定义系统提示", encoding="utf-8")

    monkeypatch.setattr(
        cli,
        "fetch_transcript_with_metadata",
        lambda *args, **kwargs: [dict(segment) for segment in segments],
    )

    fake_bundle = {
        "summary_markdown": "# 标题\n内容",
        "timeline_markdown": "# 标题\n时间线",
        "metadata": {"title": "Demo"},
        "total_words": 2,
        "estimated_minutes": 1,
        "file_base": "Demo",
    }

    captured_prompt: Dict[str, Any] = {}

    def fake_generate(
        provided_segments: Any,
        url: str,
        prompt: str | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        captured_prompt["prompt"] = prompt
        return fake_bundle

    monkeypatch.setattr(cli, "generate_translation_summary", fake_generate)

    exit_code = cli.run(
        [
            "--url",
            "https://youtu.be/testid",
            "--azure-summary",
            "--summary-prompt-file",
            str(prompt_file),
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out.strip()
    data = json.loads(output)
    assert data["summary"] == fake_bundle["summary_markdown"]
    assert captured_prompt["prompt"] == "自定义系统提示"


def test_write_summary_documents_copies_to_default_outbox_and_adds_suffix(monkeypatch, tmp_path):
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()

    default_outbox = tmp_path / "outbox"
    monkeypatch.setattr(cli, "DEFAULT_OUTBOX_DIR", str(default_outbox))
    monkeypatch.delenv("ANY2SUMMARY_OUTBOX_DIR", raising=False)
    monkeypatch.setattr(
        cli,
        "_resolve_video_cache_dir",
        lambda *_args, **_kwargs: str(cache_dir),
    )

    existing_summary = cache_dir / "demo_summary.md"
    existing_summary.write_text("旧摘要", encoding="utf-8")

    result = cli._write_summary_documents(
        "https://youtu.be/default",
        "# 摘要\n内容",
        "# 时间轴\n内容",
        "demo",
    )

    outbox_summary = result.get("outbox_summary")
    assert outbox_summary
    assert Path(outbox_summary).exists()
    assert Path(outbox_summary).parent == default_outbox
    summary_path = Path(result["summary"])
    assert summary_path.exists()
    assert summary_path.name.startswith("demo_summary")
    assert summary_path.suffix == ".md"
    timeline_path = Path(result["timeline"])
    assert timeline_path.exists()
    assert timeline_path.name.startswith("demo_timeline")
    assert timeline_path.suffix == ".md"
    assert "outbox_timeline" not in result
