#!/usr/bin/env python3
"""Script-only NeoEngine founder hourly watchdog.

Reads the shared cron-control file directly before emitting the blocker-state sentinel.
Fail closed if the shared control file/section is missing. This avoids LLM file-tool
misses while preserving the shared-control contract.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTROL = Path('/Users/neoengine/.hermes/cron/shared/neoengine-product-org-shared-cron-control.md')
SECTION = 'Job section: founder-hourly-watchdog / NeoEngine'
REPO = Path('/Users/neoengine/workspace/ai-org/neoengine')
STATE = Path('/Users/neoengine/.hermes/state/neoengine-hourly-watchdog-progress.json')
DIRTY_RECEIPT = Path('/Users/neoengine/.hermes/state/neoengine-dirty-files.json')
PROOF = Path('/Users/neoengine/.hermes/state/agent-runtime/neoengine-last-proof.json')
RUNTIME_PROOF_GATE = Path('/Users/neoengine/.hermes/state/agent-runtime/agent_runtime_proof_gate.py')
QWEN_HEARTBEATS = Path('/Users/neoengine/.hermes/state/cross-org-cto/heartbeats')
FRESHNESS_THRESHOLD_MINUTES = 15


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now() -> str:
    return utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    raw = value.strip()
    if raw.endswith('Z'):
        raw = raw[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(raw)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def age_minutes(value: Any) -> int | None:
    dt = parse_ts(value)
    if not dt:
        return None
    return max(0, int((utcnow() - dt).total_seconds() // 60))


def fail_closed(reason: str) -> int:
    print('NeoEngine founder-hourly-watchdog failed closed.\n')
    print('Blocker: ' + reason + '\n')
    print(f'Required file: `{CONTROL}`\n')
    print('Per the cron bootstrap contract, I did not invent fallback behavior or issue a normal NeoEngine status heartbeat from stale assumptions.')
    return 0  # human-facing advisory, not scheduler infrastructure failure


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return p.returncode, p.stdout.strip()
    except Exception as e:
        return 124, str(e)


def load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def fmt_delta(previous: Any, current: Any) -> str:
    if previous in (None, '') or current in (None, ''):
        return 'unknown'
    try:
        p = int(previous)
        c = int(current)
        if p == c:
            return 'unchanged'
        sign = '+' if c - p > 0 else ''
        return f'{p} → {c} ({sign}{c-p})'
    except Exception:
        return 'unchanged' if str(previous) == str(current) else f'{previous} → {current}'


def git_branch_head() -> tuple[str, str]:
    branch = head = 'unknown'
    if not REPO.is_dir():
        return branch, head
    rc, out = run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'], cwd=REPO, timeout=10)
    if rc == 0 and out:
        branch = out.splitlines()[-1].strip()
    rc, out = run(['git', 'rev-parse', '--short', 'HEAD'], cwd=REPO, timeout=10)
    if rc == 0 and out:
        head = out.splitlines()[-1].strip()
    return branch, head


def categorize_dirty(path: str) -> str:
    p = path.lower()
    generated_tokens = ('__pycache__/', '.pytest_cache/', 'node_modules/', 'dist/', 'build/', 'coverage/', '.next/', 'generated', '.lock', 'package-lock.json', 'pnpm-lock.yaml', 'yarn.lock')
    proof_tokens = ('test', 'tests/', 'spec', 'proof', 'receipt', 'evidence', 'validation', 'fixtures/', 'snapshots/')
    if any(tok in p for tok in generated_tokens):
        return 'generated'
    if any(tok in p for tok in proof_tokens):
        return 'test/proof files'
    return 'unknown'


def dirty_state() -> dict:
    entries: list[dict[str, str]] = []
    summary = {
        'tracked_modified': 0,
        'untracked': 0,
        'generated': 0,
        'test/proof files': 0,
        'unknown': 0,
    }
    if not REPO.is_dir():
        return {'dirty': 'unknown', 'count': 'unknown', 'summary': summary, 'entries': entries, 'error': 'repo_missing'}
    rc, out = run(['git', 'status', '--porcelain=v1'], cwd=REPO, timeout=15)
    if rc != 0:
        return {'dirty': 'unknown', 'count': 'unknown', 'summary': summary, 'entries': entries, 'error': f'git_status_failed rc={rc}', 'output': out[:1000]}
    for line in out.splitlines():
        if not line:
            continue
        status = line[:2]
        # Porcelain v1 uses two status columns followed by an optional space.
        # Use line[2:].lstrip() so `M path` and ` M path` do not lose the first filename byte.
        path = line[2:].lstrip() if len(line) > 2 else line
        if ' -> ' in path:
            path = path.split(' -> ', 1)[1]
        tracked = status != '??'
        if tracked:
            summary['tracked_modified'] += 1
        else:
            summary['untracked'] += 1
        category = categorize_dirty(path)
        summary[category] += 1
        entries.append({'status': status, 'path': path, 'category': category})
    receipt = {
        'updated_at': now(),
        'repo': str(REPO),
        'dirty': bool(entries),
        'count': len(entries),
        'summary': summary,
        'entries': entries,
        'top_dirty_files': [e['path'] for e in entries[:12]],
    }
    DIRTY_RECEIPT.parent.mkdir(parents=True, exist_ok=True)
    DIRTY_RECEIPT.write_text(json.dumps(receipt, indent=2, sort_keys=True) + '\n', encoding="utf-8")
    return receipt


def open_pr_count() -> str:
    if not REPO.is_dir():
        return 'unknown'
    rc, out = run(['gh', 'pr', 'list', '--repo', 'neoengine-ai-org/neoengine', '--state', 'open', '--json', 'number', '--limit', '100'], cwd=REPO, timeout=25)
    if rc != 0:
        return f'gh_failed rc={rc}'
    try:
        return str(len(json.loads(out or '[]')))
    except Exception:
        return 'parse_failed'




def qwen_observed_state(proof: dict) -> dict:
    """Return observed Qwen/Qwen-ops lanes without treating them as delivery proof.

    Qwen lanes are candidate/sidecar capacity. They should be visible in the
    founder watchdog, but not counted as Codex/Sonnet runtime-capacity proof or
    product-delivery/merge evidence.
    """
    observed: dict[str, dict[str, Any]] = {}
    for lane in proof.get('lanes') or []:
        lane_id = str(lane.get('lane_id') or '')
        family = str(lane.get('lane_family') or '').lower()
        provider = str(lane.get('provider') or '').lower()
        if 'qwen' not in ' '.join([lane_id.lower(), family, provider]):
            continue
        if lane.get('process_alive') and lane.get('process_command_matches') and lane.get('log_health') != 'FATAL_SIGNATURE':
            observed[lane_id or f"proof:{lane.get('pid')}"] = {
                'source': 'runtime_proof',
                'lane_id': lane_id or f"proof:{lane.get('pid')}",
                'status': 'live',
                'updated_at': lane.get('last_heartbeat_at'),
            }

    if QWEN_HEARTBEATS.is_dir():
        for path in QWEN_HEARTBEATS.glob('*.json'):
            obj = load_json(path)
            lane_id = str(obj.get('lane_id') or path.stem)
            if str(obj.get('org_id') or '').lower() != 'neoengine':
                continue
            if 'qwen' not in ' '.join([lane_id.lower(), str(obj.get('agent_type') or '').lower(), str(obj.get('lane_type') or '').lower()]):
                continue
            updated = obj.get('updated_at')
            age = age_minutes(updated)
            status = str(obj.get('status') or 'unknown')
            if status.lower() in {'working', 'active', 'registered'}:
                observed[lane_id] = {
                    'source': 'cross_org_heartbeat',
                    'lane_id': lane_id,
                    'status': status,
                    'updated_at': updated,
                    'age_minutes': age,
                    'fresh': age is not None and age <= FRESHNESS_THRESHOLD_MINUTES,
                    'evidence_path': obj.get('evidence_path'),
                    'not_merge_evidence': obj.get('not_merge_evidence', True),
                }

    rc, out = run(['pgrep', '-fl', 'qwen-ops-runner-conductor'], timeout=5)
    gateway_live = False
    if rc == 0 and out:
        for line in out.splitlines():
            text = line.lower()
            if 'gateway run' in text or 'hermes_cli.main' in text:
                gateway_live = True
                observed['qwen-ops-runner-conductor'] = {
                    'source': 'process',
                    'lane_id': 'qwen-ops-runner-conductor',
                    'status': 'live',
                    'updated_at': now(),
                    'process': line[:240],
                    'not_merge_evidence': True,
                }
                break
    live = [v for v in observed.values() if v.get('status') == 'live' or v.get('fresh') is True]
    stale = [v for v in observed.values() if v.get('fresh') is False]
    return {
        'observed': observed,
        'live_count': len(live),
        'stale_count': len(stale),
        'gateway_live': gateway_live,
        'summary': f"Qwen {len(live)} observed" + (f", {len(stale)} stale" if stale else ''),
    }

def closure_schema() -> dict:
    # This watchdog is not a product/PR closeout authority. It only reports a
    # proven closure if a closure receipt with the full evidence schema exists.
    # No authoritative closure-receipt source is consumed here, so fail closed.
    return {
        'proven': 0,
        'claimed_without_evidence': 0,
        'candidate_closures': 0,
        'blocked_by_missing_evidence': [],
        'evidence_required': ['closure_id', 'blocker_id', 'before_state', 'after_state', 'evidence_commit_or_pr', 'validation_commands', 'receipt_path', 'not_merge_evidence'],
    }


def main() -> int:
    if not CONTROL.is_file():
        return fail_closed('Hermes could not read the required single source of truth because the file is missing.')
    try:
        control_text = CONTROL.read_text(encoding="utf-8")
    except Exception as e:
        return fail_closed(f'Hermes could not read the required single source of truth: {e}')
    if SECTION not in control_text:
        return fail_closed(f'Hermes read the shared cron-control file, but the required section `{SECTION}` is missing.')

    previous = load_json(STATE)
    previous_snapshot = previous.get('snapshot') if isinstance(previous.get('snapshot'), dict) else {}

    proof_refresh = 'not_run'
    if RUNTIME_PROOF_GATE.is_file():
        rc, _out = run(['python3', str(RUNTIME_PROOF_GATE), 'neoengine', '--repair'], timeout=90)
        proof_refresh = 'ok' if rc == 0 else f'failed rc={rc}'

    proof = load_json(PROOF)
    proof_status = proof.get('proof_status') or proof.get('status') or 'UNKNOWN'
    checked_at = proof.get('checked_at') or proof.get('updated_at') or 'unknown'
    proof_age = age_minutes(checked_at)
    proof_fresh = proof_age is not None and proof_age <= FRESHNESS_THRESHOLD_MINUTES
    codex_live = proof.get('live_codex_build_lanes', '?')
    codex_req = proof.get('codex_required', '?')
    sonnet_live = proof.get('live_sonnet_build_lanes', '?')
    sonnet_req = proof.get('sonnet_required', '?')
    blockers = proof.get('remaining_blockers') or proof.get('blockers') or []
    qwen_state = qwen_observed_state(proof)

    branch, head = git_branch_head()
    dirty = dirty_state()
    dirty_count = dirty.get('count', 'unknown')
    open_prs = open_pr_count()
    closures = closure_schema()

    runtime_pass = proof_status in {'PASS', 'REPAIRED_PASS'} and proof_fresh and not blockers
    if not proof_fresh:
        status = 'DEGRADED_STALE_PROOF; blocker_movement=none_proven'
    elif proof_status in {'PASS', 'REPAIRED_PASS'}:
        status = 'RUNTIME_PASS; blocker_movement=none_proven'
    else:
        status = f'RUNTIME_{proof_status}; blocker_movement=none_proven'

    founder_blocker = 'none_declared'
    if not runtime_pass:
        reason = f'runtime proof is {proof_status}'
        if not proof_fresh:
            reason = f'stale runtime proof age={proof_age if proof_age is not None else "unknown"}m'
        if blockers:
            b = '; '.join(map(str, blockers[:3])) if isinstance(blockers, list) else str(blockers)
            reason = f'{reason}; blockers={b}'
        founder_blocker = f'unknown_runtime_decision_needed:{reason}'

    snapshot = {
        'proof_status': proof_status,
        'proof_fresh': proof_fresh,
        'codex_live': codex_live,
        'codex_req': codex_req,
        'sonnet_live': sonnet_live,
        'sonnet_req': sonnet_req,
        'qwen_live': qwen_state.get('live_count'),
        'qwen_stale': qwen_state.get('stale_count'),
        'qwen_gateway_live': qwen_state.get('gateway_live'),
        'blockers': blockers[:5] if isinstance(blockers, list) else blockers,
        'branch': branch,
        'head': head,
        'dirty_count': dirty_count,
        'open_prs': open_prs,
        'closures_proven': closures['proven'],
        'founder_blocker': founder_blocker,
    }
    material_digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, default=str).encode()).hexdigest()
    changed = 'none materially' if previous.get('digest') == material_digest else 'blocker-state sentinel refreshed; material receipt changed'
    rule = 'Runtime proof can prove the NeoEngine operating substrate is alive; it cannot prove product progress, PR readiness, merge readiness, blocker closure, CI repair, launch readiness, or production readiness.'
    state_doc = {
        'updated_at': now(),
        'digest': material_digest,
        'snapshot': snapshot,
        'runtime_receipt': str(PROOF),
        'dirty_receipt': str(DIRTY_RECEIPT),
        'control_file': str(CONTROL),
        'section': SECTION,
        'rule': rule,
    }
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state_doc, indent=2, sort_keys=True) + '\n', encoding="utf-8")

    prev_blockers = previous_snapshot.get('blockers') or []
    try:
        new_blockers = len(set(map(str, blockers)) - set(map(str, prev_blockers))) if isinstance(blockers, list) and isinstance(prev_blockers, list) else 0
    except Exception:
        new_blockers = 0
    stale_receipts = 0 if proof_fresh else 1

    age_text = f'{proof_age}m' if proof_age is not None else 'unknown'
    freshness_status = 'fresh' if proof_fresh else 'stale'
    dirty_text = 'dirty' if dirty.get('dirty') is True else ('clean' if dirty.get('dirty') is False else 'unknown')
    dirty_files_text = dirty_count
    clean_readiness = 'false' if dirty.get('dirty') else ('true' if dirty.get('dirty') is False else 'unknown')
    ds = dirty.get('summary', {})

    print('## NeoEngine watchdog')
    print(f'Status: {status}')
    print('Epoch: founder-hourly-watchdog / NeoEngine')
    print(f'Changed: {changed}')
    print('')
    print('Runtime health:')
    print('- watchdog substrate: repaired/refreshed')
    print('- shared cron-control: PASS')
    print(f'- active agents: Codex {codex_live}/{codex_req}, Sonnet {sonnet_live}/{sonnet_req}, {qwen_state.get("summary")}')
    print('- Qwen scope: candidate/sidecar observed only; not runtime-capacity, merge, delivery, or readiness evidence')
    print(f'- proof age: {age_text}')
    print(f'- freshness threshold: <= {FRESHNESS_THRESHOLD_MINUTES}m')
    print(f'- freshness status: {freshness_status} (proof `{checked_at}`, refresh {proof_refresh})')
    print('')
    print('Repo/worktree state:')
    print(f'- open PRs: {open_prs}')
    print(f'- active branch: `{branch}`')
    print(f'- head: `{head}`')
    print(f'- worktree: {dirty_text}')
    print(f'- dirty files: {dirty_files_text}')
    print('- dirty summary:')
    print(f'  - tracked_modified: {ds.get("tracked_modified", 0)}')
    print(f'  - untracked: {ds.get("untracked", 0)}')
    print(f'  - generated: {ds.get("generated", 0)}')
    print(f'  - test/proof files: {ds.get("test/proof files", 0)}')
    print(f'  - unknown: {ds.get("unknown", 0)}')
    print(f'- dirty proof: receipt `{DIRTY_RECEIPT}`')
    print(f'- clean-readiness: {clean_readiness}')
    print('')
    print('Blocker movement:')
    print(f'- closures proven this hour: {closures["proven"]}')
    print(f'- claimed_without_evidence: {closures["claimed_without_evidence"]}')
    print(f'- candidate closures: {closures["candidate_closures"]}')
    print(f'- founder blocker: {founder_blocker}')
    print('- product blocker movement: none_proven')
    print('')
    print('Delta since prior hour:')
    print(f'- open PRs: {fmt_delta(previous_snapshot.get("open_prs"), open_prs)}')
    print(f'- dirty files: {fmt_delta(previous_snapshot.get("dirty_count"), dirty_count)}')
    print(f'- agent lanes: Codex {fmt_delta(previous_snapshot.get("codex_live"), codex_live)}, Sonnet {fmt_delta(previous_snapshot.get("sonnet_live"), sonnet_live)}, Qwen {fmt_delta(previous_snapshot.get("qwen_live"), qwen_state.get("live_count"))}')
    print(f'- stale receipts: {stale_receipts}')
    print('')
    print('Next:')
    print('- keep NeoEngine runtime lanes proof-gated')
    print('- do not claim product closure from runtime proof')
    print('- PR/CI/dispatch controllers own merge pressure')
    print('- inspect dirty worktree before any readiness/closure claim')
    print(f'- progress receipt `{STATE}`')
    print('')
    print(rule)
    print('')
    print('not_merge_evidence=true')
    return 0


if __name__ == '__main__':
    sys.exit(main())
