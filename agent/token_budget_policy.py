"""Shared output-token budget policy for Hermes LLM calls.

The policy keeps routine turns compact by default while preserving explicit
caller budgets for tasks that genuinely need longer completions.
"""

from __future__ import annotations

import math
import os
from typing import Any, Iterable, Mapping, Optional

ROUTINE_LLM_MAX_TOKENS = 1024
STANDARD_LLM_MAX_TOKENS = 2048
EXPANDED_LLM_MAX_TOKENS = 3072
DEEP_LLM_MAX_TOKENS = 4096

_STANDARD_TERMS = (
    "tool",
    "json",
    "code",
    "patch",
    "diff",
    "test",
    "typescript",
    "python",
)
_EXPANDED_TERMS = (
    "review",
    "research",
    "architecture",
    "evidence",
    "verification",
    "debug",
    "incident",
    "root cause",
)
_DEEP_TERMS = (
    "security",
    "governance",
    "permission",
    "schema",
    "financial",
    "finance",
    "tax",
    "compliance",
    "adversarial",
    "doctrine",
    "cross-org",
    "money movement",
)


def _positive_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value):
            return None
    except Exception:
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _env_positive_int(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    try:
        return _positive_int(int(raw))
    except (TypeError, ValueError):
        return None


def _message_text(messages: Optional[Iterable[Mapping[str, Any]]]) -> str:
    if not messages:
        return ""
    parts: list[str] = []
    for message in messages:
        content = message.get("content") if isinstance(message, Mapping) else None
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, Mapping):
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str):
                        parts.append(text)
    return "\n".join(parts).lower()


def infer_llm_max_tokens(
    *,
    messages: Optional[Iterable[Mapping[str, Any]]] = None,
    prompt: str = "",
    task_type: str = "",
    has_tools: bool = False,
    reasoning_config: Optional[Mapping[str, Any]] = None,
    response_format: Any = None,
) -> int:
    """Infer an output budget for a call that did not set max_tokens."""

    text = "\n".join(
        part
        for part in (_message_text(messages), prompt.lower(), task_type.lower())
        if part
    )
    if any(term in text for term in _DEEP_TERMS):
        return DEEP_LLM_MAX_TOKENS
    if any(term in text for term in _EXPANDED_TERMS):
        return EXPANDED_LLM_MAX_TOKENS
    if (
        has_tools
        or response_format is not None
        or (reasoning_config and reasoning_config.get("enabled") is not False)
        or any(term in text for term in _STANDARD_TERMS)
    ):
        return STANDARD_LLM_MAX_TOKENS
    return _env_positive_int("HERMES_LLM_DEFAULT_MAX_TOKENS") or ROUTINE_LLM_MAX_TOKENS


def resolve_llm_max_tokens(
    explicit: Any = None,
    *,
    messages: Optional[Iterable[Mapping[str, Any]]] = None,
    prompt: str = "",
    task_type: str = "",
    has_tools: bool = False,
    reasoning_config: Optional[Mapping[str, Any]] = None,
    response_format: Any = None,
) -> int:
    """Resolve the final output-token budget.

    Explicit positive caller values are honored. Inferred budgets can be capped
    by ``HERMES_LLM_ESCALATION_MAX_TOKENS``; by default they cannot exceed the
    deep-work budget.
    """

    parsed = _positive_int(explicit)
    if parsed is not None:
        return parsed
    inferred = infer_llm_max_tokens(
        messages=messages,
        prompt=prompt,
        task_type=task_type,
        has_tools=has_tools,
        reasoning_config=reasoning_config,
        response_format=response_format,
    )
    cap = _env_positive_int("HERMES_LLM_ESCALATION_MAX_TOKENS") or DEEP_LLM_MAX_TOKENS
    return min(inferred, cap)
