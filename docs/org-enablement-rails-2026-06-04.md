# Org enablement rails — Hermes adoption

Command: `python -m pytest tests/gateway/test_gateway_run_facade_invariants.py`.

This slice adds a Python-compatible PolicyDecision schema fixture, repo-hygiene audit script, and the first behavior-preserving `gateway/run.py` seam: user-facing delivery redaction now lives in `gateway/runtime/delivery_redaction.py` while the old private facade remains.

Blocked example: changing Telegram/gateway delivery behavior while moving code, committing credentials/local state, or claiming production readiness.

Rollback: revert the seam module and restore the in-file redaction helper in `gateway/run.py`.

Non-claims: no launch readiness, production readiness, live-bank readiness, customer-data readiness, money-movement authority, protected approval, or mainline adoption claim.
