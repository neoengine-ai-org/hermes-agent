# Founder hourly watchdog scripts

Canonical repository copies for Hermes script-only founder hourly watchdogs.

Operational copies may run from `~/.hermes/scripts/`, but changes must land here
with tests before being treated as durable. These controllers run under the
Hermes cron script timeout, so every variable-duration subprocess path must use
an internal run budget below the scheduler cap and defer overflow work instead
of relying on the scheduler to kill the job.
