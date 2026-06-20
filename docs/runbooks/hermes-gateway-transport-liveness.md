# Hermes Gateway Transport Liveness

Use this runbook when a gateway process is running but conductors are silent.
This incident class is process-alive / transport-dead: the Hermes process can
remain up while Telegram polling has paused after repeated reconnect failures.
Liveness codes are `HERMES_TELEGRAM_PAUSED` (auto-paused after reconnect
failures) and `HERMES_TELEGRAM_STALE` (no successful poll within the stale
threshold despite the transport reporting connected).

## First Response

1. Check Hermes behavior-level health first:

   ```bash
   hermes gateway status
   hermes doctor
   ```

2. Confirm the gateway process is alive (transport-dead vs process-dead is the
   discriminator here):

   ```bash
   hermes gateway pid
   ```

   A non-empty PID with a red `hermes gateway status` is the signature of this
   incident class — the process is up but the Telegram transport is paused or
   stale.

3. Check the gateway log for the transport pause signature:

   ```bash
   tail -n 120 "${HERMES_HOME:-$HOME/.hermes}/logs/gateway.log"
   ```

   Classify this line as `HERMES_TELEGRAM_PAUSED`:

   ```text
   Telegram paused after 10 consecutive reconnect failures
   ```

4. Ask the supervised recovery path what it would do.

   Default profile:

   ```bash
   hermes gateway recover --dry-run
   ```

   Named profile:

   ```bash
   hermes --profile qwen-ops-runner-conductor gateway recover --dry-run
   ```

   A recoverable decision must name only the affected Hermes profile and must
   not mention Qwen, Ollama, Postgres, NeoEngine API, Cockpit, the tunnel, or
   unrelated Hermes profiles as restart targets.

5. Let the supervisor restart only the affected Hermes gateway profile when
   the dry-run decision is `restart_profile`.

   Default profile:

   ```bash
   hermes gateway recover
   ```

   Named profile:

   ```bash
   hermes --profile qwen-ops-runner-conductor gateway recover
   ```

   The supervisor enforces the per-profile cooldown: max 3 supervised restarts
   in 15 minutes. If it returns `operator_intervention_required`, stop here and
   investigate the underlying Telegram/network/auth failure.

6. If supervised recovery is unavailable, restart only the affected Hermes
   gateway profile manually.

   Default profile (no profile flag):

   ```bash
   hermes gateway restart
   ```

   Named profile (qwen-ops-runner-conductor or any other named profile):

   ```bash
   hermes --profile qwen-ops-runner-conductor gateway restart
   ```

   The two commands are distinct on purpose — the supervisor decides which
   profile is affected based on the `gateway_state.json` it owns, then
   emits the matching command shape. Default profile uses the bare
   command; named profiles use `--profile <name>` so operators reading the
   log can tell which slot the supervisor touched.

7. Prove recovery:

   ```bash
   tail -n 80 "${HERMES_HOME:-$HOME/.hermes}/logs/gateway.log" | grep "Connected to Telegram (polling mode)"
   hermes -z "Reply exactly: ok"
   hermes --profile qwen-ops-runner-conductor -z "Reply exactly: ok"
   ```

   Either oneshot succeeding is the post-restart verification signal. The
   pre-restart oneshot fails or times out because the transport is paused;
   the post-restart oneshot succeeds because polling resumed.

## Do Not Chase First

Do not start with Qwen, Ollama, Postgres, NeoEngine API, Cockpit, or the tunnel
unless Hermes transport liveness is clean. Qwen tunnel health is separate from
Hermes Telegram polling health. The supervisor is **profile-scoped** — it will
only restart the affected Hermes profile and will not touch Qwen, Ollama,
Postgres, the NeoEngine API, or any unrelated Hermes profile.

## Supervised Recovery Decision

The supervisor uses `gateway.recovery.evaluate_gateway_recovery(...)` to turn a
transport-liveness verdict into a bounded action:

- Liveness healthy → `no_action`.
- `HERMES_TELEGRAM_PAUSED` or `HERMES_TELEGRAM_STALE` and the profile is under
  the cooldown bound → `restart_profile`; restart only the affected Hermes
  profile via the profile-aware command shape above.
- Profile has hit the cooldown bound → no restart; the decision returns
  `operator_intervention_required` and the runbook routes to a human.

### Cooldown / Flap Guard

Maximum **3 restarts in 15 minutes** per profile. After the third in-window
restart, the supervisor stops restarting and reports
`operator_intervention_required` until enough of the in-window restart history
ages out. The bound is per-profile — a saturated qwen-ops-runner-conductor
profile does not block recovery on the default profile, and vice versa.

When the supervisor stops restarting, treat it as a hard escalation. The
underlying transport failure is probably not a transient reconnect — common
root causes:

- Bot token revoked or rotated; Telegram returns 401 forever.
- Network partition between the host and `api.telegram.org`.
- Another poller (different `HERMES_HOME`, container, or laptop) is holding
  the long-poll session, causing the local profile to get kicked.

In every case, the fix is a human action (rotate token, restore connectivity,
identify and stop the rogue poller) — looping restarts will not heal it.

If the supervisor returns `recovery_state_corrupt`, inspect and move aside the
cooldown ledger before retrying supervised recovery:

```bash
RECOVERY_STATE="${HERMES_HOME:-$HOME/.hermes}/gateway_recovery_state.json"
python -m json.tool "$RECOVERY_STATE"
mv "$RECOVERY_STATE" "$RECOVERY_STATE.corrupt.$(date +%Y%m%d%H%M%S)"
hermes gateway recover --dry-run
```

For a named profile, let Hermes resolve the profile home; do not guess an
ad-hoc `HERMES_HOME`. Confirm the profile-scoped gateway state, inspect the
ledger under that profile's real home, then retry the profile-scoped dry-run:

```bash
hermes --profile qwen-ops-runner-conductor gateway status
hermes --profile qwen-ops-runner-conductor gateway recover --dry-run
```

### Watchdog Rule

Restart Hermes only when either condition is true:

- `gateway_state.json` reports `platforms.telegram.state = "paused"` or
  `platforms.telegram.transport_paused = true` (`HERMES_TELEGRAM_PAUSED`).
- `platforms.telegram.last_successful_poll_at` is older than the configured
  stale threshold while Telegram is reported as connected
  (`HERMES_TELEGRAM_STALE`).

The default stale threshold is 15 minutes. Keep it above the Telegram polling
heartbeat interval; `HERMES_TELEGRAM_POLLING_HEARTBEAT_SECONDS` is clamped to
30-600 seconds.

The repair is profile-scoped:

```bash
hermes gateway restart
```

or:

```bash
hermes --profile <profile> gateway restart
```

Leave Qwen tunnel, Ollama, Postgres, and NeoEngine API untouched unless their
own independent checks fail after Hermes transport health is clean.

## Incident Pattern (reference)

Original incident:

- Telegram reconnect failures accumulated on the qwen-ops-runner-conductor
  profile until polling auto-paused.
- Qwen tunnel was healthy throughout — no need to bounce.
- `hermes -z` oneshot failed against the affected profile (transport dead).
- Profile-scoped `hermes --profile qwen-ops-runner-conductor gateway restart`
  resumed polling.
- Post-restart `hermes -z` oneshot succeeded; conductor traffic resumed.

The supervisor's job is to make that exact sequence happen automatically while
the cooldown/flap guard prevents it from masking a deeper failure.
