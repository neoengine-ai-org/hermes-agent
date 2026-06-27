from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
QWEN_SKILL = ROOT / "skills" / "autonomous-ai-agents" / "qwen-code" / "SKILL.md"
QWEN_PREFLIGHT = ROOT / "scripts" / "qwen_headless_tool_preflight.sh"


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


def test_bundled_qwen_preflight_requires_real_tool_sentinel() -> None:
    script = QWEN_PREFLIGHT.read_text(encoding="utf-8")

    assert "--approval-mode yolo" in script
    assert "qwen -y" not in script
    assert "qwen --yolo" not in script
    assert "QWEN_TOOL_PREFLIGHT_OK" in script
    assert "sentinel file was not created" in script
    assert "sentinel content mismatch" in script
    assert "QWEN_PREFLIGHT_PASS" in script
    assert "qwen3.5:9b" in script


def test_repo_qwen_headless_invocations_do_not_reintroduce_legacy_yolo_flags() -> None:
    scanned_roots = [ROOT / "skills", ROOT / "scripts"]
    bad_invocations: list[str] = []
    for scanned_root in scanned_roots:
        for path in scanned_root.rglob("*"):
            if not path.is_file() or path.suffix not in {".md", ".sh", ".py", ".txt"}:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for line_number, line in enumerate(text.splitlines(), start=1):
                if re.search(r"^\s*qwen\b.*(?:\s-y\b|--yolo\b)", line):
                    bad_invocations.append(f"{path.relative_to(ROOT)}:{line_number}:{line.strip()}")

    assert bad_invocations == []


def test_qwen_watchdog_display_contract_is_documented() -> None:
    text = QWEN_SKILL.read_text(encoding="utf-8")
    reference = (
        ROOT
        / "skills"
        / "autonomous-ai-agents"
        / "qwen-code"
        / "references"
        / "qwen-watchdog-display-evidence.md"
    )
    reference_text = reference.read_text(encoding="utf-8")

    assert "qwen-watchdog-display-evidence.md" in text
    assert "Qwen 1/1" in reference_text
    assert "qwen-ops-runner-conductor" in reference_text
    assert "qwen-ops-unified-pr-ci-controller.py" in reference_text
    assert "do not inflate" in reference_text
    assert "not product progress" in reference_text
