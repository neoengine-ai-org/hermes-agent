"""Fixed-command routing for qwen-ops Mac shared memory helpers."""
from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path

HELPER_DIR = Path(os.environ.get("ORG_MEMORY_HELPER_DIR", "/srv/conductors/bin"))
REMOTE_HELPER_HOST = os.environ.get("ORG_MEMORY_REMOTE_HELPER_HOST", "qwen-ops-01").strip()
REMOTE_HELPER_DIR = os.environ.get("ORG_MEMORY_REMOTE_HELPER_DIR", "/srv/conductors/bin").strip()
SCRIPT_TIMEOUT_SECONDS = 20
OUTPUT_LIMIT = 3500

_ALLOWED_LATEST = {"hourly", "cycle", "runner-health", "qwen-health", "inbox"}


def _compact(text: str) -> str:
    text = (text or "").strip()
    if len(text) > OUTPUT_LIMIT:
        return text[:OUTPUT_LIMIT] + "\nWARN memory-command output_truncated=true"
    return text


async def _run_helper(name: str, args: list[str] | None = None, stdin: str | None = None) -> str:
    helper = HELPER_DIR / name
    argv = [str(helper), *(args or [])]
    if not helper.exists() or not os.access(helper, os.X_OK):
        if not REMOTE_HELPER_HOST or not REMOTE_HELPER_DIR:
            return f"WARN {name.replace('mac-', '').replace('-', '-')} helper_missing=true path={helper}"
        remote_helper = f"{REMOTE_HELPER_DIR.rstrip('/')}/{name}"
        remote_cmd = " ".join(shlex.quote(part) for part in [remote_helper, *(args or [])])
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            REMOTE_HELPER_HOST,
            remote_cmd,
        ]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE if stdin is not None else None,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={
            "PATH": "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": os.environ.get("HOME", ""),
        },
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None),
            timeout=SCRIPT_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return f"FAIL {name.replace('mac-', '')} error=TIMEOUT"
    text = (out + err).decode(errors="replace")
    return _compact(text)


def parse_memory_write(raw_args: str) -> tuple[str, str]:
    raw = raw_args.strip()
    if not raw:
        return "qwen ops memory note", ""
    if "\n" in raw:
        first, body = raw.split("\n", 1)
        return (first.strip() or "qwen ops memory note", body.strip())
    # /memory-write <note> uses a generated safe title and exact body.
    words = raw.split()
    title = " ".join(words[:8]) or "qwen ops memory note"
    return title, raw


async def handle_memory_command(command: str, raw_args: str) -> str:
    command = command.replace("_", "-").lstrip("/")
    raw_args = raw_args.strip()
    if command == "memory-status":
        args = ["--ensure-dirs"] if raw_args == "--ensure-dirs" else []
        return await _run_helper("mac-memory-status", args)
    if command == "memory-search":
        if not raw_args:
            return "FAIL memory-search error=MISSING_TERM"
        return await _run_helper("mac-memory-search", ["--fixed", raw_args])
    if command == "memory-write":
        title, body = parse_memory_write(raw_args)
        if not body:
            return "FAIL memory-write-note error=MISSING_BODY"
        return await _run_helper("mac-memory-write-note", ["--stdin", title], stdin=body)
    if command == "memory-publish-drain":
        return await _run_helper("mac-memory-publish-drain")
    if command == "memory-publish-health":
        return await _run_helper("mac-memory-publish-health")
    if command == "memory-latest":
        typ = raw_args.split()[0] if raw_args else ""
        if typ not in _ALLOWED_LATEST:
            allowed = ",".join(sorted(_ALLOWED_LATEST))
            return f"FAIL memory-read-latest error=UNSUPPORTED_RECORD_TYPE allowed={allowed}"
        return await _run_helper("mac-memory-read-latest", [typ])
    return f"FAIL memory-command error=UNSUPPORTED_COMMAND command={shlex.quote(command)}"
