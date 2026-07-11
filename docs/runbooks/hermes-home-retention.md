# Hermes Home Retention & Rotation

`~/.hermes` on the conductor Mac grew to 15.6 GiB with no retention: `state.db`
5.1 GiB, dispatch `lane-workdirs/` 2.1 GiB (all leaked — every one older than
14 days), dispatch `logs/` 2.4 GiB across orgs, `cron/output/` ~257k tiny files
(1.2 GiB, inode pressure), and the qwen-ops profile home mirroring the same
shapes. The ai-org storage automation deliberately treats `~/.hermes` as
report-only / owner-item, so retention is owned here, in hermes itself.

This runbook defines the policy. The implementation is
`neoengine_local/hermes_home_retention.py` (pure stdlib, dry-run by default).

## Retention domains are disjoint by root

This job's root is `~/.hermes` (override: `--root`). It refuses targets that
resolve outside its root and skips symlinks — the same escape guard as
ai-org's `scripts/qwen-ops-evidence-retention.py`. It never reaches into the
ai-org workspace; the ai-org tool never reaches into `~/.hermes`.

## Protected paths (never candidates, hard-coded)

- `state/org-evidence/**` — append-only verified evidence ledger (org
  evidence fabric). Never read, never written.
- Registries and runtime state: `state/*/agent-work-registry.json*`,
  `cron/jobs.json`, `kanban.db*`, `state.db` FILES themselves (row retention
  only, below), `bin/`, `scripts/`, `skills/`, `state/config-snapshots/`.
- Any receipt-class file inside a target lane is archived before deletion,
  never silently dropped (see per-lane rules).
- Everything not explicitly listed as a lane target. Targeting is
  allowlist-only; there is no "sweep everything else" mode.

## Lanes

| Lane | Target | Eligibility | Action |
|---|---|---|---|
| `dispatch-logs` | `state/*-agent-dispatch/logs/*` + `logs/` | mtime > 14d AND outside newest 25 per dir | archive then delete (`*.log` bulk class; `*.md` finals + `*receipt*` receipt class) |
| `lane-workdirs` | `state/*-agent-dispatch/lane-workdirs/<lane>/` | dir mtime > 24h grace AND registry does not report the lane alive AND no live process references the lane id/workdir (best-effort `ps` env sweep, `HERMES_LANE_MARKER` pattern) | archive `launch-receipt.json` + root `*.json`/`*.md` + git status/diff/HEAD snapshot (receipt class), then remove dir |
| `cron-output` | `cron/output/<job>/*` | mtime > 14d AND outside newest 25 per job dir | archive (bulk class) then delete; rmdir emptied job dirs |
| `state-db` | rows in `state.db`, `sessions/state.db`, `state/hermes/state.db` | ended sessions older than 30d, plus crashed "open" sessions with no message inside the window | export sessions summary (receipt class, `system_prompt` stripped) + messages (bulk class) to gzipped JSONL, batch-delete (FTS follows via triggers), null orphaned `parent_session_id`, clear expired `compression_locks`, checkpoint WAL, VACUUM when reclaimable > threshold AND free disk > 2.2x DB size |
| `profile-caches` | `lsp/`, `profiles/*/{lsp,cache}`, `profiles/*/home/.npm*`, `profiles/*/home/.cache` | mtime > 14d | delete without archive (cache class, re-derivable) |
| `profiles` | `profiles/<name>/` | recursion | `cron-output`, `state-db`, `logs` lanes applied per profile home |
| `archive-gc` | `retention-archive/*.tar.gz`/`.jsonl.gz` | bulk/cache-class archives older than 90d (by receipt) | delete archive + receipt; receipt-class never |

Windows and keep-min match the ai-org evidence-retention defaults (14 days,
keep-min 25 newest per directory) except where a lane has a stricter contract
(lane-workdirs use the ratified 24h lane-worktree standing authority, gated on
lane liveness; state.db uses 30d row retention).

Additionally, `cron/jobs.py:save_job_output` now rotates at write time:
each job keeps its newest `HERMES_CRON_OUTPUT_KEEP` (default 200) run
outputs, so `cron/output` growth is bounded at the source and the sweep only
handles pre-existing history and profiles.

### Why not the existing `maybe_auto_prune_and_vacuum`?

`hermes_state.py` already ships session pruning (90d default), but it is
startup-triggered — a long-lived gateway rarely restarts, and the live DB
predates any 90d cutoff, so it never fired here. It also cannot see profile
DBs or non-DB lanes. The retention tool uses guarded direct SQL (checks
tables exist, tolerates schema drift between the installed hermes and this
checkout) with the same eligibility semantics as `prune_sessions`, plus
export-before-delete.

## Archive-before-delete

Archive root: `~/.hermes/retention-archive/` (inside the job's own root).

- Layout mirrors ai-org: `<slug>-<YYYYMMDDTHHMMSSZ>.tar.gz` + sibling
  `<slug>-<runstamp>.receipt.json` with
  `{created_utc, target, file_count, byte_count, oldest_mtime, newest_mtime,
  mode, archive, class}`.
- **Receipt class** (indefinite retention): `launch-receipt.json`,
  `*receipt*.json`, and the dirty-workdir snapshot (`git status --porcelain` +
  `git diff` captured at removal time). These archives are never pruned by
  this job.
- **Bulk class** (bounded retention): raw logs, cron run outputs, exported
  message JSONL, clean-workdir file listings. Bulk archives are pruned by this
  same job after 90 days — this is what keeps the archive itself from
  becoming the next 15 GiB.
- Restore: `tar -xzf <archive> -C ~/.hermes` (arcnames are root-relative).

Nothing is ever deleted without either (a) an archive containing it, or
(b) for bulk re-derivable data in `--delete` mode, an explicit operator flag —
`--delete` is never the default and never applies to receipt-class files.

## Lane-workdir cleanup at lane end

The dispatch heartbeats that own lane lifecycle are live scripts in
`~/.hermes/scripts/` (unversioned, concurrently self-modifying), so the
durable mechanism is a registry-driven sweep, not an inline hook: a workdir is
removable only when the work registry no longer lists the lane as active AND
the `HERMES_LANE_MARKER` process sweep (same liveness test the stall reaper
uses) finds no live process, AND the 24h grace has passed. A lane that crashed
without cleanup is therefore still collected — fail-safe, not fail-open.

## state.db retention + VACUUM

- Eligible sessions: ended (`ended_at`/`end_reason` set) with reference time
  older than `--db-max-age-days` (default 30), plus crashed "open" sessions
  started before the cutoff with no message inside the window. On the 2026-07
  data this covers 94% of over-window messages; genuinely live sessions have
  recent messages and are never touched.
- Before deletion: sessions rows are exported (minus `system_prompt` — bulk
  prompt text, not accounting) to a **receipt-class** gzipped JSONL kept
  indefinitely, preserving token/burn accounting; messages are exported to a
  **bulk-class** gzipped JSONL (90d archive retention).
- Deletion runs in batches of 500 session ids with `busy_timeout=10000`; the
  FTS shadow tables follow via the existing delete triggers; children of
  pruned parents get `parent_session_id` nulled (mirrors `prune_sessions`);
  expired `compression_locks` rows are cleared when the table exists.
- After deletion: `PRAGMA wal_checkpoint(TRUNCATE)`, then `VACUUM` only when
  estimated reclaimable bytes exceed `--vacuum-threshold-bytes` (default
  256 MiB). VACUUM needs up to 2x DB size free on disk; the job checks free
  space first and skips (with a report line) when insufficient. SQLITE_BUSY
  aborts the VACUUM cleanly; rows already deleted stay deleted and the next
  run retries.
- The live gateway keeps writing during row deletion (WAL); VACUUM is the
  only exclusive step and fails soft.

## Safety envelope

- Dry-run by default; `--execute` required to mutate; `--check` mutates
  nothing and exits 1 over thresholds (default 10k files / 500 MiB per lane,
  plus state.db size cap 2 GiB).
- Kill switches: `HERMES_HOME_RETENTION_DISABLED=1` disables everything;
  per-lane `HERMES_HOME_RETENTION_<LANE>_DISABLED=1` (LANE in DISPATCH_LOGS,
  LANE_WORKDIRS, CRON_OUTPUT, STATE_DB, PROFILE_CACHES, PROFILES,
  ARCHIVE_GC). Write-time cron rotation: `HERMES_CRON_OUTPUT_KEEP=0`
  disables.
- Symlinks never followed or collected; targets resolving outside root are
  dropped; receipt-class files are archived before any deletion path.
- Archive integrity gates deletion: archives are written tmp→fsync→rename
  with a readback verification, and ONLY files proven present in the
  verified archive are unlinked. Slug/collision-safe naming (dots kept,
  `-N` suffix on collision). A failed or partial archive means the sources
  are kept, with a report note.
- Deletion re-checks at the last instant: a file replaced with fresh
  content or a symlink since the scan is skipped; a lane workdir whose
  mtime or registry state changed since eligibility is kept ("revived").
- Fail-safe liveness: an unreadable registry, or an unavailable `ps`
  sweep, means "assume alive". Dirty lane workdirs (uncommitted OR
  untracked work) are archived as full working-tree content (minus
  `.git`), because diffs cannot reproduce untracked files.
- state.db deletions revalidate inside the delete transaction: a session
  that gained an in-window message after selection is kept. Exports must
  succeed durably (fsync + rename) before any row is deleted.
  `PRAGMA foreign_keys=ON` so ON DELETE CASCADE references (telegram
  bindings) follow. Residual accepted risk: VACUUM briefly holds an
  exclusive lock against the live gateway; it fails soft on SQLITE_BUSY
  and the checkpoint-busy result is surfaced as a report note.
- The kill switches also stop write-time cron rotation
  (`HERMES_HOME_RETENTION_DISABLED` / `..._CRON_OUTPUT_DISABLED`), so one
  env var halts all retention deletion in an incident. Rotation refuses
  job dirs that resolve outside `cron/output` (hand-edited traversal job
  ids) and sorts by mtime, not filename.
- Every execute run writes a run receipt to
  `~/.hermes/state/hermes-home-retention/runs/<runstamp>.json` summarizing
  per-lane plan/actions/bytes — the job's own actions are themselves
  evidenced.

## Timer + alerts (operator-gated install)

- launchd templates in `neoengine_local/launchd/`:
  `ai.hermes.home-retention-check.plist` (daily 09:45 `--check`) and
  `ai.hermes.home-retention-execute.plist` (Sunday 10:15 `--execute`), both
  driving `scripts/hermes-home-retention-cron.sh`. Replace `__HERMES_REPO__`
  with the repo checkout path, copy to `~/Library/LaunchAgents`, then
  `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<plist>`. Install
  is a documented one-time operator step — the repo never self-installs
  timers.
- Over-threshold `--check` writes a handoff JSON
  (`schema: hermes.home_retention_alert.v1`, same field shape as
  `qwen_ops.evidence_retention_alert.v1`) into
  `$HERMES_RETENTION_HANDOFF_DIR` (default
  `~/.hermes/state/hermes-home-retention/handoffs/`). Point it at the qwen-ops
  conductor sidecar handoffs dir for conductor drain visibility. The wrapper
  always exits 0 — the handoff IS the alert.

## Operator cadence

1. Daily (timer): `--check` health gate.
2. Weekly (timer or manual): `--execute` in archive mode.
3. Never run `--execute --delete` unattended.
4. First-run expectation on 2026-07 data: lane-workdirs −2.1 GiB, dispatch
   logs −2.2 GiB, cron/output −1.0 GiB and ~190k inodes, state.db −2.5 to
   −3 GiB after VACUUM.
