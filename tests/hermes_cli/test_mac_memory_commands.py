from __future__ import annotations

import os
from pathlib import Path

import pytest

from hermes_cli import mac_memory_commands


def test_parse_memory_write_uses_generated_title_for_single_line() -> None:
    title, body = mac_memory_commands.parse_memory_write("record bounded qwen ops drain evidence")

    assert title == "record bounded qwen ops drain evidence"
    assert body == "record bounded qwen ops drain evidence"


def test_parse_memory_write_splits_multiline_title_and_body() -> None:
    title, body = mac_memory_commands.parse_memory_write("Title here\nBody line one\nBody line two")

    assert title == "Title here"
    assert body == "Body line one\nBody line two"


@pytest.mark.asyncio
async def test_memory_latest_rejects_unsupported_record_type() -> None:
    result = await mac_memory_commands.handle_memory_command("memory-latest", "../../secrets")

    assert result.startswith("FAIL memory-read-latest error=UNSUPPORTED_RECORD_TYPE")
    assert "hourly" in result


@pytest.mark.asyncio
async def test_memory_search_passes_fixed_arg_without_shell_expansion(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = tmp_path / "mac-memory-search"
    output = tmp_path / "argv.txt"
    helper.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        f"pathlib.Path({str(output)!r}).write_text(repr(sys.argv[1:]))\n"
        "print('ok')\n"
    )
    helper.chmod(0o755)
    monkeypatch.setattr(mac_memory_commands, "HELPER_DIR", tmp_path)

    result = await mac_memory_commands.handle_memory_command("memory-search", "alpha; echo should-not-run")

    assert result == "ok"
    assert output.read_text() == "['--fixed', 'alpha; echo should-not-run']"


@pytest.mark.asyncio
async def test_missing_local_helper_falls_back_to_qwen_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            captured["stdin"] = stdin
            return b"PASS remote helper\n", b""

        def kill(self):
            captured["killed"] = True

        async def wait(self):
            return 0

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(mac_memory_commands, "HELPER_DIR", tmp_path / "missing")
    monkeypatch.setattr(mac_memory_commands, "REMOTE_HELPER_HOST", "qwen-ops-01")
    monkeypatch.setattr(mac_memory_commands, "REMOTE_HELPER_DIR", "/srv/conductors/bin")
    monkeypatch.setattr(mac_memory_commands.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await mac_memory_commands.handle_memory_command("memory-search", "alpha; echo should-not-run")

    assert result == "PASS remote helper"
    argv = captured["argv"]
    assert argv[:5] == (
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
    )
    assert argv[5] == "qwen-ops-01"
    assert argv[6] == "/srv/conductors/bin/mac-memory-search --fixed 'alpha; echo should-not-run'"


@pytest.mark.asyncio
async def test_memory_write_sends_stdin_over_remote_ssh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeProc:
        returncode = 0

        async def communicate(self, stdin=None):
            captured["stdin"] = stdin
            return b"PASS note written\n", b""

        def kill(self):
            captured["killed"] = True

        async def wait(self):
            return 0

    async def fake_create_subprocess_exec(*argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr(mac_memory_commands, "HELPER_DIR", tmp_path / "missing")
    monkeypatch.setattr(mac_memory_commands, "REMOTE_HELPER_HOST", "qwen-ops-01")
    monkeypatch.setattr(mac_memory_commands, "REMOTE_HELPER_DIR", "/srv/conductors/bin")
    monkeypatch.setattr(mac_memory_commands.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)

    result = await mac_memory_commands.handle_memory_command("memory-write", "Title\nBody line")

    assert result == "PASS note written"
    assert captured["stdin"] == b"Body line"
    assert captured["argv"][6] == "/srv/conductors/bin/mac-memory-write-note --stdin Title"
