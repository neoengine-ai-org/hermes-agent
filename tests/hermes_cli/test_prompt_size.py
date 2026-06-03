"""Tests for the ``hermes prompt-size`` diagnostic (issue #34667)."""

import json

import pytest

from hermes_cli.prompt_size import (
    _SKILLS_BLOCK_RE,
    compute_prompt_breakdown,
    render_breakdown,
)


def _seed_memory(hermes_home, memory_text="", user_text=""):
    mem_dir = hermes_home / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    if memory_text:
        (mem_dir / "MEMORY.md").write_text(memory_text, encoding="utf-8")
    if user_text:
        (mem_dir / "USER.md").write_text(user_text, encoding="utf-8")


def _seed_skill(hermes_home, name, description):
    skill_dir = hermes_home / "skills" / "demo" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n# {name}\nbody\n",
        encoding="utf-8",
    )


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.chdir(tmp_path)  # avoid picking up the repo's AGENTS.md
    return hermes_home


def test_breakdown_keys_and_shape(isolated_home):
    """The breakdown exposes every documented key with int byte/char counts."""
    data = compute_prompt_breakdown("cli")
    assert set(data) >= {
        "platform",
        "model",
        "system_prompt",
        "skills_index",
        "memory",
        "user_profile",
        "tools",
        "sections",
    }
    assert data["platform"] == "cli"
    for key in ("system_prompt", "skills_index", "memory", "user_profile"):
        assert data[key]["bytes"] >= 0
        assert data[key]["chars"] >= 0
    assert data["tools"]["count"] >= 0
    assert data["tools"]["json_bytes"] >= 0
    # System prompt is non-trivial even with empty home (identity + guidance).
    assert data["system_prompt"]["bytes"] > 0


def test_runs_offline_without_credentials(isolated_home, monkeypatch):
    """No provider credentials configured → still produces a breakdown."""
    for var in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY",
                "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    data = compute_prompt_breakdown("cli")
    assert data["system_prompt"]["bytes"] > 0


def test_skills_index_reflects_installed_skills(isolated_home):
    """Installing a skill makes the skills-index block non-empty.

    Note: the skills prompt is cached per-process (in-process LRU + disk
    snapshot), so we seed the skill BEFORE the first build rather than
    comparing before/after within one process.
    """
    _seed_skill(isolated_home, "hello", "a demo skill for size testing")
    data = compute_prompt_breakdown("cli")
    assert data["skills_index"]["bytes"] > 0


def test_memory_and_profile_are_attributed(isolated_home):
    """Memory and user-profile blocks are measured separately."""
    _seed_memory(
        isolated_home,
        memory_text="Project uses pytest.\n",
        user_text="User is a developer.\n",
    )
    data = compute_prompt_breakdown("cli")
    assert data["memory"]["bytes"] > 0
    assert data["user_profile"]["bytes"] > 0


def test_skills_block_regex_matches_tagged_block():
    text = "preamble\n<available_skills>\n  cat:\n    - a: b\n</available_skills>\ntail"
    m = _SKILLS_BLOCK_RE.search(text)
    assert m is not None
    assert m.group(0).startswith("<available_skills>")
    assert m.group(0).endswith("</available_skills>")


def test_render_breakdown_is_plain_text(isolated_home):
    data = compute_prompt_breakdown("cli")
    out = render_breakdown(data)
    assert "System prompt total" in out
    assert "skills index" in out
    assert "Tool schemas" in out
    # Plain text — no JSON braces leaking in.
    assert not out.strip().startswith("{")


def test_json_serializable(isolated_home):
    data = compute_prompt_breakdown("cli")
    # Round-trips cleanly for ``--json`` output.
    assert json.loads(json.dumps(data)) == json.loads(json.dumps(data))


def test_compact_tool_schemas_strip_descriptions(isolated_home):
    """``tools.compact_schemas`` removes prose while preserving callable shapes."""
    (isolated_home / "config.yaml").write_text(
        "tools:\n  compact_schemas: true\n",
        encoding="utf-8",
    )
    from model_tools import _clear_tool_defs_cache
    from hermes_cli.prompt_size import _build_inspection_agent

    _clear_tool_defs_cache()
    agent = _build_inspection_agent("cli")
    tools_json = json.dumps(agent.tools, ensure_ascii=False)

    assert agent.tools
    assert '"description"' not in tools_json
    assert '"parameters"' in tools_json


def test_context_file_max_chars_is_configurable(isolated_home, tmp_path):
    """Large project rules can be capped aggressively for low-token profiles."""
    (isolated_home / "config.yaml").write_text(
        "agent:\n  context_file_max_chars: 1000\n",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text("A" * 5000, encoding="utf-8")

    from agent.prompt_builder import build_context_files_prompt

    prompt = build_context_files_prompt(cwd=str(tmp_path), skip_soul=True)

    assert "[...truncated AGENTS.md:" in prompt
    assert len(prompt) < 1500


def test_compact_skills_index_lists_names_only(isolated_home):
    """``skills.compact_index`` keeps discovery names and drops description prose."""
    _seed_skill(isolated_home, "alpha", "verbose description " * 20)
    _seed_skill(isolated_home, "beta", "another long description " * 20)
    (isolated_home / "config.yaml").write_text(
        "skills:\n  compact_index: true\n",
        encoding="utf-8",
    )

    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache

    clear_skills_system_prompt_cache(clear_snapshot=True)
    prompt = build_skills_system_prompt()

    assert "    - alpha\n" in prompt
    assert "    - beta\n" in prompt
    assert "verbose description" not in prompt
    assert "another long description" not in prompt


def test_compact_skills_index_reduces_prompt_size(isolated_home):
    """Name-only skill indexes are materially smaller for description-heavy libraries."""
    for idx in range(30):
        _seed_skill(isolated_home, f"skill-{idx}", "description token " * 50)

    from agent.prompt_builder import build_skills_system_prompt, clear_skills_system_prompt_cache

    clear_skills_system_prompt_cache(clear_snapshot=True)
    verbose_prompt = build_skills_system_prompt()
    (isolated_home / "config.yaml").write_text(
        "skills:\n  compact_index: true\n",
        encoding="utf-8",
    )
    clear_skills_system_prompt_cache(clear_snapshot=True)
    compact_prompt = build_skills_system_prompt()

    verbose_match = _SKILLS_BLOCK_RE.search(verbose_prompt)
    compact_match = _SKILLS_BLOCK_RE.search(compact_prompt)
    assert verbose_match is not None
    assert compact_match is not None
    verbose_block = verbose_match.group(0)
    compact_block = compact_match.group(0)

    assert len(compact_block.encode("utf-8")) < len(verbose_block.encode("utf-8")) / 2
    assert "skill-0" in compact_prompt


def test_compact_system_guidance_reduces_stable_prompt(isolated_home):
    """``agent.compact_system_guidance`` shortens always-on behavioral prose."""
    from agent.system_prompt import build_system_prompt_parts
    from hermes_cli.prompt_size import _build_inspection_agent

    verbose_agent = _build_inspection_agent("cli")
    verbose_stable = build_system_prompt_parts(verbose_agent)["stable"]

    (isolated_home / "config.yaml").write_text(
        "model:\n  default: gpt-5\nagent:\n  compact_system_guidance: true\n",
        encoding="utf-8",
    )
    compact_agent = _build_inspection_agent("cli")
    compact_stable = build_system_prompt_parts(compact_agent)["stable"]

    assert "# Tool-use discipline" in compact_stable
    assert "# Execution discipline" not in compact_stable
    assert len(compact_stable.encode("utf-8")) < len(verbose_stable.encode("utf-8"))
