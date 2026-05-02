"""Pure helpers for the platform's capability_failure_learning surface.

The platform persists a memory card every time an installed tool fails, so
the next request from the same agent can route around the broken tool.
The DB write itself is platform-shaped (SQLAlchemy + ``AgentChatMemoryCard``
+ SAVEPOINT-style supersede), but the *decisions* feeding into the row are
pure functions over the execution outcome:

* "what kind of failure was this?" — :func:`failure_kind_from_execution`
* "how long should the avoidance last?" — :func:`learning_expiry_for_kind`
* "how aggressively should we avoid?" — :func:`learning_scores_for_kind`
* "what task family does this request belong to?" —
  :func:`infer_capability_task_family`
* "what's the human-readable advice text?" —
  :func:`build_learning_content` / :func:`build_system_prompt_overflow_content`
* "what does the dispatch result actually tell us?" —
  :func:`api_outcome_from_execution` / :func:`api_outcome_from_output` /
  :func:`last_tool_output_from_steps`
* "shorten this for storage" — :func:`clip_text`

This module ships those decision functions as pure Python so a publisher
can read exactly what triggers an avoidance, what the resulting memory card
says, and how long it lives.

Repository pattern (callback-injection, same as v0.3):
    The DB-bound entry points (``record_capability_failure_learning``,
    ``record_system_prompt_overflow_learning``,
    ``capability_learning_by_tool``, ``should_avoid_tool_for_request``)
    stay in the platform — they need a SQLAlchemy ``Session`` and a
    SAVEPOINT for the supersede + insert pair. They compose the pure
    helpers from this module and persist the result however they like.
    A future eval harness or CLI replay tool can compose the same helpers
    against a different store (in-memory list, JSON file, etc.) without
    pulling in any DB dependency.

Clock injection:
    :func:`learning_expiry_for_kind` requires ``now`` as a keyword
    argument so the function has no hidden time dependency. The platform
    passes ``utcnow()`` from its infrastructure layer; tests can pass a
    frozen instant for deterministic expiry assertions.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .types import ResolvedToolDefinition

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# memory_type column value used for every card this surface emits. Other
# memory_types (rolling_summary, structured_facts, etc.) live alongside
# but are never queried by the failure-learning code paths.
CAPABILITY_PREFERENCE_MEMORY_TYPE = "capability_preference"

# Tag included on every card so the platform's audit / diagnostics layer
# can grep for "everything written by this surface" without parsing the
# (richer, more variable) failure_kind / task_family tags.
CAPABILITY_LEARNING_TAG = "capability_failure_learning"

# Failure kind for the system-prompt-budget-overflow case (Bug A class).
# Recorded with tool=None and agent-scoped (not tool-scoped) so it never
# punishes individual tools — those would compound the overflow because
# fewer eligible tools means a larger residual prompt per remaining tool.
SYSTEM_PROMPT_OVERFLOW_KIND = "system_prompt_overflow"

# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------


def clip_text(value: Any, max_chars: int = 900) -> str:
    """Collapse whitespace and truncate ``value`` to ``max_chars``.

    Used for short storage previews (memory card content / metadata
    goal_preview) where the original may be a multi-line agent output and
    we want a single-line excerpt with an ellipsis tail. Idempotent for
    inputs already under the cap. Returns an empty string for ``None``
    or empty input.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return f"{text[:max_chars].rstrip()}..."


# ---------------------------------------------------------------------------
# Outcome classification (per-step / per-execution)
# ---------------------------------------------------------------------------


def api_outcome_from_output(output: dict[str, Any] | None) -> str:
    """Classify a single tool step's output as ``"success"`` or ``"out_of_coverage"``.

    Two coverage signals are checked:
      * ``output["fallback_used"] is True`` — the dispatch adapter
        returned its fallback path because the live API rejected the
        request (e.g. wrong language, unsupported region).
      * ``output["match_type"] in {"fallback", "identity"}`` — the
        scoring layer returned an identity / fallback match.

    Anything else (including missing / malformed output) classifies as
    ``"success"`` — outcome is conservative because flagging spurious
    failures would compound future avoidance.
    """
    if not isinstance(output, dict):
        return "success"
    if output.get("fallback_used") is True:
        return "out_of_coverage"
    match_type = str(output.get("match_type") or "").strip().lower()
    if match_type in {"fallback", "identity"}:
        return "out_of_coverage"
    return "success"


def last_tool_output_from_steps(step_results: list[dict[str, Any]] | None) -> dict[str, Any]:
    """Return the most recent step's ``output`` dict, or ``{}``.

    Walks ``step_results`` in reverse and returns the first ``output``
    that is itself a dict. Non-dict entries (skipped step records,
    failure markers without a payload) are tolerated. Empty / missing
    inputs return ``{}`` so callers can chain with ``.get()`` directly.
    """
    if not isinstance(step_results, list):
        return {}
    for step in reversed(step_results):
        if not isinstance(step, dict):
            continue
        output = step.get("output")
        if isinstance(output, dict):
            return output
    return {}


def api_outcome_from_execution(
    *,
    structured_output: dict[str, Any] | None,
    step_results: list[dict[str, Any]] | None = None,
) -> str:
    """Classify a multi-step execution as ``"success"`` or ``"out_of_coverage"``.

    Lookup order (first non-success wins):
      1. ``structured_output["last_tool_output"]`` (orchestrator-supplied
         summary of the final tool call).
      2. ``structured_output`` itself (in case the orchestrator stored
         the final tool's output at the top level).
      3. Each entry of ``step_results`` walked in reverse.

    Conservative: if every layer reports success the execution is
    success. Returning ``"out_of_coverage"`` only happens when at least
    one layer affirmatively said so.
    """
    if isinstance(structured_output, dict):
        last_output = structured_output.get("last_tool_output")
        if isinstance(last_output, dict):
            outcome = api_outcome_from_output(last_output)
            if outcome != "success":
                return outcome
        direct_outcome = api_outcome_from_output(structured_output)
        if direct_outcome != "success":
            return direct_outcome
    for step in reversed(step_results or []):
        if not isinstance(step, dict):
            continue
        output = step.get("output")
        if isinstance(output, dict):
            outcome = api_outcome_from_output(output)
            if outcome != "success":
                return outcome
    return "success"


# ---------------------------------------------------------------------------
# Task family inference (drives normalized_key partitioning)
# ---------------------------------------------------------------------------


def infer_capability_task_family(user_message: str | None, goal: str | None) -> str:
    """Bucket the user request into a coarse "task family" label.

    The family is part of the memory card's ``normalized_key`` so
    avoidance learnings only fire on similarly-shaped future requests.
    Labels:
      * ``"short_translation"`` — translation request, ≤140 chars and
        ≤24 tokens, no domain hint. Cheap general translators usually
        cover these.
      * ``"long_or_general_translation"`` — translation request that's
        long, token-heavy, or carries a domain hint (industry / business
        / strategy / 半導体 / 産業 / etc.). General translators tend to
        underperform here.
      * ``"general_request"`` — anything not flagged as translation.

    The translator-vs-general split exists because most early
    out-of-coverage failures clustered on long-form translation hitting
    short-form-only tools; partitioning the family lets the learning
    avoid those without poisoning short-translation availability.
    """
    text = f"{user_message or ''} {goal or ''}".strip()
    lowered = text.lower()
    translation_hint = any(
        token in lowered
        for token in (
            "translate",
            "translation",
            "英訳",
            "和訳",
            "翻訳",
            "日本語",
            "英語",
        )
    )
    if translation_hint:
        word_count = len(re.findall(r"\w+", lowered))
        domain_hint = any(
            token in lowered
            for token in (
                "industry",
                "business",
                "semiconductor",
                "market",
                "strategy",
                "policy",
                "半導体",
                "産業",
                "覇権",
                "市場",
                "戦略",
            )
        )
        if len(text) > 140 or word_count > 24 or domain_hint:
            return "long_or_general_translation"
        return "short_translation"
    return "general_request"


# ---------------------------------------------------------------------------
# Failure kind classification (drives expiry + scoring + content text)
# ---------------------------------------------------------------------------


def _looks_like_transient_runtime_failure(details: str) -> bool:
    lowered = details.lower()
    return any(
        token in lowered
        for token in (
            "503",
            "service unavailable",
            "temporar",
            "timeout",
            "timed out",
            "no tunnel",
            "tunnel",
        )
    )


def _looks_like_policy_or_limit(details: str) -> bool:
    lowered = details.lower()
    return any(
        token in lowered
        for token in (
            "policy_denied",
            "rate limit",
            "cap reached",
            "daily cap",
            "approval",
            "quota",
            "limit",
        )
    )


def failure_kind_from_execution(
    *,
    status: str | None,
    api_outcome: str | None,
    details: str,
) -> str | None:
    """Decide which failure-kind label (if any) applies to an execution.

    Priority:
      1. ``api_outcome == "out_of_coverage"`` → ``"out_of_coverage"``.
         This wins even when ``status == "succeeded"`` because the tool
         technically returned but didn't actually do the work.
      2. ``status == "failed"`` with policy / limit keywords in
         ``details`` → ``"policy_or_limit"``.
      3. ``status == "failed"`` with transient runtime keywords in
         ``details`` (5xx / timeout / tunnel) → ``"runtime_unavailable"``.
      4. ``status == "failed"`` with no matched keyword →
         ``"execution_failure"``.
      5. Otherwise → ``None`` (no learning recorded).

    The keyword lists are intentionally small — false positives here
    cause a tool to be avoided when it's actually fine, which is more
    expensive than a missed learning (the next failure picks it up).
    """
    if str(api_outcome or "").strip().lower() == "out_of_coverage":
        return "out_of_coverage"
    if str(status or "").strip().lower() != "failed":
        return None
    if _looks_like_policy_or_limit(details):
        return "policy_or_limit"
    if _looks_like_transient_runtime_failure(details):
        return "runtime_unavailable"
    return "execution_failure"


# ---------------------------------------------------------------------------
# Memory card scoring + expiry (per failure_kind)
# ---------------------------------------------------------------------------


def learning_expiry_for_kind(
    failure_kind: str,
    *,
    now: dt.datetime,
) -> dt.datetime | None:
    """Return the absolute expiry instant for a memory card of this kind.

    Returns ``None`` for ``"out_of_coverage"`` (never expires —
    coverage gaps are persistent until a publisher updates the
    capability) and for any unknown kind (fail-open: a card with no
    expiry can still be superseded by a later card).

    ``now`` is required as a keyword argument so the function stays
    pure: callers (the platform DB layer, tests, an eval harness)
    supply their own clock and we never read a wall clock here. The
    monorepo passes ``utcnow()``; tests pass a frozen instant.

    Durations:
      * runtime_unavailable: 1 hour (transient)
      * policy_or_limit: 6 hours (until owner addresses approval / quota)
      * execution_failure: 1 day (broader recovery window)
      * system_prompt_overflow: 24 hours (config-issue, lasts until rebuild)
      * out_of_coverage: never (returns ``None``)
    """
    if failure_kind == "runtime_unavailable":
        return now + dt.timedelta(hours=1)
    if failure_kind == "policy_or_limit":
        return now + dt.timedelta(hours=6)
    if failure_kind == "execution_failure":
        return now + dt.timedelta(days=1)
    if failure_kind == SYSTEM_PROMPT_OVERFLOW_KIND:
        return now + dt.timedelta(hours=24)
    return None


def learning_scores_for_kind(failure_kind: str) -> tuple[float, float]:
    """Return ``(importance, confidence)`` for a memory card of this kind.

    The ranking layer reads ``importance`` to decide which cards survive
    truncation when the agent has many; ``confidence`` is shown to the
    LLM so it can weigh the avoidance against fresh contradictory
    evidence. Both are in [0, 1].

    Defaults (returned for unknown kinds): ``(0.7, 0.74)`` — a
    moderately confident generic learning. Out-of-coverage and overflow
    cards score highest because their false-positive rate is low.
    """
    if failure_kind == "out_of_coverage":
        return 0.9, 0.92
    if failure_kind == "runtime_unavailable":
        return 0.55, 0.66
    if failure_kind == "policy_or_limit":
        return 0.5, 0.62
    if failure_kind == SYSTEM_PROMPT_OVERFLOW_KIND:
        return 0.9, 0.95
    return 0.7, 0.74


# ---------------------------------------------------------------------------
# Memory card content templates
# ---------------------------------------------------------------------------


def build_learning_content(
    *,
    tool: ResolvedToolDefinition,
    failure_kind: str,
    task_family: str,
    request_preview: str,
) -> str:
    """Render the human-readable advice string stored on the memory card.

    The advice is what the LLM reads at the next selection step; it
    explains *why* a particular tool should not be picked again. The
    string is also visible to operators inspecting cards directly.

    Branches by failure_kind so each kind nudges the LLM toward a
    different next step (try a different tool, retry later, satisfy
    the missing condition).
    """
    if failure_kind == "out_of_coverage":
        return (
            f"Tool selection guidance: avoid using {tool.display_name} for {task_family} "
            f"requests similar to '{request_preview}'. The last execution was out of coverage; "
            "prefer the base LLM or a broader installed API unless the request clearly matches "
            "this tool's documented scope."
        )
    if failure_kind == "runtime_unavailable":
        return (
            f"Tool selection guidance: {tool.display_name} recently had a transient runtime "
            f"failure for '{request_preview}'. Avoid immediate retries unless the owner "
            "explicitly asks to retry."
        )
    if failure_kind == "policy_or_limit":
        return (
            f"Tool selection guidance: {tool.display_name} recently hit a policy, approval, "
            f"or usage limit for '{request_preview}'. Do not retry blindly; satisfy the "
            "missing approval/account/limit condition first."
        )
    return (
        f"Tool selection guidance: {tool.display_name} recently failed for {task_family} "
        f"requests similar to '{request_preview}'. Prefer an alternative until this API is "
        "known healthy."
    )


def build_system_prompt_overflow_content(
    *,
    request_preview: str,
    fit_meta: dict[str, Any] | None,
) -> str:
    """Render the advice text for a system-prompt-overflow card.

    Distinct from per-tool failures because the cause is *systemic*
    (system prompt > input token budget): no individual tool is to
    blame, and the advice points at config remediations (fewer tools
    bound, higher input budget, prefilter on). When ``fit_meta``
    carries integer ``estimated_required_system_tokens`` and
    ``chat_input_token_budget`` fields, they are surfaced so an
    operator can size the gap immediately.
    """
    fit = fit_meta or {}
    required = fit.get("estimated_required_system_tokens")
    budget = fit.get("chat_input_token_budget")
    detail = ""
    if isinstance(required, int) and isinstance(budget, int) and budget > 0:
        detail = f" (required={required} tokens vs budget={budget})"
    return (
        "System configuration alert: the agent's required system prompt exceeded "
        f"the chat input token budget{detail}. This is a configuration issue "
        "(too many bound tools / oversized prompt sections), not a tool failure. "
        f"Last request preview: '{request_preview}'. Reduce bound tools, raise "
        "the input budget, or enable the installed-tool pre-filter so the LLM "
        "can be reached at all."
    )


__all__ = [
    "CAPABILITY_LEARNING_TAG",
    "CAPABILITY_PREFERENCE_MEMORY_TYPE",
    "SYSTEM_PROMPT_OVERFLOW_KIND",
    "api_outcome_from_execution",
    "api_outcome_from_output",
    "build_learning_content",
    "build_system_prompt_overflow_content",
    "clip_text",
    "failure_kind_from_execution",
    "infer_capability_task_family",
    "last_tool_output_from_steps",
    "learning_expiry_for_kind",
    "learning_scores_for_kind",
]
