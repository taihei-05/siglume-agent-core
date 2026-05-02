"""Pure publisher dev-simulator helpers — Tier C Phase 3 (v0.7).

The platform's
``packages/shared-python/agent_sns/application/capability_runtime/dev_simulator.py``
predicts which tools the API Store planner would call for a publisher's
offer text, *without* executing any of them. The full pipeline is:

  1. Top-N catalog selection (DB query — platform glue)
  2. Keyword pre-filter to top candidates (pure — this module)
  3. Single ``tool_choice="auto"`` LLM turn against Anthropic Haiku
     (LLM call wrapped behind an injected callable so this module does
     not import the Anthropic SDK)
  4. Returns the predicted tool chain — no side-effects

Stages 2-4 live here. The platform passes:

* the already-fetched catalog rows (so this module never touches a
  ``Session``); rows are typed against the
  :class:`ProductListingLike` / :class:`CapabilityReleaseLike` Protocols
  so platform ORM models satisfy them structurally without any inheritance.
* an :class:`LLMSimulateCall` callable that receives ``(system_prompt,
  tools, user_message)`` and returns an :class:`LLMSimulateResponse`
  carrying tool_use blocks (or an ``error_note`` when the call failed /
  the SDK is missing).

Byte-equivalence contract (verified against the monorepo platform copy
at ``packages/shared-python/agent_sns/application/capability_runtime/dev_simulator.py``):

* :data:`STOP_WORDS` set, :data:`ANTHROPIC_PROPERTY_KEY_RE` /
  :data:`ANTHROPIC_TOOL_NAME_RE` patterns and :data:`SIMULATE_MODEL` /
  :data:`SIMULATE_SYSTEM_PROMPT` strings — character-for-character.
* :func:`extract_keywords` regex (``[a-z0-9]+`` after lowercasing) and
  stop-word filter.
* :func:`build_tool_def` field-fallback order
  (``tool_prompt_compact`` -> ``manual.compact_prompt`` ->
  ``manual.description`` -> ``manual.summary_for_model`` ->
  ``listing.description`` -> ``listing.title`` -> ``capability_key``)
  and the 600-char description clip.
* :func:`sanitize_input_schema_for_anthropic` recursion / ``required``
  pruning — same dropped-keys behavior the platform shipped in PR #201.
* Dedupe + Anthropic name-regex skip in
  :func:`filter_tools_for_anthropic` — first-wins-by-score, same
  ``skipped_dup`` / ``skipped_bad_name`` counters PR #203 added.
* :func:`simulate_planner` short-circuit notes (``"empty offer_text"``,
  ``"no candidate tools — catalog empty or all listings unusable"``,
  ``"no candidate tools survived Anthropic schema validation ..."``,
  ``"LLM picked no tools (offer may not match any catalog entry)"``)
  produced verbatim.

The platform shim retains the DB query plus a thin
:class:`LLMSimulateCall` factory wrapping the Anthropic SDK; everything
else is now sourced from this module.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants — byte-equivalent with the monorepo dev_simulator.py.
# ---------------------------------------------------------------------------

SIMULATE_MODEL: str = "claude-haiku-4-5-20251001"
"""Default model used by :func:`simulate_planner`. Hardcoded for cost /
latency predictability — v1 of the publisher dev simulator."""


STOP_WORDS: frozenset[str] = frozenset(
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


# Anthropic enforces this regex on every property key in tool
# input_schema.properties. A single non-conforming key causes a 400
# BadRequestError that aborts the entire tool_use call (not just the
# offending tool). We pre-filter to skip bad keys rather than reject the
# whole simulate request.
ANTHROPIC_PROPERTY_KEY_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")

# Anthropic also enforces a regex on the tool's `name` field. Note this
# is stricter than the property-key one (no '.'). A single non-conforming
# tool name aborts the whole tool_use call, same blast-radius problem as
# the property-key regex above.
ANTHROPIC_TOOL_NAME_RE: re.Pattern[str] = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


SIMULATE_SYSTEM_PROMPT: str = (
    "You are a dev simulator for the Siglume API Store planner. Given the "
    "user's offer text, decide which of the provided tools to call and in "
    "what order to fulfill it. Use tool_use blocks for your selection. "
    "Pick AS FEW tools as needed. Do not execute the tools yourself — "
    "tool_use blocks here are PLAN ONLY."
)
"""System prompt for the single ``tool_choice="auto"`` simulate turn.
Kept here so the platform shim and the OSS loop stay byte-equivalent."""


# ---------------------------------------------------------------------------
# Protocols for catalog rows — agent-core stays free of ORM / SQLAlchemy
# imports. Platform ORM models satisfy these structurally.
# ---------------------------------------------------------------------------


class ProductListingLike(Protocol):
    """Minimal contract :func:`build_tool_def` reads from a ProductListing.

    Platform's ``ProductListing`` ORM model satisfies this structurally;
    no inheritance required. ``id`` / ``title`` / ``description`` /
    ``capability_key`` are all read defensively (``getattr`` with a
    default), so the absence of any non-id attribute is tolerated.
    """

    id: Any  # str-like; coerced via ``str(listing.id)``


class CapabilityReleaseLike(Protocol):
    """Minimal contract :func:`build_tool_def` reads from a CapabilityRelease.

    All three fields are JSON / text columns that may be None on the
    platform side; :func:`build_tool_def` handles that fallback chain.
    """

    tool_manual_jsonb: Any
    tool_prompt_compact: Any
    input_schema_jsonb: Any


# ---------------------------------------------------------------------------
# Result records (kept non-frozen — byte-equivalent with monorepo).
# ---------------------------------------------------------------------------


@dataclass
class SimulatedToolCall:
    """One predicted tool call in the simulated chain."""

    tool_name: str
    capability_key: str
    listing_id: str
    listing_title: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class SimulationResult:
    """The result of a :func:`simulate_planner` call."""

    offer_text: str
    catalog_size: int
    candidates_considered: int
    predicted_chain: list[SimulatedToolCall]
    model: str
    quota_used_today: int
    quota_limit: int
    note: str | None = None


# ---------------------------------------------------------------------------
# LLM-call abstraction — caller injects a callable so this module never
# touches the Anthropic (or any other) SDK.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LLMSimulateToolUseBlock:
    """One tool_use block extracted from the simulate LLM turn.

    Provider-neutral shape: the platform's Anthropic-wrapping callable
    converts ``response.content[i]`` (when ``type == "tool_use"``) into
    this. ``input`` is whatever the LLM emitted as the tool call args —
    no validation here; the simulator is "what would the planner have
    done", not "is the call shape valid".
    """

    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class LLMSimulateResponse:
    """Provider-neutral simulate-turn response.

    ``tool_use_blocks`` is empty when the LLM declined to pick a tool,
    when the SDK is missing, or when the call errored. ``error_note``
    distinguishes those cases — it is propagated verbatim into
    :attr:`SimulationResult.note` so callers see exactly the same
    diagnostic strings the monorepo shipped pre-extraction.
    """

    tool_use_blocks: list[LLMSimulateToolUseBlock]
    error_note: str | None = None


LLMSimulateCall = Callable[
    [str, list[dict[str, Any]], str],
    LLMSimulateResponse,
]
"""Signature for the injected simulate-turn callable.

Arguments: ``(system_prompt, tools, user_message)`` where ``tools`` is
the dedupe/filter output of :func:`filter_tools_for_anthropic` (already
Anthropic-conforming). The callable MUST NOT raise — provider failures
should surface as ``error_note`` on :class:`LLMSimulateResponse`.
"""


# ---------------------------------------------------------------------------
# Pure helpers — these are tested in isolation.
# ---------------------------------------------------------------------------


def extract_keywords(text: str) -> set[str]:
    """Lowercase tokens of ``[a-z0-9]+`` minus :data:`STOP_WORDS`."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w and w not in STOP_WORDS}


def sanitize_input_schema_for_anthropic(schema: dict[str, Any]) -> dict[str, Any]:
    """Strip property keys that Anthropic rejects (^[a-zA-Z0-9_.-]{1,64}$).

    Recurses into nested ``properties``. Keys violating the pattern are
    dropped from this level (and any matching ``required`` entries pruned)
    so a bad key on one published listing doesn't poison the whole
    simulate request — Anthropic 400s the entire call when even one tool
    has a non-conforming key. Returns a sanitized COPY; never mutates the
    caller's dict.
    """
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    out: dict[str, Any] = {k: v for k, v in schema.items() if k != "properties"}
    props = schema.get("properties")
    if isinstance(props, dict):
        clean_props: dict[str, Any] = {}
        dropped: list[str] = []
        for key, value in props.items():
            if not isinstance(key, str) or not ANTHROPIC_PROPERTY_KEY_RE.match(key):
                dropped.append(str(key))
                continue
            # Recurse one level: nested object schemas may also have bad keys.
            if isinstance(value, dict) and isinstance(value.get("properties"), dict):
                value = sanitize_input_schema_for_anthropic(value)
            clean_props[key] = value
        out["properties"] = clean_props
        if dropped:
            logger.debug(
                "dev_simulator: dropped %d non-Anthropic-conforming property keys: %s",
                len(dropped),
                dropped[:5],
            )
        # If 'required' list referenced any of the dropped keys, prune them.
        required = out.get("required")
        if isinstance(required, list) and dropped:
            out["required"] = [r for r in required if r not in dropped]
    else:
        out["properties"] = {}
    return out


def build_tool_def(
    listing: ProductListingLike,
    release: CapabilityReleaseLike,
) -> dict[str, Any] | None:
    """Build a candidate tool definition from a (listing, release) pair.

    Returns None when the listing has no usable manual / capability_key —
    such listings are skipped from the simulator catalog.
    """
    manual_jsonb = getattr(release, "tool_manual_jsonb", None)
    manual = manual_jsonb if isinstance(manual_jsonb, dict) else {}
    capability_key = manual.get("capability_key") or getattr(listing, "capability_key", None) or ""
    capability_key = str(capability_key).strip()
    if not capability_key:
        return None

    description = (
        getattr(release, "tool_prompt_compact", None)
        or manual.get("compact_prompt")
        or manual.get("description")
        or manual.get("summary_for_model")
        or getattr(listing, "description", None)
        or getattr(listing, "title", None)
        or capability_key
    )
    description = str(description).strip()[:600]

    raw_schema = getattr(release, "input_schema_jsonb", None) or manual.get("input_schema") or {}
    if not isinstance(raw_schema, dict):
        raw_schema = {}
    if "type" not in raw_schema:
        raw_schema = {**raw_schema, "type": "object"}
    if "properties" not in raw_schema:
        raw_schema = {**raw_schema, "properties": {}}
    raw_schema = sanitize_input_schema_for_anthropic(raw_schema)

    return {
        "name": capability_key,
        "description": description,
        "input_schema": raw_schema,
        "_listing_id": str(listing.id),
        "_listing_title": str(getattr(listing, "title", "") or capability_key),
    }


def score_candidate(tool_def: dict[str, Any], offer_keywords: set[str]) -> int:
    """Cheap keyword-overlap score so we hand the LLM a relevant top-N."""
    text = " ".join(
        [
            str(tool_def.get("name") or ""),
            str(tool_def.get("description") or ""),
        ]
    ).lower()
    tool_words = {w for w in re.findall(r"[a-z0-9]+", text) if w and w not in STOP_WORDS}
    return len(offer_keywords & tool_words)


def select_candidates(
    rows: Sequence[tuple[ProductListingLike, CapabilityReleaseLike]],
    *,
    offer_text: str,
    max_candidates: int,
) -> list[dict[str, Any]]:
    """Score (listing, release) rows by keyword overlap and return the top-N.

    Listings that fail :func:`build_tool_def` (no capability_key) are
    dropped silently. ``max_candidates`` is clamped to a minimum of 1
    via ``max(1, max_candidates)`` — same shape the monorepo's
    ``simulate_planner`` shipped, so a caller passing ``0`` still gets
    one candidate.
    """
    offer_keywords = extract_keywords(offer_text)
    scored: list[tuple[int, dict[str, Any]]] = []
    for listing, release in rows:
        tool_def = build_tool_def(listing, release)
        if tool_def is None:
            continue
        scored.append((score_candidate(tool_def, offer_keywords), tool_def))
    scored.sort(key=lambda x: -x[0])
    return [d for _, d in scored[: max(1, max_candidates)]]


def filter_tools_for_anthropic(
    candidate_tool_defs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, str]], int, int]:
    """Dedupe + Anthropic-name-regex filter the candidate tool defs.

    Returns ``(clean_tools, listing_lookup, skipped_dup, skipped_bad_name)``:

    * ``clean_tools`` — Anthropic-ready ``{name, description,
      input_schema}`` dicts (the leading ``_listing_*`` private keys
      stripped).
    * ``listing_lookup`` — ``name -> {listing_id, listing_title}`` so
      the caller can re-attach listing metadata to the LLM's emitted
      tool_use blocks.
    * ``skipped_dup`` / ``skipped_bad_name`` — counters surfaced into
      the SimulationResult.note when every candidate is filtered out.

    Anthropic 400s "tools: Tool names must be unique." if two tools
    share a name. Multiple published listings legitimately share the
    same capability_key (e.g. competing implementations of
    "post_to_slack"), so dedupe by name before handing the tool array
    to the LLM. ``candidate_tool_defs`` is expected pre-sorted by score
    descending (the contract :func:`select_candidates` produces), so
    first-wins keeps the most relevant listing for each capability_key.
    """
    listing_lookup: dict[str, dict[str, str]] = {}
    clean_tools: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    skipped_dup = 0
    skipped_bad_name = 0
    for tool in candidate_tool_defs:
        name = tool["name"]
        if name in seen_names:
            skipped_dup += 1
            continue
        if not ANTHROPIC_TOOL_NAME_RE.match(name):
            # info, not debug: a publisher whose capability_key contains '.',
            # space, or non-ASCII silently disappears from the simulator
            # otherwise. They have no other signal that their listing is
            # invisible here.
            logger.info(
                "dev_simulator: skipping listing %s — capability_key %r "
                "does not match Anthropic tool name regex %s",
                tool.get("_listing_id"),
                name,
                ANTHROPIC_TOOL_NAME_RE.pattern,
            )
            skipped_bad_name += 1
            continue
        seen_names.add(name)
        listing_lookup[name] = {
            "listing_id": tool["_listing_id"],
            "listing_title": tool["_listing_title"],
        }
        clean_tools.append(
            {
                "name": name,
                "description": tool["description"],
                "input_schema": tool["input_schema"],
            }
        )
    return clean_tools, listing_lookup, skipped_dup, skipped_bad_name


# ---------------------------------------------------------------------------
# High-level entry point — composes the pure helpers + injected LLM call.
# ---------------------------------------------------------------------------


def simulate_planner(
    rows: Sequence[tuple[ProductListingLike, CapabilityReleaseLike]],
    *,
    offer_text: str,
    quota_used_today: int,
    quota_limit: int,
    llm_call: LLMSimulateCall,
    max_candidates: int = 10,
    model: str = SIMULATE_MODEL,
) -> SimulationResult:
    """Predict the orchestrator tool chain for ``offer_text`` against ``rows``.

    Single-turn LLM call via the injected ``llm_call``. Returns at most
    ``max_candidates`` tools in the predicted chain; usually fewer since
    the LLM picks just what it needs. Catalog selection (which rows to
    pass) is the caller's responsibility — typically the platform reads
    the public catalog from the DB, sorted by recency, capped to
    ``max_catalog`` rows.

    ``rows`` MUST support ``len()`` (it is read for ``catalog_size``), so
    pass a concrete list — not a generator or a SQLAlchemy ``Query``
    object. The platform's call site uses ``.all()`` which already
    materialises a list.

    ``llm_call`` is invoked at most once per call. The contract is "MUST
    NOT raise" — provider errors should surface as
    :attr:`LLMSimulateResponse.error_note`. If the callable raises,
    :func:`simulate_planner` propagates the exception verbatim — there is
    no try/except around the invocation.

    All early-return branches preserve the monorepo's diagnostic strings
    on :attr:`SimulationResult.note`:

    * ``"empty offer_text"`` — offer text trims to empty.
    * ``"no candidate tools — catalog empty or all listings unusable"`` —
      every row failed :func:`build_tool_def`.
    * ``"no candidate tools survived Anthropic schema validation
      (skipped X duplicate, Y non-conforming name)"`` — every candidate
      was deduped out or had an Anthropic-incompatible name.
    * ``"LLM picked no tools (offer may not match any catalog entry)"`` —
      the LLM returned no tool_use blocks.
    * ``error_note`` from :class:`LLMSimulateResponse` — propagated
      verbatim when the SDK is missing or the call raised.
    """
    offer_text = (offer_text or "").strip()
    if not offer_text:
        return SimulationResult(
            offer_text="",
            catalog_size=0,
            candidates_considered=0,
            predicted_chain=[],
            model=model,
            quota_used_today=quota_used_today,
            quota_limit=quota_limit,
            note="empty offer_text",
        )

    catalog_size = len(rows)
    candidates = select_candidates(
        rows,
        offer_text=offer_text,
        max_candidates=max_candidates,
    )

    if not candidates:
        return SimulationResult(
            offer_text=offer_text,
            catalog_size=catalog_size,
            candidates_considered=0,
            predicted_chain=[],
            model=model,
            quota_used_today=quota_used_today,
            quota_limit=quota_limit,
            note="no candidate tools — catalog empty or all listings unusable",
        )

    clean_tools, listing_lookup, skipped_dup, skipped_bad_name = filter_tools_for_anthropic(
        candidates
    )

    # Anthropic rejects tools=[] outright. If every candidate failed dedupe +
    # name validation, short-circuit before the API call so we don't burn a
    # quota slot on a guaranteed 400.
    if not clean_tools:
        return SimulationResult(
            offer_text=offer_text,
            catalog_size=catalog_size,
            candidates_considered=len(candidates),
            predicted_chain=[],
            model=model,
            quota_used_today=quota_used_today,
            quota_limit=quota_limit,
            note=(
                f"no candidate tools survived Anthropic schema validation "
                f"(skipped {skipped_dup} duplicate, {skipped_bad_name} non-conforming name)"
            ),
        )

    response = llm_call(SIMULATE_SYSTEM_PROMPT, clean_tools, offer_text)
    if response.error_note is not None:
        return SimulationResult(
            offer_text=offer_text,
            catalog_size=catalog_size,
            candidates_considered=len(candidates),
            predicted_chain=[],
            model=model,
            quota_used_today=quota_used_today,
            quota_limit=quota_limit,
            note=response.error_note,
        )

    chain: list[SimulatedToolCall] = []
    for block in response.tool_use_blocks:
        ref = listing_lookup.get(block.name, {})
        chain.append(
            SimulatedToolCall(
                tool_name=block.name,
                capability_key=block.name,
                listing_id=ref.get("listing_id", ""),
                listing_title=ref.get("listing_title", ""),
                args=dict(block.input or {}),
            )
        )

    if not chain:
        return SimulationResult(
            offer_text=offer_text,
            catalog_size=catalog_size,
            candidates_considered=len(candidates),
            predicted_chain=[],
            model=model,
            quota_used_today=quota_used_today,
            quota_limit=quota_limit,
            note="LLM picked no tools (offer may not match any catalog entry)",
        )

    return SimulationResult(
        offer_text=offer_text,
        catalog_size=catalog_size,
        candidates_considered=len(candidates),
        predicted_chain=chain,
        model=model,
        quota_used_today=quota_used_today,
        quota_limit=quota_limit,
        note=None,
    )
