from __future__ import annotations

from neoengine_local.runtime.provider_canary_repair import (
    classify_provider_canary_failure,
    maybe_repair_provider_canary_failure,
)


def test_classifies_homebrew_dylib_failure_without_overmatching_generic_errors():
    dyld = """
    dyld[12345]: Library not loaded: /opt/homebrew/opt/llhttp/lib/libllhttp.9.2.dylib
      Referenced from: /opt/homebrew/bin/node
      Reason: tried: '/opt/homebrew/opt/llhttp/lib/libllhttp.9.2.dylib' (no such file)
    """

    classified = classify_provider_canary_failure("codex", [dyld])

    assert classified["repairable"] is True
    assert classified["blocker_type"] == "homebrew_dylib_missing"
    assert classified["dependency"] == "llhttp"
    assert classify_provider_canary_failure("codex", ["401 unauthorized"])["repairable"] is False
    assert classify_provider_canary_failure("claude", [dyld])["repairable"] is False


def test_dynamic_repair_relinks_dependency_and_retries_canary_once():
    calls: list[list[str]] = []

    def runner(cmd: list[str], **_kwargs):
        calls.append(cmd)
        class Result:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return Result()

    attempts = {"count": 0}

    def retry_canary():
        attempts["count"] += 1
        return {"status": "PASS", "blockers": []}

    failure = {
        "status": "PROVIDER_CANARY_FAILED",
        "blockers": ["dyld: Library not loaded: /opt/homebrew/opt/llhttp/lib/libllhttp.9.2.dylib"],
    }

    repaired = maybe_repair_provider_canary_failure(
        "codex",
        failure,
        retry_canary,
        command_runner=runner,
        enabled=True,
    )

    assert repaired["status"] == "PASS"
    assert repaired["repair_attempted"] is True
    assert repaired["repair_status"] == "REPAIRED_PASS"
    assert attempts["count"] == 1
    assert ["brew", "link", "--overwrite", "llhttp"] in calls


def test_dynamic_repair_has_kill_switch_and_does_not_touch_unrepairable_failures():
    calls: list[list[str]] = []

    failure = {"status": "PROVIDER_CANARY_FAILED", "blockers": ["command not found: codex"]}

    repaired = maybe_repair_provider_canary_failure(
        "codex",
        failure,
        lambda: {"status": "PASS"},
        command_runner=lambda cmd, **kwargs: calls.append(cmd),
        enabled=False,
    )

    assert repaired["status"] == "PROVIDER_CANARY_FAILED"
    assert repaired["repair_attempted"] is False
    assert repaired["repair_status"] == "DISABLED"
    assert calls == []

    repaired = maybe_repair_provider_canary_failure(
        "codex",
        failure,
        lambda: {"status": "PASS"},
        command_runner=lambda cmd, **kwargs: calls.append(cmd),
        enabled=True,
    )

    assert repaired["repair_attempted"] is False
    assert repaired["repair_status"] == "NOT_REPAIRABLE"
    assert calls == []
