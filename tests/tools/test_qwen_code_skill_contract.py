from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QWEN_SKILL = ROOT / "skills" / "autonomous-ai-agents" / "qwen-code" / "SKILL.md"


def test_qwen_headless_tool_approval_contract_is_documented() -> None:
    text = QWEN_SKILL.read_text(encoding="utf-8")

    assert "--approval-mode yolo" in text
    assert '"approvalMode": "yolo"' in text
    assert "Do **not** combine it with `-y`/`--yolo`" in text
    assert "Verify tool execution, not just exit code" in text
    assert "sentinel file" in text
    assert "qwen3.5:9b" in text


def test_qwen_headless_examples_do_not_use_legacy_y_flag() -> None:
    text = QWEN_SKILL.read_text(encoding="utf-8")

    bad_invocations = re.findall(r"(?m)^\s*qwen\b.*(?:\s-y\b|--yolo\b).*", text)
    assert bad_invocations == []
