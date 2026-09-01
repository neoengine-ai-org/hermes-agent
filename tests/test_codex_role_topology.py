from __future__ import annotations

from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / ".codex" / "config.toml"
CUSTOM_PATH = ROOT / ".codex" / "agents" / "custom.toml"
WORKER_PATH = ROOT / ".codex" / "agents" / "hermes-worker.toml"
DOC_PATH = ROOT / "docs" / "operations" / "codex-role-topology-v1.md"

ALLOWED_ROLE_KEYS = {"name", "description", "developer_instructions", "features"}
FORBIDDEN_ROLE_KEYS = {
    "model",
    "reasoning_effort",
    "approval_policy",
    "sandbox",
    "sandbox_mode",
    "filesystem",
    "filesystem_class",
    "writable_roots",
    "network",
    "network_access",
    "permission_profile",
}


def load_toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def assert_permission_neutral(role_text: str, *, expected_name: str) -> None:
    role = tomllib.loads(role_text)
    assert set(role) == ALLOWED_ROLE_KEYS
    assert role["name"] == expected_name
    assert role["features"] == {"request_permissions_tool": False}
    assert not (set(role) & FORBIDDEN_ROLE_KEYS)


def test_registered_roles_are_exact_and_disjoint() -> None:
    config = load_toml(CONFIG_PATH)
    assert set(config) == {"agents"}
    agents = config["agents"]
    assert set(agents) == {"custom", "hermes_worker"}
    assert agents["custom"] == {
        "description": "Hermes tracked-thread runtime bootstrap role; never performs implementation work",
        "config_file": "agents/custom.toml",
    }
    assert agents["hermes_worker"]["config_file"] == "agents/hermes-worker.toml"
    assert "agent_type=hermes_worker" in agents["hermes_worker"]["description"]
    assert agents["custom"]["config_file"] != agents["hermes_worker"]["config_file"]


def test_roles_are_model_and_permission_neutral() -> None:
    assert_permission_neutral(
        CUSTOM_PATH.read_text(encoding="utf-8"), expected_name="custom"
    )
    assert_permission_neutral(
        WORKER_PATH.read_text(encoding="utf-8"), expected_name="hermes_worker"
    )


def test_tracked_bootstrap_protocol_is_zero_effect_then_one_read() -> None:
    instructions = load_toml(CUSTOM_PATH)["developer_instructions"]
    for marker in (
        "purpose=TRACKED_THREAD_BOOTSTRAP_AND_RECOVERY",
        "substantive_first_turn_work=false",
        "DESKTOP_CREATE_THREAD_SAME_CWD",
        "DESKTOP_CREATE_THREAD_CROSS_CWD",
        "RUNTIME_BOOTSTRAP_RECEIPT_ONLY",
        "commands=0",
        "TRACKED_CHILD_SECOND_TURN_RUNTIME_VERIFIED",
        "CUSTOM_AGENT_ROLE_ROUTE_MISMATCH",
        "HOST_OWNED_APPROVAL_STALL",
        "Delegation-tool approval is separate from child active-turn runtime",
        "APP_SERVER_EXPLICIT_THREAD is PLAN_ONLY",
        "Existing workers are not retroactively changed",
    ):
        assert marker in instructions


def test_native_worker_requires_explicit_role_and_model_binding() -> None:
    instructions = load_toml(WORKER_PATH)["developer_instructions"]
    for marker in (
        "purpose=NATIVE_IMPLEMENTATION_AND_SOURCE_CONVERGENCE",
        "dispatch_route=NATIVE_SPAWN_AGENT",
        "explicit_implementation_model_required=true",
        "agent_type=hermes_worker",
        "trusted parent-side host events",
        "CUSTOM_AGENT_BINDING_MISMATCH",
        "HOST_OWNED_APPROVAL_STALL",
        "APP_SERVER_EXPLICIT_THREAD remains",
        "Existing workers are not retroactively changed",
    ):
        assert marker in instructions
    assert "must not emit RUNTIME_BOOTSTRAP_RECEIPT_ONLY" in instructions


def test_hostile_permission_and_role_pins_are_rejected() -> None:
    worker_text = WORKER_PATH.read_text(encoding="utf-8")
    bad_model = worker_text.replace(
        "\n[features]\n", '\nmodel = "review-only"\n\n[features]\n'
    )
    bad_sandbox = worker_text.replace(
        "\n[features]\n", '\n[sandbox]\nmode = "workspace-write"\n\n[features]\n'
    )
    bad_permission = worker_text.replace(
        "request_permissions_tool = false",
        "request_permissions_tool = true",
    )

    for hostile in (bad_model, bad_sandbox, bad_permission):
        try:
            assert_permission_neutral(hostile, expected_name="hermes_worker")
        except AssertionError:
            continue
        raise AssertionError("hostile role mutation was accepted")


def test_documented_source_and_nonclaims_are_bound() -> None:
    doc = DOC_PATH.read_text(encoding="utf-8")
    for marker in (
        "canonical_source_head=231d6f119fe8ddf5558e449099d330f2cdac8747",
        "canonical_protected_merge=7e68823504a6cc329ef4554362e1175e48cfae2b",
        "parent_controller=neoengine-ai-org/ai-org#554",
        "recipient_owner=Hermes PR owner (Issues are disabled)",
        "product_effect_mode=OFF",
        "runtime_admitted=false",
        "Worker self-report and repository",
        "SOURCE_ONLY",
    ):
        assert marker in doc
