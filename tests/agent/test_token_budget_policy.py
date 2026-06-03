"""Tests for Hermes shared LLM output-token budget policy."""

from agent.token_budget_policy import (
    DEEP_LLM_MAX_TOKENS,
    EXPANDED_LLM_MAX_TOKENS,
    ROUTINE_LLM_MAX_TOKENS,
    STANDARD_LLM_MAX_TOKENS,
    resolve_llm_max_tokens,
)


def test_routine_default_is_compact(monkeypatch):
    monkeypatch.delenv("HERMES_LLM_DEFAULT_MAX_TOKENS", raising=False)
    assert resolve_llm_max_tokens(
        None,
        messages=[{"role": "user", "content": "Say hi"}],
    ) == ROUTINE_LLM_MAX_TOKENS


def test_explicit_positive_budget_wins():
    assert resolve_llm_max_tokens(
        8192,
        messages=[{"role": "user", "content": "Say hi"}],
    ) == 8192


def test_tools_escalate_to_standard_budget():
    assert resolve_llm_max_tokens(
        None,
        messages=[{"role": "user", "content": "Inspect this file"}],
        has_tools=True,
    ) == STANDARD_LLM_MAX_TOKENS


def test_review_escalates_to_expanded_budget():
    assert resolve_llm_max_tokens(
        None,
        messages=[{"role": "user", "content": "Review this patch with evidence"}],
    ) == EXPANDED_LLM_MAX_TOKENS


def test_governance_or_security_escalates_to_deep_budget():
    assert resolve_llm_max_tokens(
        None,
        messages=[{"role": "user", "content": "Security governance schema audit"}],
    ) == DEEP_LLM_MAX_TOKENS


def test_inferred_budget_respects_escalation_cap(monkeypatch):
    monkeypatch.setenv("HERMES_LLM_ESCALATION_MAX_TOKENS", "2048")
    assert resolve_llm_max_tokens(
        None,
        messages=[{"role": "user", "content": "Security governance schema audit"}],
    ) == STANDARD_LLM_MAX_TOKENS
