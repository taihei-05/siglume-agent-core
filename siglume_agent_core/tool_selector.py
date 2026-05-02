"""Keyword-based tool selection for installed-tool runtime.

Companion to ``installed_tool_prefilter`` (which trims a large catalog
down to ~50 entries via TF-IDF). This module's job runs one step later:
once the prompt-budget filter has rendered a manageable set of tools,
``select_tools`` picks the actual handful (default 5) that scored
highest against the user's request, and returns them in score order so
the LLM sees the most relevant candidates first.

Algorithm (v1):
    Per-tool score = (keyword overlap × 10)
                   + readiness bonus (ready=+5, unhealthy=-5)
                   + auto-execute read-only bonus (+3)
                   + approval friction penalty (-2 for ask / owner_approval)
                   - cost_hint_usd_cents / 100  (USD penalty)

    Trigger words come from capability_key + display_name + description
    + usage_hints, lowercased and split on ``[a-z0-9]+``. The user
    request is tokenised the same way so overlap is symmetric.

    Hard filter applied BEFORE scoring: tools with
    ``account_readiness == "missing"`` and a permission_class that
    requires an account (action / payment) are excluded unless the
    caller passes ``include_missing_accounts=True``. The prefilter
    runs earlier and is allowed to keep missing-account tools so the
    UI can show "you need to connect X to use this", but at dispatch
    time we never want the LLM to pick one.

Repository pattern:
    The platform persists "no useful match" gap signals to drive the
    cross-publisher gap-report. This module is pure — it surfaces the
    signal via the optional ``on_unmatched`` callback and lets the
    caller persist however it wants (SQLAlchemy SAVEPOINT, a queue,
    a metric counter — agent-core does not care). Callback exceptions
    are swallowed so the request path never breaks on telemetry.

Out of scope for v1:
    Learned re-ranking, recency weighting, semantic similarity. Those
    become useful once the gap-report has a few weeks of data on which
    tools the LLM actually picks once the prompt narrows down to 5.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from .types import ResolvedToolDefinition

logger = logging.getLogger(__name__)

# Default catalog size handed to the LLM after final selection. The
# prefilter runs first (default 50); this is the dispatch-time top-K.
DEFAULT_MAX_CANDIDATES = 5

# Permission classes that require an account to be already connected
# before the tool can dispatch. Tools in these classes get hard-filtered
# when account_readiness == "missing", because picking them would
# guarantee a runtime failure the LLM can't recover from.
#
# "write" is included for forward-compatibility — the validator's
# canonical set is {"read-only","recommendation","action","payment"} but
# the platform's ResolvedToolDefinition has historically tolerated a
# "write" string in this filter. Kept as-is for byte-equivalent
# behaviour with the platform-side ToolSelector.
_REQUIRES_ACCOUNT_PERMISSION_CLASSES: tuple[str, ...] = ("action", "payment", "write")

# Stop words excluded from the unmatched-request shape signature so
# common filler doesn't bloat the fingerprint and produce thousands of
# near-identical "shapes" in the gap-report. Mirrors
# seller_analytics._STOP_WORDS in the platform so aggregated gap
# fingerprints stay comparable across modules.
UNMATCHED_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "it",
        "as",
        "be",
        "was",
        "are",
        "this",
        "that",
        "if",
        "not",
        "no",
        "can",
        "do",
        "has",
        "have",
        "will",
        "all",
        "any",
        "my",
        "your",
        "our",
        "its",
        "so",
        "up",
        "out",
        "about",
        "than",
        "them",
        "then",
        "into",
        "over",
        "such",
        "when",
        "which",
        "what",
        "how",
        "where",
        "who",
        "each",
        "some",
        "these",
        "those",
        "been",
        "had",
        "just",
        "more",
        "also",
        "very",
        "may",
        "should",
        "could",
        "would",
        "their",
        "there",
        "here",
        "other",
        "only",
        "please",
        "want",
        "need",
    }
)

# Cap on tokens that participate in the shape hash. Long unique queries
# shouldn't form their own fingerprint; truncating limits how much
# "shape entropy" a single query can carry into the gap-report.
UNMATCHED_MAX_TOKENS_FOR_HASH: int = 16

# Cap on the redacted request sample stored on the gap signal. The
# storage column itself may be larger; this is the hard upper bound
# the open-core layer guarantees regardless of what the caller's
# redactor does.
UNMATCHED_TEXT_MAX_LEN: int = 200

# Q4-style backstop: catch long alphanumeric / base64 / hex runs that a
# Q10-style key-pattern redactor might miss. Conservative — only
# triggers on runs the length of real keys.
_LONG_HEX_RE = re.compile(r"\b[a-fA-F0-9]{32,}\b")
_LONG_BASE64_RE = re.compile(r"\b[A-Za-z0-9+/=_-]{40,}\b")


def strip_long_alphanumeric_secrets(text: str) -> str:
    """Q4-style catch-all: strip long hex / base64 runs that look like keys.

    Public utility — callers composing their own ``redactor`` for
    ``select_tools`` typically want to chain this AFTER their primary
    redactor (Q10 / pattern-based / etc.) to catch any long unstructured
    runs the primary missed. Idempotent: repeated calls converge once
    no long runs remain.

    Returns the input unchanged if empty.
    """
    if not text:
        return text
    out = _LONG_HEX_RE.sub("<redacted-long-hex>", text)
    out = _LONG_BASE64_RE.sub("<redacted-long-base64>", out)
    return out


MissKind = Literal[
    "no_tools_installed",
    "all_filtered_account_missing",
    "no_keyword_match",
]


@dataclass(frozen=True)
class UnmatchedRequestSignal:
    """Pure description of a request that produced no useful tool match.

    The caller decides how to persist this — agent-core surfaces it via
    callback so the platform can wrap a SAVEPOINT around the DB write
    while a different host (CLI, eval harness, queue worker) might just
    log it. The fields are exactly what the platform's
    ``UnmatchedCapabilityRequest`` row stores, minus host-specific
    metadata (``agent_id`` / ``owner_user_id``) the caller captures via
    closure.
    """

    miss_kind: MissKind
    request_text_redacted: str
    """The redacted request sample (capped at ``UNMATCHED_TEXT_MAX_LEN``).

    Empty string if the redactor returned empty or the input was empty.
    """

    request_words: list[str] = field(default_factory=list)
    """Sorted unique tokens after stop-word removal.

    Same set used to compute ``shape_hash``; surfaced separately so
    the caller can store both the hash (for cheap aggregation) and the
    raw token list (for human inspection).
    """

    shape_hash: str = ""
    """SHA-256 hex digest of the comma-joined first-N tokens.

    N is ``UNMATCHED_MAX_TOKENS_FOR_HASH``. Enables aggregation across
    requests with the same vocabulary shape without storing the full
    token list as the join key.
    """

    available_tool_count: int = 0
    candidate_count_after_filter: int = 0
    top_match_count: int = 0


# Caller-supplied redactor. Receives the raw request text, returns the
# redacted form. agent-core will additionally cap the result at
# ``UNMATCHED_TEXT_MAX_LEN`` characters.
RedactorCallback = Callable[[str], str]

# Caller-supplied gap-signal sink. Invoked exactly once per
# zero-useful-match outcome. Exceptions are caught + logged at WARNING;
# select_tools never raises on a callback failure.
OnUnmatchedCallback = Callable[[UnmatchedRequestSignal], None]


def extract_trigger_words(tool: ResolvedToolDefinition) -> set[str]:
    """Gather searchable keywords from tool metadata.

    Lower-cased, split on ``[a-z0-9]+`` so punctuation and whitespace
    don't carry into the bag. Mirrors the symmetric tokenisation
    applied to the user's request text.

    Public so callers that need a tool's trigger-word set outside of
    ``select_tools`` (e.g. test-runner overlap diagnostics, alias
    routers) can use the same canonical extraction without
    re-implementing the tokeniser.
    """
    parts: list[str] = [
        tool.capability_key,
        tool.display_name,
        tool.description,
    ]
    parts.extend(tool.usage_hints)
    combined = " ".join(p for p in parts if p).lower()
    return set(re.findall(r"[a-z0-9]+", combined))


def _score_tool(tool: ResolvedToolDefinition, request_words: set[str]) -> float:
    """Compute relevance score for one tool against the request word set.

    Pure: identical input always produces identical output. Negative
    scores are valid (a high-friction expensive tool with no keyword
    overlap can score below zero).
    """
    score: float = 0.0

    trigger_words = extract_trigger_words(tool)
    match_count = len(trigger_words & request_words)
    score += match_count * 10.0

    if tool.account_readiness == "ready":
        score += 5.0
    elif tool.account_readiness == "unhealthy":
        score -= 5.0
    # "missing" gets 0

    if tool.approval_mode == "auto" and tool.permission_class == "read_only":
        score += 3.0

    if tool.approval_mode in ("always_ask", "owner_approval"):
        score -= 2.0

    if tool.cost_hint_usd_cents is not None and tool.cost_hint_usd_cents > 0:
        score -= tool.cost_hint_usd_cents / 100.0

    return score


def _build_unmatched_signal(
    *,
    request_text: str,
    miss_kind: MissKind,
    available_tool_count: int,
    candidate_count_after_filter: int,
    top_match_count: int,
    redactor: RedactorCallback | None,
) -> UnmatchedRequestSignal | None:
    """Build the gap signal payload from a request.

    Returns ``None`` if the token set is empty after redaction +
    stop-word removal — emitting a signal in that case would produce
    a degenerate empty-set hash that collides across all "fully
    redacted" or "stop-word-only" requests, and pollutes the
    gap-report.

    Errors from the caller's ``redactor`` are caught: the request text
    falls back to empty so the signal still records the *shape* of the
    miss (counts + hash) even if redaction failed.
    """
    redacted = ""
    if request_text:
        if redactor is None:
            redacted = request_text
        else:
            try:
                redacted = redactor(request_text) or ""
            except Exception:  # noqa: BLE001 - never break on caller redactor
                logger.warning(
                    "tool_selector: redactor raised; storing empty sample", exc_info=True
                )
                redacted = ""
    capped = redacted[:UNMATCHED_TEXT_MAX_LEN]

    raw_words = re.findall(r"[a-z0-9]+", capped.lower())
    filtered = [w for w in raw_words if w not in UNMATCHED_STOP_WORDS]
    unique_sorted = sorted(set(filtered))
    if not unique_sorted:
        return None

    hashed_tokens = unique_sorted[:UNMATCHED_MAX_TOKENS_FOR_HASH]
    shape_hash = hashlib.sha256(",".join(hashed_tokens).encode("utf-8")).hexdigest()

    return UnmatchedRequestSignal(
        miss_kind=miss_kind,
        request_text_redacted=capped,
        request_words=unique_sorted,
        shape_hash=shape_hash,
        available_tool_count=available_tool_count,
        candidate_count_after_filter=candidate_count_after_filter,
        top_match_count=top_match_count,
    )


def _emit_unmatched(
    *,
    on_unmatched: OnUnmatchedCallback | None,
    signal: UnmatchedRequestSignal | None,
) -> None:
    """Invoke the caller's gap-signal sink with full exception isolation."""
    if on_unmatched is None or signal is None:
        return
    try:
        on_unmatched(signal)
    except Exception:  # noqa: BLE001 - never break request path on telemetry sink
        logger.warning("tool_selector: on_unmatched callback raised", exc_info=True)


def select_tools(
    tools: Sequence[ResolvedToolDefinition],
    request_text: str,
    *,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    include_missing_accounts: bool = False,
    on_unmatched: OnUnmatchedCallback | None = None,
    redactor: RedactorCallback | None = None,
) -> list[ResolvedToolDefinition]:
    """Return up to ``max_candidates`` tools ranked by relevance score.

    Behaviour contract:
      * Empty inputs short-circuit to the original (possibly empty)
        list trimmed to ``max_candidates``. If ``request_text`` is
        non-empty but ``tools`` is empty, a "no_tools_installed" gap
        signal is emitted before returning.
      * The hard filter on ``account_readiness == "missing"`` runs
        BEFORE scoring. ``include_missing_accounts=True`` disables the
        filter (used by the prefilter / catalog views that want to
        show "needs-account" tools so the UI can prompt connection).
      * The gap signal (``on_unmatched``) fires for one of three
        miss kinds, evaluated BEFORE truncating to ``max_candidates``:
            - ``no_tools_installed``: caller passed an empty tool list
            - ``all_filtered_account_missing``: every tool got filtered
            - ``no_keyword_match``: no tool's trigger words overlap
        Truncation of "we have candidates but the LLM might still
        not pick any" is not a miss — picking is the LLM's job.
      * Sort is descending by score, ties broken by original index
        (stable). The original order encodes binding creation history
        which is a useful weak prior when scores tie.
    """
    if max_candidates <= 0:
        return []

    tools_list = list(tools or [])

    if not tools_list or not request_text:
        if request_text and not tools_list:
            signal = _build_unmatched_signal(
                request_text=request_text,
                miss_kind="no_tools_installed",
                available_tool_count=0,
                candidate_count_after_filter=0,
                top_match_count=0,
                redactor=redactor,
            )
            _emit_unmatched(on_unmatched=on_unmatched, signal=signal)
        return tools_list[:max_candidates]

    request_lower = request_text.lower()
    request_words = set(re.findall(r"[a-z0-9]+", request_lower))

    scored: list[tuple[float, int, ResolvedToolDefinition, int]] = []
    for idx, tool in enumerate(tools_list):
        if (
            not include_missing_accounts
            and tool.account_readiness == "missing"
            and tool.permission_class in _REQUIRES_ACCOUNT_PERMISSION_CLASSES
        ):
            continue

        score = _score_tool(tool, request_words)
        match_count = len(extract_trigger_words(tool) & request_words)
        scored.append((score, idx, tool, match_count))

    candidate_count = len(scored)
    top_match_count = max((s[3] for s in scored), default=0)

    if candidate_count == 0:
        signal = _build_unmatched_signal(
            request_text=request_text,
            miss_kind="all_filtered_account_missing",
            available_tool_count=len(tools_list),
            candidate_count_after_filter=0,
            top_match_count=0,
            redactor=redactor,
        )
        _emit_unmatched(on_unmatched=on_unmatched, signal=signal)
    elif top_match_count == 0:
        signal = _build_unmatched_signal(
            request_text=request_text,
            miss_kind="no_keyword_match",
            available_tool_count=len(tools_list),
            candidate_count_after_filter=candidate_count,
            top_match_count=0,
            redactor=redactor,
        )
        _emit_unmatched(on_unmatched=on_unmatched, signal=signal)

    scored.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in scored[:max_candidates]]


__all__ = [
    "DEFAULT_MAX_CANDIDATES",
    "MissKind",
    "OnUnmatchedCallback",
    "RedactorCallback",
    "UNMATCHED_MAX_TOKENS_FOR_HASH",
    "UNMATCHED_STOP_WORDS",
    "UNMATCHED_TEXT_MAX_LEN",
    "UnmatchedRequestSignal",
    "extract_trigger_words",
    "select_tools",
    "strip_long_alphanumeric_secrets",
]
