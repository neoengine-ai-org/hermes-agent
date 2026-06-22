#!/usr/bin/env python3
"""Guard NeoEngine Mac Postgres SSH host trust from drifting to retired hosts.

This script is intended for script-only Hermes cron jobs. It is deliberately
idempotent:

* discover accessible org repos that already use the NEOENGINE_CI_POSTGRES SSH
  bridge;
* keep their SSH host variable pointed at the approved replacement Mac host;
* refresh the GitHub Actions known_hosts secret from a live ED25519 ssh-keyscan;
* keep Qwen's user-level mac-memory SSH aliases pointed at the same approved
  host with strict host-key checking.

GitHub Actions secrets are write-only, so a scoped repo's known_hosts secret is
refreshed every run. The script prints only actionable drift/failure output;
healthy runs remain quiet and write a JSON receipt.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

REPO_VAR = "NEOENGINE_CI_POSTGRES_SSH_HOST"
KNOWN_SECRET = "NEOENGINE_CI_POSTGRES_SSH_KNOWN_HOSTS"
KEY_SECRET = "NEOENGINE_CI_POSTGRES_SSH_KEY"
DEFAULT_ORG_RE = r"^neoengine-ai-org$"


@dataclass(frozen=True)
class GuardConfig:
    approved_host: str = os.environ.get("NEOENGINE_APPROVED_MAC_POSTGRES_HOST", "192.168.5.136")
    approved_port: int = int(os.environ.get("NEOENGINE_APPROVED_MAC_POSTGRES_SSH_PORT", "22"))
    qwen_host: str = os.environ.get("NEOENGINE_QWEN_OPS_HOST", "qwen-ops-01")
    org_re: str = os.environ.get("NEOENGINE_MAC_TRUST_ORG_RE", DEFAULT_ORG_RE)
    state_file: Path = Path(os.environ.get("NEOENGINE_MAC_TRUST_STATE_FILE", str(Path.home() / ".hermes/state/mac-postgres-trust-guard/last-run.json")))


@dataclass
class GuardResult:
    status: str
    approved_host: str
    scoped_repos: list[str]
    events: list[str]
    failures: list[str]
    started_at: str
    finished_at: str

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": "mac-postgres-trust-guard-v1",
            "status": self.status,
            "approved_host": self.approved_host,
            "scoped_repos": self.scoped_repos,
            "events": self.events,
            "failures": self.failures,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def run(cmd: list[str], *, timeout: int = 30, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, input=input_text, text=True, capture_output=True, timeout=timeout, check=False)


def normalize_ed25519_keyscan(stdout: str, approved_host: str) -> str:
    """Return a known_hosts line pinned to approved_host from ssh-keyscan output."""
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "ssh-ed25519":
            return f"{approved_host} {parts[1]} {parts[2]}"
    raise RuntimeError("no ed25519 key found in ssh-keyscan output")


def keyscan(config: GuardConfig) -> str:
    cp = run(["ssh-keyscan", "-T", "5", "-p", str(config.approved_port), "-t", "ed25519", config.approved_host], timeout=10)
    if cp.returncode != 0 or not cp.stdout.strip():
        raise RuntimeError(f"ssh-keyscan failed for {config.approved_host}:{config.approved_port}: {(cp.stderr or cp.stdout).strip()}")
    return normalize_ed25519_keyscan(cp.stdout, config.approved_host)


def gh_json(args: list[str], *, timeout: int = 45) -> object:
    cp = run(["gh", *args], timeout=timeout)
    if cp.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {(cp.stderr or cp.stdout).strip()}")
    return json.loads(cp.stdout or "[]")


def gh_lines(args: list[str], *, timeout: int = 45) -> list[str]:
    cp = run(["gh", *args], timeout=timeout)
    if cp.returncode != 0:
        return []
    return cp.stdout.splitlines()


def parse_first_column(lines: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for line in lines:
        if line.strip():
            names.add(line.split()[0])
    return names


def repo_uses_postgres_bridge(variable_names: set[str], secret_names: set[str]) -> bool:
    """Scope guard writes to repos already carrying the bridge contract."""
    return REPO_VAR in variable_names or KNOWN_SECRET in secret_names or KEY_SECRET in secret_names


def discover_repos(config: GuardConfig) -> list[str]:
    org_allow = re.compile(config.org_re)
    repos: list[str] = []
    for org in gh_lines(["org", "list", "--limit", "100"], timeout=30):
        if not org_allow.search(org):
            continue
        payload = gh_json(["repo", "list", org, "--limit", "1000", "--json", "nameWithOwner"], timeout=60)
        if not isinstance(payload, list):
            raise RuntimeError(f"unexpected repo list payload for org {org}")
        repos.extend(sorted(str(row["nameWithOwner"]) for row in payload if isinstance(row, dict) and row.get("nameWithOwner")))
    return repos


def get_repo_var(repo: str, name: str) -> str | None:
    cp = run(["gh", "variable", "get", name, "--repo", repo], timeout=20)
    if cp.returncode != 0:
        return None
    return cp.stdout.strip()


def repo_scope(repo: str) -> bool:
    variables = parse_first_column(gh_lines(["variable", "list", "--repo", repo], timeout=30))
    secrets = parse_first_column(gh_lines(["secret", "list", "--repo", repo], timeout=30))
    return repo_uses_postgres_bridge(variables, secrets)


def enforce_repo(repo: str, hostkey: str, config: GuardConfig) -> list[str]:
    events: list[str] = []
    current = get_repo_var(repo, REPO_VAR)
    if current != config.approved_host:
        cp = run(["gh", "variable", "set", REPO_VAR, "--repo", repo, "--body", config.approved_host], timeout=30)
        if cp.returncode != 0:
            raise RuntimeError(f"failed setting {REPO_VAR} for {repo}: {(cp.stderr or cp.stdout).strip()}")
        events.append(f"{repo}:{REPO_VAR}:{current or 'MISSING'}->{config.approved_host}")

    cp = run(["gh", "secret", "set", KNOWN_SECRET, "--repo", repo], input_text=hostkey + "\n", timeout=45)
    if cp.returncode != 0:
        raise RuntimeError(f"failed refreshing {KNOWN_SECRET} for {repo}: {(cp.stderr or cp.stdout).strip()}")
    events.append(f"{repo}:{KNOWN_SECRET}:refreshed")
    return events


def qwen_alias_script() -> str:
    return r'''
set -euo pipefail
mkdir -p ~/.ssh
chmod 700 ~/.ssh
[ -f ~/.ssh/config ] && cp ~/.ssh/config ~/.ssh/config.bak.$(date -u +%Y%m%dT%H%M%SZ) || true
python3 - "$DESIRED_HOST" <<'PY'
from pathlib import Path
import sys
host = sys.argv[1]
p = Path.home() / '.ssh/config'
text = p.read_text() if p.exists() else ''
block = (
    f"Host mac-memory mac-memory-codex-pickup\n"
    f"    HostName {host}\n"
    f"    HostKeyAlias {host}\n"
    f"    User neoengine\n"
    f"    StrictHostKeyChecking yes\n"
    f"    UserKnownHostsFile ~/.ssh/known_hosts\n"
)
lines = text.splitlines()
out = []
skip = False
for line in lines:
    stripped = line.strip()
    if stripped.startswith('Host '):
        names = stripped.split()[1:]
        if any(n in {'mac-memory', 'mac-memory-codex-pickup'} for n in names):
            skip = True
            continue
        skip = False
    if not skip:
        out.append(line)
p.write_text(('\n'.join(out).rstrip() + '\n\n' + block).lstrip())
PY
chmod 600 ~/.ssh/config
ssh-keygen -R "$DESIRED_HOST" >/dev/null 2>&1 || true
ssh-keygen -R mac-memory >/dev/null 2>&1 || true
ssh-keygen -R mac-memory-codex-pickup >/dev/null 2>&1 || true
printf '%s\n' "$HOSTKEY" >> ~/.ssh/known_hosts
chmod 600 ~/.ssh/known_hosts
ssh -G mac-memory | awk '/^(hostname|hostkeyalias|userknownhostsfile|stricthostkeychecking|user) /{print}'
'''


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def ensure_qwen_alias(hostkey: str, config: GuardConfig) -> list[str]:
    probe = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", config.qwen_host, "hostname"], timeout=10)
    if probe.returncode != 0:
        return [f"qwen:{config.qwen_host}:UNREACHABLE:{(probe.stderr or probe.stdout).strip()[:160]}"]

    remote_env = f"DESIRED_HOST={shell_quote(config.approved_host)} HOSTKEY={shell_quote(hostkey)} "
    cp = run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6", config.qwen_host, remote_env + "bash -s"], input_text=qwen_alias_script(), timeout=30)
    if cp.returncode != 0:
        return [f"qwen:{config.qwen_host}:ALIAS_REPAIR_FAILED:{(cp.stderr or cp.stdout).strip()[:220]}"]
    if f"hostname {config.approved_host}" in cp.stdout and f"hostkeyalias {config.approved_host}" in cp.stdout:
        return [f"qwen:{config.qwen_host}:mac-memory:{config.approved_host}:verified"]
    return [f"qwen:{config.qwen_host}:ALIAS_VERIFY_UNCLEAR:{cp.stdout.strip()[:220]}"]


def actionable_events(events: Iterable[str], failures: Iterable[str], approved_host: str) -> list[str]:
    actionable = list(failures)
    for event in events:
        if any(token in event for token in ("UNREACHABLE", "FAILED", "UNCLEAR", f"->{approved_host}")):
            actionable.append(event)
    return actionable


def run_guard(config: GuardConfig) -> GuardResult:
    started = datetime.now(timezone.utc).isoformat()
    events: list[str] = []
    failures: list[str] = []
    scoped: list[str] = []
    try:
        hostkey = keyscan(config)
        for repo in discover_repos(config):
            try:
                if not repo_scope(repo):
                    continue
                scoped.append(repo)
                events.extend(enforce_repo(repo, hostkey, config))
            except Exception as exc:  # continue protecting other repos
                failures.append(f"{repo}:{exc}")
        events.extend(ensure_qwen_alias(hostkey, config))
    except Exception as exc:
        failures.append(str(exc))

    finished = datetime.now(timezone.utc).isoformat()
    return GuardResult(
        status="PASS" if not failures else "FAIL",
        approved_host=config.approved_host,
        scoped_repos=scoped,
        events=events,
        failures=failures,
        started_at=started,
        finished_at=finished,
    )


def render_actionable(result: GuardResult) -> str:
    if not actionable_events(result.events, result.failures, result.approved_host):
        return ""
    lines = [
        "## Mac Postgres SSH trust guard",
        f"Status: {result.status}",
        f"Approved host: {result.approved_host}",
        "Scoped repos: " + (", ".join(result.scoped_repos) if result.scoped_repos else "none"),
    ]
    if result.events:
        lines.append("Events:")
        lines.extend(f"- {event}" for event in result.events)
    if result.failures:
        lines.append("Failures:")
        lines.extend(f"- {failure}" for failure in result.failures)
    return "\n".join(lines) + "\n"


def main() -> int:
    config = GuardConfig()
    result = run_guard(config)
    config.state_file.parent.mkdir(parents=True, exist_ok=True)
    config.state_file.write_text(json.dumps(result.to_json(), indent=2, sort_keys=True) + "\n")
    output = render_actionable(result)
    if output:
        print(output, end="")
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
