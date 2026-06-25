from __future__ import annotations

import argparse
import contextlib
import fnmatch
import json
import os
import unicodedata
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - POSIX file locks are required for file-backed claims.
    fcntl = None  # type: ignore[assignment]

VALID_WAKE_EVENTS = {
    "new_repair_packet",
    "pr_status_check_change",
    "review_request",
    "queue_priority_change",
    "stale_claim_expiry",
    "failed_ci_transition",
    "governance_unblock",
    "explicit_operator_command",
    "followup_repair_packet",
}

TERMINAL_CLAIM_STATUSES = {
    "completed",
    "blocked",
    "refused",
    "superseded",
    "no-op with evidence",
    "expired/recovered",
}

ACTIVE_CLAIM_STATUS = "active"


class DevLaneStore:
    """Durable file-backed heartbeat, claim, and pickup store for developer lanes.

    The store is deliberately small and JSON-only so cron/controller wrappers can
    use it as a restart-safe coordination layer without adding a new daemon or
    timer loop.  All mutating operations read current state, validate governance
    constraints, write atomically, and leave receipt pointers behind.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for child in ("heartbeats", "continuity", "claims", "work", "receipts"):
            (self.root / child).mkdir(parents=True, exist_ok=True)

    def path_for(self, relative: str | Path) -> Path:
        return self.root / relative

    def read_json(self, relative: str | Path, default: Any | None = None) -> Any:
        path = self.path_for(relative)
        if not path.exists():
            return [] if default is None else default
        return json.loads(path.read_text())

    def write_json(self, relative: str | Path, value: Any) -> Path:
        path = self.path_for(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, path)
        return path

    @contextlib.contextmanager
    def _exclusive_store_lock(self) -> Iterator[None]:
        if fcntl is None:
            raise RuntimeError("file-backed lane control requires POSIX fcntl locks; unsupported platform fails closed")
        lock_path = self.root / ".dev_lane_store.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def register_lane(self, lane_id: str, *, repo_scope: str, authorized_scopes: list[str]) -> dict[str, Any]:
        with self._exclusive_store_lock():
            lanes = self.read_json("lanes.json", {})
            lanes[lane_id] = {
                "lane_id": lane_id,
                "repo_scope": repo_scope,
                "authorized_scopes": authorized_scopes,
                "registered_at": lanes.get(lane_id, {}).get("registered_at") or utc_now(),
            }
            self.write_json("lanes.json", lanes)
            return lanes[lane_id]

    def _safe_storage_key(self, value: str, *, kind: str, allow_encoded: bool = False) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{kind} must be a string")
        normalized = unicodedata.normalize("NFC", value.strip())
        if (
            not normalized
            or normalized in {".", ".."}
            or normalized.startswith(".")
            or Path(normalized).is_absolute()
            or "/" in normalized
            or "\\" in normalized
            or ".." in normalized.split("/")
            or normalized != value.strip()
        ):
            raise ValueError(f"{kind} is unsafe for lane-control storage: {value!r}")
        safe = quote(normalized, safe="-_.")
        stem = safe.removesuffix(".json")
        reserved = {"history", "_history", "items", "lanes", "events", "receipts", "claims", "continuity", "heartbeats"}
        if not safe or safe.startswith(".") or stem in reserved:
            raise ValueError(f"{kind} is reserved or unsafe for lane-control storage: {value!r}")
        if safe != normalized:
            if not allow_encoded or not normalized.isascii():
                raise ValueError(f"{kind} is reserved or unsafe for lane-control storage: {value!r}")
            return safe
        if safe_id(normalized) != normalized:
            if not allow_encoded:
                raise ValueError(f"{kind} must be a stable safe slug: {value!r}")
            return safe
        return safe

    def _lane_path(self, dirname: str, lane_id: str) -> Path:
        safe_lane = self._safe_storage_key(lane_id, kind="lane_id")
        return Path(dirname) / f"{safe_lane}.json"

    def _claim_path(self, work_item_id: str) -> Path:
        safe_work_item = self._safe_storage_key(work_item_id, kind="work_item_id", allow_encoded=True)
        return Path("claims") / "by-work-item" / f"{safe_work_item}.json"

    def latest_heartbeat(self, lane_id: str) -> dict[str, Any] | None:
        value = self.read_json(self._lane_path("heartbeats", lane_id), None)
        return value

    def emit_heartbeat(
        self,
        lane_id: str,
        agent_session_id: str,
        repo_scope: str,
        current_state: str,
        claimed_work_item_id: str | None,
        last_successful_activity_at: str,
        next_eligible_wake_at: str,
        evidence_pointer: str,
        last_error_at: str | None = None,
        last_error_message: str | None = None,
        last_event_consumed: str | None = None,
    ) -> dict[str, Any]:
        heartbeat = {
            "schema_version": "dev_lane_heartbeat.v1",
            "lane_id": lane_id,
            "agent_session_id": agent_session_id,
            "repo_project_scope": repo_scope,
            "current_state": current_state,
            "claimed_work_item_id": claimed_work_item_id,
            "last_successful_activity_at": last_successful_activity_at,
            "last_error_at": last_error_at,
            "last_error_message": last_error_message,
            "next_eligible_wake_at": next_eligible_wake_at,
            "evidence_pointer": evidence_pointer,
            "last_event_consumed": last_event_consumed,
            "emitted_at": utc_now(),
        }
        with self._exclusive_store_lock():
            self.write_json(self._lane_path("heartbeats", lane_id), heartbeat)
        return heartbeat

    def write_continuity_packet(self, lane_id: str, packet: dict[str, Any]) -> dict[str, Any]:
        required = {
            "current_objective",
            "current_repo_branch_pr",
            "files_touched_or_planned",
            "active_blocker",
            "last_verified_command_check",
            "next_safe_action",
            "explicit_non_claims",
            "operator_approvals_relied_on",
        }
        missing = sorted(required - set(packet))
        if missing:
            raise ValueError(f"continuity packet missing required fields: {', '.join(missing)}")
        document = {"schema_version": "dev_lane_continuity.v1", "lane_id": lane_id, **packet, "updated_at": utc_now()}
        with self._exclusive_store_lock():
            self.write_json(self._lane_path("continuity", lane_id), document)
        return document

    def continuity_packet(self, lane_id: str) -> dict[str, Any] | None:
        return self.read_json(self._lane_path("continuity", lane_id), None)

    def active_claim_for_work(self, work_item_id: str, *, now: str) -> dict[str, Any] | None:
        claim = self.claim_for_work(work_item_id)
        if not claim or claim.get("claim_status") != ACTIVE_CLAIM_STATUS:
            return None
        if utc_parse(claim["claim_expires_at"]) <= utc_parse(now):
            return None
        return claim

    def _legacy_claim_path(self, work_item_id: str) -> Path:
        return Path("claims") / f"{quote(work_item_id, safe='-_.')}.json"

    def claim_for_work(self, work_item_id: str) -> dict[str, Any] | None:
        claim = None
        try:
            claim = self.read_json(self._claim_path(work_item_id), None)
        except ValueError:
            # Legacy claim files may exist for IDs that are no longer admitted
            # into the canonical namespaced claim path.  Read them only through
            # the bounded legacy percent-encoded path so they can be closed or
            # recovered without making new unsafe paths valid.
            claim = None
        legacy_claim = None
        legacy = self._legacy_claim_path(work_item_id)
        if (self.root / legacy).exists():
            legacy_candidate = self.read_json(legacy, None)
            if isinstance(legacy_candidate, dict):
                legacy_claim = legacy_candidate
        if isinstance(claim, dict) and claim.get("claim_status") == ACTIVE_CLAIM_STATUS:
            return claim
        if isinstance(legacy_claim, dict) and legacy_claim.get("claim_status") == ACTIVE_CLAIM_STATUS:
            return legacy_claim
        if isinstance(claim, dict):
            return claim
        return legacy_claim if isinstance(legacy_claim, dict) else None

    def _consume_events_through(self, item: dict[str, Any], boundary_event_id: str | None, consumed_at: str) -> None:
        if not boundary_event_id:
            return
        events = item.setdefault("events", [])
        boundary_index = next((idx for idx, event in enumerate(events) if event_id(event) == boundary_event_id), None)
        if boundary_index is None:
            return
        boundary_created_at = str(events[boundary_index].get("created_at", ""))
        for event in events:
            if event.get("event_type") in VALID_WAKE_EVENTS and str(event.get("created_at", "")) <= boundary_created_at:
                event.setdefault("consumed_at", consumed_at)

    def claim_work(
        self,
        work_item_id: str,
        lane_id: str,
        owner_session_id: str,
        now: str,
        ttl_seconds: int,
        evidence_path: str,
        recovery_evidence_path: str | None = None,
    ) -> dict[str, Any]:
        with self._exclusive_store_lock():
            current = self.claim_for_work(work_item_id)
            if current is None:
                self._safe_storage_key(work_item_id, kind="work_item_id", allow_encoded=True)
            now_dt = utc_parse(now)
            if current and current.get("claim_status") == ACTIVE_CLAIM_STATUS:
                expires = utc_parse(current["claim_expires_at"])
                if expires > now_dt:
                    raise ValueError(
                        f"active claim already exists for {work_item_id}: "
                        f"{current.get('claim_owner_session_id')} until {current.get('claim_expires_at')}"
                    )
                if not recovery_evidence_path:
                    raise ValueError(f"expired claim for {work_item_id} requires recovery_evidence_path")
                recovered = dict(current)
                recovered.update(
                    {
                        "claim_status": "expired/recovered",
                        "recovered_by_session_id": owner_session_id,
                        "recovered_at": now,
                        "recovery_evidence_path": recovery_evidence_path,
                    }
                )
                self._append_history(recovered)
                for path in self._claim_record_paths():
                    claim_at_path = self.read_json(str(path.relative_to(self.root)), {})
                    if (
                        claim_at_path.get("work_item_id") == work_item_id
                        and claim_at_path.get("claim_status") == ACTIVE_CLAIM_STATUS
                        and utc_parse(claim_at_path.get("claim_expires_at", "1970-01-01T00:00:00Z")) <= now_dt
                    ):
                        self.write_json(str(path.relative_to(self.root)), {**claim_at_path, **recovered})
                self._set_work_item_status(work_item_id, "open")
            items = self.read_json("work/items.json", {})
            item = items.get(work_item_id)
            if item is None:
                raise ValueError(f"work item does not exist for claim: {work_item_id}")
            lanes = self.read_json("lanes.json", {})
            lane = lanes.get(lane_id) or {
                "lane_id": lane_id,
                "repo_scope": item.get("repo_scope"),
                "authorized_scopes": item.get("authorized_scopes", ["**"]),
            }
            pickable, reason = self._is_item_pickable_for_lane(item, lane, now)
            if not pickable:
                raise ValueError(f"work item is not claimable: {reason}")
            claimed_event_id = latest_valid_event_id(item)
            expires_at = format_utc(now_dt + timedelta(seconds=ttl_seconds))
            claim = {
                "schema_version": "dev_lane_claim.v1",
                "work_item_id": work_item_id,
                "lane_id": lane_id,
                "claim_owner_session_id": owner_session_id,
                "claim_started_at": now,
                "claim_expires_at": expires_at,
                "claim_status": ACTIVE_CLAIM_STATUS,
                "claim_evidence_path": evidence_path,
                "claimed_event_id": claimed_event_id,
            }
            self._consume_events_through(item, claimed_event_id, now)
            items[work_item_id] = item
            self.write_json("work/items.json", items)
            self.write_json(self._claim_path(work_item_id), claim)
            self._set_work_item_status(work_item_id, "claimed")
            return claim

    def _set_work_item_status(self, work_item_id: str, status: str, *, blocked_at_event_id: str | None = None) -> None:
        items = self.read_json("work/items.json", {})
        item = items.get(work_item_id)
        if not item:
            return
        item["status"] = status
        if status == "blocked" and blocked_at_event_id is not None:
            item["blocked_at_event_id"] = blocked_at_event_id
        item["updated_at"] = utc_now()
        items[work_item_id] = item
        self.write_json("work/items.json", items)

    def close_claim(self, work_item_id: str, status: str, evidence_path: str, now: str, message: str | None = None) -> dict[str, Any]:
        if status not in TERMINAL_CLAIM_STATUSES:
            raise ValueError(f"invalid terminal claim status: {status}")
        with self._exclusive_store_lock():
            claim = self.claim_for_work(work_item_id)
            if not claim:
                raise ValueError(f"no claim exists for {work_item_id}")
            if claim.get("claim_status") != ACTIVE_CLAIM_STATUS:
                raise ValueError(f"claim is not active for {work_item_id}: {claim.get('claim_status')}")
            closed = dict(claim)
            closed.update({"claim_status": status, "closed_at": now, "closeout_evidence_path": evidence_path})
            if message:
                closed["closeout_message"] = message
            wrote_canonical = False
            try:
                self.write_json(self._claim_path(work_item_id), closed)
                wrote_canonical = True
            except ValueError:
                wrote_canonical = False
            legacy = self._legacy_claim_path(work_item_id)
            if (self.root / legacy).exists():
                self.write_json(legacy, closed)
            elif not wrote_canonical:
                raise ValueError(f"work_item_id is unsafe for canonical claim close and no legacy claim exists: {work_item_id!r}")
            self._append_history(closed)
            status_by_close = {
                "completed": "completed",
                "blocked": "blocked",
                "refused": "refused",
                "superseded": "superseded",
                "no-op with evidence": "completed",
                "expired/recovered": "open",
            }
            blocked_event_id = None
            if status_by_close[status] == "blocked":
                items = self.read_json("work/items.json", {})
                blocked_event_id = claim.get("claimed_event_id") or latest_valid_event_id_before(
                    items.get(work_item_id, {}), claim.get("claim_started_at")
                )
            self._set_work_item_status(
                work_item_id,
                status_by_close[status],
                blocked_at_event_id=blocked_event_id,
            )
            return closed

    def add_work_item(self, item: dict[str, Any]) -> dict[str, Any]:
        if "work_item_id" not in item:
            raise ValueError("work item missing work_item_id")
        self._safe_storage_key(str(item["work_item_id"]), kind="work_item_id", allow_encoded=True)
        with self._exclusive_store_lock():
            items = self.read_json("work/items.json", {})
            work_item_id = str(item["work_item_id"])
            existing = items.get(work_item_id)
            incoming = {"schema_version": "dev_lane_work_item.v1", **item}
            incoming.setdefault("events", [])
            for event in incoming.get("events", []):
                if event.get("event_type") not in VALID_WAKE_EVENTS:
                    raise ValueError(f"invalid wake event: {event.get('event_type')}")
            incoming.setdefault("status", "queued")
            incoming.setdefault("created_at", utc_now())
            if existing:
                merged_events = list(existing.get("events", []))
                seen = {event_id(event) for event in merged_events}
                blocked_boundary = existing.get("blocked_at_event_id") if existing.get("status") == "blocked" else None
                boundary_event = next((event for event in merged_events if event_id(event) == blocked_boundary), None)
                boundary_created_at = str(boundary_event.get("created_at", "")) if boundary_event else None
                for event in incoming.get("events", []):
                    if event_id(event) in seen:
                        continue
                    if blocked_boundary:
                        if boundary_event is None or not event_after_frontier(event, boundary_event):
                            continue
                    merged_events.append(event)
                    seen.add(event_id(event))
                incoming["events"] = merged_events
                incoming.setdefault("created_at", existing.get("created_at") or utc_now())
                if existing.get("blocked_at_event_id") and not incoming.get("blocked_at_event_id"):
                    incoming["blocked_at_event_id"] = existing.get("blocked_at_event_id")
                if existing.get("status") == "blocked":
                    latest_valid = latest_valid_event_id(incoming)
                    if not latest_valid or latest_valid == incoming.get("blocked_at_event_id"):
                        incoming["status"] = "blocked"
                active_claim = self.active_claim_for_work(work_item_id, now=utc_now())
                if active_claim:
                    incoming["status"] = "claimed"
                    incoming["active_claim_lane_id"] = active_claim.get("lane_id")
                    incoming["active_claim_owner_session_id"] = active_claim.get("claim_owner_session_id")
                    incoming["claim_expires_at"] = active_claim.get("claim_expires_at")
                elif existing.get("status") in {"completed", "superseded", "refused"}:
                    incoming["status"] = existing["status"]
            items[work_item_id] = incoming
            self.write_json("work/items.json", items)
            return incoming

    def record_event(self, work_item_id: str, event_type: str, created_at: str, event_id: str | None = None) -> dict[str, Any]:
        if event_type not in VALID_WAKE_EVENTS:
            raise ValueError(f"invalid wake event: {event_type}")
        with self._exclusive_store_lock():
            items = self.read_json("work/items.json", {})
            if work_item_id not in items:
                raise ValueError(f"unknown work item: {work_item_id}")
            item = items[work_item_id]
            boundary_id = item.get("blocked_at_event_id") if item.get("status") == "blocked" else None
            boundary_created_at = None
            if item.get("status") == "claimed":
                claim = self.claim_for_work(work_item_id)
                boundary_id = claim.get("claimed_event_id") if claim else boundary_id
                if claim and not boundary_id:
                    boundary_created_at = str(claim.get("claim_started_at", "")) or None
            if boundary_id:
                boundary = next((existing for existing in item.get("events", []) if globals()["event_id"](existing) == boundary_id), None)
                boundary_created_at = str(boundary.get("created_at", "")) if boundary else boundary_created_at
            event = {"event_id": event_id or f"{event_type}:{created_at}", "event_type": event_type, "created_at": created_at}
            if boundary_created_at is not None:
                if item.get("status") == "claimed" and claim and not boundary_id:
                    if str(created_at) <= boundary_created_at:
                        raise ValueError("event is older than blocked baseline or claim baseline")
                elif boundary and not event_after_frontier(event, boundary):
                    raise ValueError("event is older than blocked baseline or claim baseline")
            item.setdefault("events", []).append(event)
            self.write_json("work/items.json", items)
            return event

    def start_session(self, lane_id: str, session_id: str, now: str) -> dict[str, Any]:
        heartbeat = self.latest_heartbeat(lane_id)
        continuity = self.continuity_packet(lane_id)
        claim = None
        if heartbeat and heartbeat.get("claimed_work_item_id"):
            claim = self.claim_for_work(heartbeat["claimed_work_item_id"])
            if claim and claim.get("claim_status") == ACTIVE_CLAIM_STATUS and utc_parse(claim["claim_expires_at"]) > utc_parse(now):
                return {
                    "action": "resume",
                    "lane_id": lane_id,
                    "session_id": session_id,
                    "heartbeat": heartbeat,
                    "continuity_packet": continuity,
                    "claim": claim,
                }
        return self.pick_next_work(lane_id, session_id, now=now)

    def pick_next_work(self, lane_id: str, session_id: str, *, now: str) -> dict[str, Any]:
        lane = self.read_json("lanes.json", {}).get(lane_id)
        if not lane:
            raise ValueError(f"unknown lane: {lane_id}")
        items = list(self.read_json("work/items.json", {}).values())
        candidates: list[tuple[tuple[Any, ...], dict[str, Any], str]] = []
        skip_rows: list[dict[str, Any]] = []
        for item in items:
            allowed, reason = self._is_item_pickable_for_lane(item, lane, now)
            if not allowed:
                if reason in {"held_by_do_not_merge", "awaiting_human_review", "refused_without_sidecar_evidence"}:
                    skip_rows.append({"work_item_id": item["work_item_id"], "reason": reason, "skipped_at": now, "lane_id": lane_id})
                continue
            wake_reason = self._wake_reason(item, lane_id, now)
            if not wake_reason:
                continue
            candidates.append((self._rank_key(item), item, wake_reason))
        if skip_rows:
            with self._exclusive_store_lock():
                existing = self.read_json("receipts/skips.json", [])
                existing.extend(skip_rows)
                self.write_json("receipts/skips.json", existing)
        if not candidates:
            next_wake = format_utc(utc_parse(now) + timedelta(minutes=30))
            self.emit_heartbeat(
                lane_id,
                session_id,
                lane["repo_scope"],
                "idle-no-work",
                None,
                now,
                next_wake,
                "receipts/skips.json",
            )
            return {"action": "idle-no-work", "lane_id": lane_id, "next_eligible_wake_at": next_wake}
        _, item, wake_reason = sorted(candidates, key=lambda row: row[0])[0]
        evidence_path = str(self._claim_path(str(item["work_item_id"])))
        claim = self.claim_work(item["work_item_id"], lane_id, session_id, now, 3600, evidence_path)
        latest_event = latest_event_id(item)
        next_wake = format_utc(utc_parse(now) + timedelta(minutes=5))
        self.emit_heartbeat(
            lane_id,
            session_id,
            lane["repo_scope"],
            "working",
            item["work_item_id"],
            now,
            next_wake,
            evidence_path,
            last_event_consumed=latest_event,
        )
        return {
            "action": "claimed",
            "lane_id": lane_id,
            "work_item_id": item["work_item_id"],
            "claim": claim,
            "wake_reason": wake_reason,
            "next_eligible_wake_at": next_wake,
        }

    def status_report(self, *, now: str, stale_after_seconds: int = 1800) -> dict[str, Any]:
        lanes = self.read_json("lanes.json", {})
        rows = []
        for lane_id, lane in sorted(lanes.items()):
            hb = self.latest_heartbeat(lane_id)
            active_claims = self._active_claims_for_lane(lane_id, now)
            stale_claims = self._stale_claims_for_lane(lane_id, now)
            if not hb:
                status = "failed-heartbeat"
                freshness_seconds = None
            else:
                freshness_seconds = max(0, int((utc_parse(now) - utc_parse(hb["emitted_at"])).total_seconds()))
                if freshness_seconds > stale_after_seconds:
                    status = "failed-heartbeat"
                elif hb["current_state"] in {"working", "idle-no-work", "blocked", "awaiting-human", "refused-closeout"}:
                    status = hb["current_state"]
                elif stale_claims:
                    status = "stale"
                else:
                    status = hb["current_state"]
            rows.append(
                {
                    "lane_id": lane_id,
                    "repo_scope": lane.get("repo_scope"),
                    "status": status,
                    "heartbeat_freshness_seconds": freshness_seconds,
                    "active_claims": [c["work_item_id"] for c in active_claims],
                    "stale_claims": [c["work_item_id"] for c in stale_claims],
                    "next_scheduled_wake": hb.get("next_eligible_wake_at") if hb else None,
                    "last_event_consumed": hb.get("last_event_consumed") if hb else None,
                    "last_evidence_receipt": hb.get("evidence_pointer") if hb else None,
                }
            )
        return {"schema_version": "dev_lane_status_report.v1", "generated_at": now, "lanes": rows}

    def _append_history(self, claim: dict[str, Any]) -> None:
        history = self.read_json("claims/history.json", [])
        history.append(claim)
        self.write_json("claims/history.json", history)

    def _claim_record_paths(self) -> list[Path]:
        roots = [self.root / "claims", self.root / "claims" / "by-work-item"]
        paths: list[Path] = []
        for root in roots:
            if not root.exists():
                continue
            for path in root.glob("*.json"):
                if path.name == "history.json":
                    continue
                paths.append(path)
        return paths

    def _active_claims_for_lane(self, lane_id: str, now: str) -> list[dict[str, Any]]:
        claims = []
        for path in self._claim_record_paths():
            claim = json.loads(path.read_text())
            if claim.get("lane_id") == lane_id and claim.get("claim_status") == ACTIVE_CLAIM_STATUS and utc_parse(claim["claim_expires_at"]) > utc_parse(now):
                claims.append(claim)
        return claims

    def _stale_claims_for_lane(self, lane_id: str, now: str) -> list[dict[str, Any]]:
        claims = []
        for path in self._claim_record_paths():
            claim = json.loads(path.read_text())
            if claim.get("lane_id") == lane_id and claim.get("claim_status") == ACTIVE_CLAIM_STATUS and utc_parse(claim["claim_expires_at"]) <= utc_parse(now):
                claims.append(claim)
        return claims

    def _is_item_pickable_for_lane(self, item: dict[str, Any], lane: dict[str, Any], now: str) -> tuple[bool, str | None]:
        try:
            self._safe_storage_key(str(item.get("work_item_id", "")), kind="work_item_id", allow_encoded=True)
        except ValueError:
            return False, "unsafe_work_item_id"
        if item.get("repo_scope") != lane.get("repo_scope"):
            return False, "outside_repo_scope"
        if not scopes_overlap(lane.get("authorized_scopes", []), item.get("authorized_scopes", [])):
            return False, "outside_lane_capability"
        if self.active_claim_for_work(item["work_item_id"], now=now):
            return False, "already_actively_claimed"
        labels = {str(label).lower() for label in item.get("labels", [])}
        if "do-not-merge" in labels or "do_not_merge" in labels:
            return False, "held_by_do_not_merge"
        if item.get("awaiting_human_review"):
            return False, "awaiting_human_review"
        if item.get("status") == "refused" and not item.get("sidecar_evidence_path"):
            return False, "refused_without_sidecar_evidence"
        if item.get("status") == "blocked":
            latest_valid = latest_valid_event(item)
            if not latest_valid:
                return False, "blocked_without_new_event"
            blocked_boundary = item.get("blocked_at_event_id")
            boundary = next((event for event in item.get("events", []) if event_id(event) == blocked_boundary), None)
            if boundary and not event_after_frontier(latest_valid, boundary):
                return False, "blocked_without_new_event"
            if not boundary and blocked_boundary and event_id(latest_valid) == blocked_boundary:
                return False, "blocked_without_new_event"
        if item.get("status") in {"completed", "superseded"}:
            return False, "terminal"
        return True, None

    def _wake_reason(self, item: dict[str, Any], lane_id: str, now: str) -> str | None:
        hb = self.latest_heartbeat(lane_id)
        consumed = hb.get("last_event_consumed") if hb else None
        new_events = [event for event in item.get("events", []) if event_id(event) != consumed and not event.get("consumed_at")]
        valid_events = [event for event in new_events if event.get("event_type") in VALID_WAKE_EVENTS]
        if valid_events:
            event = sorted(valid_events, key=lambda e: e.get("created_at", ""), reverse=True)[0]
            return f"event:{event['event_type']}"
        if hb and utc_parse(now) >= utc_parse(hb.get("next_eligible_wake_at", "1970-01-01T00:00:00Z")):
            return "timer:fallback"
        if not hb:
            return "event:fresh_session"
        return None

    def _rank_key(self, item: dict[str, Any]) -> tuple[Any, ...]:
        operator_priority = item.get("operator_priority")
        if operator_priority is None:
            operator_priority = 1000
        created = item.get("created_at", "9999-01-01T00:00:00Z")
        return (
            operator_priority,
            0 if item.get("failed_required_ci") else 1,
            0 if item.get("stale_open_pr") else 1,
            0 if item.get("governance_blocker") else 1,
            0 if item.get("review_gap") else 1,
            0 if item.get("dependency_unblocked") else 1,
            created,
        )


def utc_now() -> str:
    return format_utc(datetime.now(timezone.utc))


def utc_parse(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)


def event_id(event: dict[str, Any]) -> str:
    return str(event.get("event_id") or f"{event.get('event_type')}:{event.get('created_at')}")


def event_after_frontier(event: dict[str, Any], boundary: dict[str, Any]) -> bool:
    event_created = str(event.get("created_at", ""))
    boundary_created = str(boundary.get("created_at", ""))
    if event_created != boundary_created:
        return event_created > boundary_created
    return event_id(event) > event_id(boundary)


def latest_event_id(item: dict[str, Any]) -> str | None:
    events = item.get("events", [])
    if not events:
        return None
    event = sorted(events, key=lambda e: (e.get("created_at", ""), event_id(e)), reverse=True)[0]
    return event_id(event)


def latest_valid_event(item: dict[str, Any]) -> dict[str, Any] | None:
    candidates = [event for event in item.get("events", []) if event.get("event_type") in VALID_WAKE_EVENTS]
    if not candidates:
        return None
    return sorted(candidates, key=lambda e: (e.get("created_at", ""), event_id(e)), reverse=True)[0]


def latest_valid_event_id(item: dict[str, Any]) -> str | None:
    event = latest_valid_event(item)
    return event_id(event) if event else None


def latest_valid_event_id_before(item: dict[str, Any], created_at: str | None) -> str | None:
    if not created_at:
        return None
    candidates = [
        event for event in item.get("events", [])
        if event.get("event_type") in VALID_WAKE_EVENTS and str(event.get("created_at", "")) < str(created_at)
    ]
    if not candidates:
        return None
    event = sorted(candidates, key=lambda e: (e.get("created_at", ""), event_id(e)), reverse=True)[0]
    return event_id(event)


def scopes_overlap(lane_scopes: list[str], item_scopes: list[str]) -> bool:
    if not lane_scopes or not item_scopes:
        return False
    for lane_scope in lane_scopes:
        for item_scope in item_scopes:
            if lane_scope == "**" or lane_scope == item_scope:
                return True
            if fnmatch.fnmatch(item_scope, lane_scope) or fnmatch.fnmatch(lane_scope, item_scope):
                return True
    return False


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Developer lane heartbeat and pickup control")
    parser.add_argument("--root", default=os.getenv("HERMES_DEV_LANE_STATE", str(Path.home() / ".hermes/state/dev-lane-heartbeat")))
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser("register-lane")
    reg.add_argument("lane_id")
    reg.add_argument("--repo-scope", required=True)
    reg.add_argument("--authorized-scope", action="append", required=True)

    hb = sub.add_parser("heartbeat")
    hb.add_argument("lane_id")
    hb.add_argument("--session", required=True)
    hb.add_argument("--repo-scope", required=True)
    hb.add_argument("--state", required=True)
    hb.add_argument("--claimed-work-item-id")
    hb.add_argument("--last-success", required=True)
    hb.add_argument("--next-wake", required=True)
    hb.add_argument("--evidence", required=True)
    hb.add_argument("--last-error-at")
    hb.add_argument("--last-error-message")

    pick = sub.add_parser("pickup")
    pick.add_argument("lane_id")
    pick.add_argument("--session", required=True)
    pick.add_argument("--now", default=utc_now())

    status = sub.add_parser("status")
    status.add_argument("--now", default=utc_now())
    status.add_argument("--stale-after-seconds", type=int, default=1800)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = DevLaneStore(args.root)
    if args.command == "register-lane":
        result = store.register_lane(args.lane_id, repo_scope=args.repo_scope, authorized_scopes=args.authorized_scope)
    elif args.command == "heartbeat":
        result = store.emit_heartbeat(
            args.lane_id,
            args.session,
            args.repo_scope,
            args.state,
            args.claimed_work_item_id,
            args.last_success,
            args.next_wake,
            args.evidence,
            args.last_error_at,
            args.last_error_message,
        )
    elif args.command == "pickup":
        result = store.pick_next_work(args.lane_id, args.session, now=args.now)
    elif args.command == "status":
        result = store.status_report(now=args.now, stale_after_seconds=args.stale_after_seconds)
    else:  # pragma: no cover
        raise AssertionError(args.command)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
