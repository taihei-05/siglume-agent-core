"""Tests for siglume_agent_core.dev_simulator (Tier C Phase 3 — v0.7).

Pins the byte-equivalent contract with the monorepo's
``packages/shared-python/agent_sns/application/capability_runtime/dev_simulator.py``:
note strings, dedupe order, regex patterns, scoring formula, fallback
chains, 600-char description clip, ``required`` pruning.

The platform shim wraps Anthropic; here the LLM call is faked via a
plain callable so the tests exercise the full ``simulate_planner``
control flow without any network / SDK dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from siglume_agent_core.dev_simulator import (
    ANTHROPIC_PROPERTY_KEY_RE,
    ANTHROPIC_TOOL_NAME_RE,
    SIMULATE_MODEL,
    SIMULATE_SYSTEM_PROMPT,
    STOP_WORDS,
    LLMSimulateResponse,
    LLMSimulateToolUseBlock,
    SimulatedToolCall,
    SimulationResult,
    build_tool_def,
    extract_keywords,
    filter_tools_for_anthropic,
    sanitize_input_schema_for_anthropic,
    score_candidate,
    select_candidates,
    simulate_planner,
)

# ---------------------------------------------------------------------------
# Fake catalog row types — satisfy ProductListingLike / CapabilityReleaseLike
# structurally without importing SQLAlchemy.
# ---------------------------------------------------------------------------


@dataclass
class FakeListing:
    id: str = "listing-x"
    title: str | None = "Listing"
    description: str | None = None
    capability_key: str | None = None


@dataclass
class FakeRelease:
    tool_manual_jsonb: Any = field(default_factory=dict)
    tool_prompt_compact: Any = None
    input_schema_jsonb: Any = None


# ---------------------------------------------------------------------------
# Constants — pin exact values byte-equivalent with the monorepo.
# ---------------------------------------------------------------------------


def test_simulate_model_constant():
    assert SIMULATE_MODEL == "claude-haiku-4-5-20251001"


def test_simulate_system_prompt_byte_equivalent():
    """The exact prompt the monorepo shipped pre-extraction. Drift here
    means the LLM gets a different instruction and the simulator's
    output distribution shifts silently."""
    expected = (
        "You are a dev simulator for the Siglume API Store planner. Given the "
        "user's offer text, decide which of the provided tools to call and in "
        "what order to fulfill it. Use tool_use blocks for your selection. "
        "Pick AS FEW tools as needed. Do not execute the tools yourself — "
        "tool_use blocks here are PLAN ONLY."
    )
    assert SIMULATE_SYSTEM_PROMPT == expected


def test_stop_words_set_byte_equivalent():
    """Sample of the closed list — drift means scoring distribution moves."""
    # spot-check a representative subset; full equality is pinned through
    # the score_candidate / extract_keywords behavior tests below.
    assert "the" in STOP_WORDS
    assert "please" in STOP_WORDS
    assert "translate" not in STOP_WORDS  # action verbs are NOT stopped
    assert "post" not in STOP_WORDS
    assert "to" in STOP_WORDS


def test_anthropic_property_key_re_pattern():
    """Pinned pattern. Anthropic 400s the entire tool_use call when even
    one property key violates this — never relax without coordinating."""
    assert ANTHROPIC_PROPERTY_KEY_RE.pattern == r"^[a-zA-Z0-9_.-]{1,64}$"
    assert ANTHROPIC_PROPERTY_KEY_RE.match("good_key_1.dot-allowed")
    assert ANTHROPIC_PROPERTY_KEY_RE.match("a")
    assert not ANTHROPIC_PROPERTY_KEY_RE.match("")
    assert not ANTHROPIC_PROPERTY_KEY_RE.match("bad space")
    assert not ANTHROPIC_PROPERTY_KEY_RE.match("non-ascii-£")
    assert not ANTHROPIC_PROPERTY_KEY_RE.match("a" * 65)


def test_anthropic_tool_name_re_pattern():
    """Stricter than the property-key regex (no '.'). Tool names that
    violate this would cause Anthropic to reject the WHOLE tool_use
    call — same blast-radius as the property regex."""
    assert ANTHROPIC_TOOL_NAME_RE.pattern == r"^[a-zA-Z0-9_-]{1,64}$"
    assert ANTHROPIC_TOOL_NAME_RE.match("post_to_slack")
    assert ANTHROPIC_TOOL_NAME_RE.match("a-b-c")
    assert not ANTHROPIC_TOOL_NAME_RE.match("with.dot")  # crucial: no dots
    assert not ANTHROPIC_TOOL_NAME_RE.match("has space")
    assert not ANTHROPIC_TOOL_NAME_RE.match("")


# ---------------------------------------------------------------------------
# extract_keywords
# ---------------------------------------------------------------------------


def test_extract_keywords_lowercases_and_strips_stopwords():
    out = extract_keywords("Please TRANSLATE this English text to Japanese")
    # 'please' / 'this' / 'to' / 'the' are stopped; lowercase
    assert out == {"translate", "english", "text", "japanese"}


def test_extract_keywords_drops_non_alnum_separators():
    """Regex is `[a-z0-9]+` after lowercase — punctuation / non-ASCII split."""
    out = extract_keywords("post-to-notion v2.0!!")
    # split on '-', '.', ' ', '!' — note 'to' is a stop word
    assert out == {"post", "notion", "v2", "0"}


def test_extract_keywords_empty_text():
    assert extract_keywords("") == set()
    assert extract_keywords("   ") == set()


# ---------------------------------------------------------------------------
# sanitize_input_schema_for_anthropic
# ---------------------------------------------------------------------------


def test_sanitize_input_schema_returns_default_for_non_dict():
    out = sanitize_input_schema_for_anthropic([])  # type: ignore[arg-type]
    assert out == {"type": "object", "properties": {}}


def test_sanitize_input_schema_drops_bad_keys_and_prunes_required():
    schema = {
        "type": "object",
        "properties": {
            "good_key": {"type": "string"},
            "bad key": {"type": "string"},  # space
            "with.dot": {"type": "string"},  # allowed by property-key regex
            "non-ascii-£": {"type": "string"},  # bad
        },
        "required": ["good_key", "bad key", "non-ascii-£"],
    }
    out = sanitize_input_schema_for_anthropic(schema)
    assert "good_key" in out["properties"]
    assert "with.dot" in out["properties"]  # dots ARE allowed for property keys
    assert "bad key" not in out["properties"]
    assert "non-ascii-£" not in out["properties"]
    # required pruned to drop only the bad-keyed entries
    assert out["required"] == ["good_key"]


def test_sanitize_input_schema_recurses_into_nested_properties():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {
                    "ok_inner": {"type": "string"},
                    "bad inner": {"type": "string"},
                },
            },
        },
    }
    out = sanitize_input_schema_for_anthropic(schema)
    assert "ok_inner" in out["properties"]["outer"]["properties"]
    assert "bad inner" not in out["properties"]["outer"]["properties"]


def test_sanitize_input_schema_does_not_mutate_caller():
    """Critical: the platform may keep a reference to the original
    schema (e.g. for logging). Mutation would corrupt persistent state."""
    schema = {
        "type": "object",
        "properties": {"good": {"type": "string"}, "bad space": {"type": "string"}},
    }
    snapshot = {"properties": dict(schema["properties"])}
    sanitize_input_schema_for_anthropic(schema)
    assert schema["properties"] == snapshot["properties"]


def test_sanitize_input_schema_handles_missing_properties():
    out = sanitize_input_schema_for_anthropic({"type": "object"})
    assert out == {"type": "object", "properties": {}}


def test_sanitize_input_schema_handles_required_with_no_dropped_keys():
    """When no keys are dropped, ``required`` is preserved untouched."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a", "b"],
    }
    out = sanitize_input_schema_for_anthropic(schema)
    assert out["required"] == ["a", "b"]


# ---------------------------------------------------------------------------
# build_tool_def
# ---------------------------------------------------------------------------


def test_build_tool_def_returns_none_when_capability_key_missing():
    assert (
        build_tool_def(
            FakeListing(capability_key=None),
            FakeRelease(tool_manual_jsonb={}),
        )
        is None
    )


def test_build_tool_def_picks_capability_key_from_manual_first():
    out = build_tool_def(
        FakeListing(capability_key="from_listing"),
        FakeRelease(tool_manual_jsonb={"capability_key": "from_manual"}),
    )
    assert out is not None
    assert out["name"] == "from_manual"


def test_build_tool_def_falls_back_to_listing_capability_key():
    out = build_tool_def(
        FakeListing(capability_key="from_listing"),
        FakeRelease(tool_manual_jsonb={}),
    )
    assert out is not None
    assert out["name"] == "from_listing"


def test_build_tool_def_description_fallback_chain():
    """Pin every step of the fallback order:
    tool_prompt_compact > manual.compact_prompt > manual.description >
    manual.summary_for_model > listing.description > listing.title >
    capability_key. Each step is exercised with all earlier steps removed
    so a future swap in adjacent steps is caught."""
    # tool_prompt_compact wins over everything below
    out = build_tool_def(
        FakeListing(capability_key="cap", title="t", description="ld"),
        FakeRelease(
            tool_prompt_compact="tpc",
            tool_manual_jsonb={
                "compact_prompt": "cp",
                "description": "md",
                "summary_for_model": "sfm",
            },
        ),
    )
    assert out and out["description"] == "tpc"

    # tool_prompt_compact missing → manual.compact_prompt
    out = build_tool_def(
        FakeListing(capability_key="cap", title="t", description="ld"),
        FakeRelease(
            tool_manual_jsonb={
                "compact_prompt": "cp",
                "description": "md",
                "summary_for_model": "sfm",
            },
        ),
    )
    assert out and out["description"] == "cp"

    # compact_prompt missing → manual.description
    out = build_tool_def(
        FakeListing(capability_key="cap", title="t", description="ld"),
        FakeRelease(
            tool_manual_jsonb={
                "description": "md",
                "summary_for_model": "sfm",
            },
        ),
    )
    assert out and out["description"] == "md"

    # manual.description missing → manual.summary_for_model
    out = build_tool_def(
        FakeListing(capability_key="cap", title="t", description="ld"),
        FakeRelease(tool_manual_jsonb={"summary_for_model": "sfm"}),
    )
    assert out and out["description"] == "sfm"

    # all manual missing → listing.description
    out = build_tool_def(
        FakeListing(capability_key="cap", title="t", description="ld"),
        FakeRelease(tool_manual_jsonb={}),
    )
    assert out and out["description"] == "ld"

    # manual + listing.description missing → listing.title
    out = build_tool_def(
        FakeListing(capability_key="cap", title="title-only", description=None),
        FakeRelease(tool_manual_jsonb={}),
    )
    assert out and out["description"] == "title-only"

    # everything missing → capability_key as last resort
    out = build_tool_def(
        FakeListing(capability_key="cap", title=None, description=None),
        FakeRelease(tool_manual_jsonb={}),
    )
    assert out and out["description"] == "cap"


def test_build_tool_def_clips_description_to_600_chars():
    long = "x" * 1000
    out = build_tool_def(
        FakeListing(capability_key="cap"),
        FakeRelease(tool_prompt_compact=long),
    )
    assert out and len(out["description"]) == 600


def test_build_tool_def_fills_missing_schema_type_and_properties():
    # input_schema empty dict
    out = build_tool_def(
        FakeListing(capability_key="cap"),
        FakeRelease(input_schema_jsonb={}),
    )
    assert out
    assert out["input_schema"]["type"] == "object"
    assert out["input_schema"]["properties"] == {}


def test_build_tool_def_handles_non_dict_schema():
    out = build_tool_def(
        FakeListing(capability_key="cap"),
        FakeRelease(input_schema_jsonb="not a dict"),
    )
    assert out
    assert out["input_schema"] == {"type": "object", "properties": {}}


def test_build_tool_def_falls_back_to_manual_input_schema():
    out = build_tool_def(
        FakeListing(capability_key="cap"),
        FakeRelease(
            input_schema_jsonb=None,
            tool_manual_jsonb={
                "capability_key": "cap",
                "input_schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                },
            },
        ),
    )
    assert out
    assert out["input_schema"]["properties"] == {"name": {"type": "string"}}


def test_build_tool_def_emits_listing_metadata():
    out = build_tool_def(
        FakeListing(id="listing-42", title="Translate"),
        FakeRelease(tool_manual_jsonb={"capability_key": "cap"}),
    )
    assert out
    assert out["_listing_id"] == "listing-42"
    assert out["_listing_title"] == "Translate"


# ---------------------------------------------------------------------------
# score_candidate
# ---------------------------------------------------------------------------


def test_score_candidate_counts_overlap_with_offer_keywords():
    tool = {"name": "translate_text", "description": "Translate to Japanese"}
    keywords = {"translate", "japanese"}
    # 'translate' + 'translate' (in name AND desc) dedupes to set, score = 2
    # tokens: name 'translate' 'text' / desc 'translate' 'japanese' → set {translate, text, japanese}
    # overlap with {translate, japanese} = 2
    assert score_candidate(tool, keywords) == 2


def test_score_candidate_zero_when_no_overlap():
    tool = {"name": "ping", "description": "echo"}
    assert score_candidate(tool, {"translate", "japanese"}) == 0


def test_score_candidate_handles_missing_fields():
    """Tool dicts with name=None / description=None must not crash."""
    tool: dict[str, Any] = {"name": None, "description": None}
    assert score_candidate(tool, {"any"}) == 0


# ---------------------------------------------------------------------------
# select_candidates
# ---------------------------------------------------------------------------


def _row(
    capability_key: str, description: str = "", title: str = "T"
) -> tuple[FakeListing, FakeRelease]:
    return (
        FakeListing(id=f"id-{capability_key}", title=title, capability_key=capability_key),
        FakeRelease(
            tool_manual_jsonb={"capability_key": capability_key},
            tool_prompt_compact=description,
        ),
    )


def test_select_candidates_orders_by_score_desc():
    rows = [
        _row("post_slack", "post message to slack"),
        _row("translate", "translate text to japanese"),
        _row("ping", "echo back"),
    ]
    candidates = select_candidates(
        rows,
        offer_text="translate to japanese",
        max_candidates=10,
    )
    # 'translate' tool scores highest on the offer
    assert candidates[0]["name"] == "translate"


def test_select_candidates_truncates_to_max():
    rows = [_row(f"cap_{i}", f"desc {i}") for i in range(20)]
    candidates = select_candidates(
        rows,
        offer_text="anything",
        max_candidates=5,
    )
    assert len(candidates) == 5


def test_select_candidates_clamps_max_to_one_minimum():
    """``max(1, max_candidates)`` — a caller passing 0 still gets 1."""
    rows = [_row("a"), _row("b")]
    candidates = select_candidates(rows, offer_text="x", max_candidates=0)
    assert len(candidates) == 1


def test_select_candidates_drops_listings_without_capability_key():
    rows: list[tuple[FakeListing, FakeRelease]] = [
        (FakeListing(capability_key=None), FakeRelease(tool_manual_jsonb={})),
        _row("good"),
    ]
    candidates = select_candidates(rows, offer_text="x", max_candidates=10)
    assert len(candidates) == 1
    assert candidates[0]["name"] == "good"


def test_select_candidates_empty_rows_returns_empty():
    assert select_candidates([], offer_text="x", max_candidates=10) == []


# ---------------------------------------------------------------------------
# filter_tools_for_anthropic
# ---------------------------------------------------------------------------


def _candidate(name: str, listing_id: str = "L", title: str = "T") -> dict[str, Any]:
    return {
        "name": name,
        "description": f"desc-{name}",
        "input_schema": {"type": "object", "properties": {}},
        "_listing_id": listing_id,
        "_listing_title": title,
    }


def test_filter_tools_dedupes_by_name_first_wins():
    candidates = [
        _candidate("post_slack", listing_id="A", title="A-listing"),
        _candidate("post_slack", listing_id="B", title="B-listing"),
        _candidate("translate"),
    ]
    clean, lookup, dup, bad = filter_tools_for_anthropic(candidates)
    assert [t["name"] for t in clean] == ["post_slack", "translate"]
    assert lookup["post_slack"] == {"listing_id": "A", "listing_title": "A-listing"}
    assert dup == 1
    assert bad == 0


def test_filter_tools_skips_bad_names():
    candidates = [
        _candidate("good_name"),
        _candidate("bad.name"),  # dots disallowed in tool name
        _candidate("has space"),
    ]
    clean, lookup, dup, bad = filter_tools_for_anthropic(candidates)
    assert [t["name"] for t in clean] == ["good_name"]
    assert "bad.name" not in lookup
    assert dup == 0
    assert bad == 2


def test_filter_tools_strips_private_listing_keys():
    """``clean_tools`` must NOT carry ``_listing_id`` / ``_listing_title``
    — Anthropic would 400 on unknown fields in tool definitions."""
    candidates = [_candidate("ok")]
    clean, _, _, _ = filter_tools_for_anthropic(candidates)
    assert set(clean[0].keys()) == {"name", "description", "input_schema"}


def test_filter_tools_empty_input():
    clean, lookup, dup, bad = filter_tools_for_anthropic([])
    assert clean == []
    assert lookup == {}
    assert dup == 0
    assert bad == 0


# ---------------------------------------------------------------------------
# simulate_planner — full control flow with a fake LLM callable.
# ---------------------------------------------------------------------------


def _ok_llm_picks(*names: str):
    """Build a fake LLM callable that picks the named tools."""

    def call(system_prompt: str, tools: list[dict[str, Any]], user_msg: str) -> LLMSimulateResponse:
        assert system_prompt == SIMULATE_SYSTEM_PROMPT
        return LLMSimulateResponse(
            tool_use_blocks=[LLMSimulateToolUseBlock(name=n, input={"arg": n}) for n in names],
        )

    return call


def _empty_llm():
    def call(system_prompt: str, tools: list[dict[str, Any]], user_msg: str) -> LLMSimulateResponse:
        # Catch any drift in SIMULATE_SYSTEM_PROMPT on the empty-pick branch
        # too — not just on the happy path.
        assert system_prompt == SIMULATE_SYSTEM_PROMPT
        return LLMSimulateResponse(tool_use_blocks=[])

    return call


def _erroring_llm(note: str):
    def call(system_prompt: str, tools: list[dict[str, Any]], user_msg: str) -> LLMSimulateResponse:
        assert system_prompt == SIMULATE_SYSTEM_PROMPT
        return LLMSimulateResponse(tool_use_blocks=[], error_note=note)

    return call


def _never_called_llm():
    def call(system_prompt: str, tools: list[dict[str, Any]], user_msg: str) -> LLMSimulateResponse:
        raise AssertionError("LLM should not be called when short-circuited")

    return call


def test_simulate_planner_empty_offer_text_short_circuits():
    result = simulate_planner(
        rows=[_row("anything")],
        offer_text="   ",
        quota_used_today=1,
        quota_limit=10,
        llm_call=_never_called_llm(),
    )
    assert result.note == "empty offer_text"
    assert result.predicted_chain == []
    assert result.catalog_size == 0
    assert result.candidates_considered == 0
    # offer_text on the result is the empty string when input was blank
    assert result.offer_text == ""


def test_simulate_planner_no_candidates_short_circuits():
    """When every row fails build_tool_def (no capability_key), the
    note pins the monorepo's exact wording."""
    rows: list[tuple[FakeListing, FakeRelease]] = [
        (FakeListing(capability_key=None), FakeRelease(tool_manual_jsonb={})),
    ]
    result = simulate_planner(
        rows=rows,
        offer_text="translate this",
        quota_used_today=0,
        quota_limit=10,
        llm_call=_never_called_llm(),
    )
    assert result.note == ("no candidate tools — catalog empty or all listings unusable")
    assert result.predicted_chain == []
    assert result.candidates_considered == 0
    assert result.catalog_size == 1


def test_simulate_planner_all_filtered_short_circuits_with_counters():
    """When every candidate is deduped or bad-named, note carries the
    skipped_dup / skipped_bad_name counters in exact monorepo wording."""
    rows = [
        _row("bad.name"),  # dots disallowed in tool name → bad
        _row("also bad name"),  # space → bad
    ]
    result = simulate_planner(
        rows=rows,
        offer_text="anything",
        quota_used_today=0,
        quota_limit=10,
        llm_call=_never_called_llm(),
    )
    assert result.note == (
        "no candidate tools survived Anthropic schema validation "
        "(skipped 0 duplicate, 2 non-conforming name)"
    )
    assert result.predicted_chain == []
    assert result.candidates_considered == 2


def test_simulate_planner_propagates_llm_error_note():
    rows = [_row("translate", "translate to japanese")]
    result = simulate_planner(
        rows=rows,
        offer_text="translate this please",
        quota_used_today=0,
        quota_limit=10,
        llm_call=_erroring_llm("anthropic SDK not available"),
    )
    assert result.note == "anthropic SDK not available"
    assert result.predicted_chain == []


def test_simulate_planner_llm_picks_no_tools_emits_note():
    rows = [_row("translate", "translate to japanese")]
    result = simulate_planner(
        rows=rows,
        offer_text="translate this please",
        quota_used_today=0,
        quota_limit=10,
        llm_call=_empty_llm(),
    )
    assert result.note == ("LLM picked no tools (offer may not match any catalog entry)")
    assert result.predicted_chain == []


def test_simulate_planner_happy_path_builds_chain():
    rows = [
        _row("translate", "translate text to japanese", title="Translator"),
        _row("post_slack", "post message to slack"),
    ]
    result = simulate_planner(
        rows=rows,
        offer_text="translate this please",
        quota_used_today=2,
        quota_limit=10,
        llm_call=_ok_llm_picks("translate"),
    )
    assert result.note is None
    assert result.predicted_chain == [
        SimulatedToolCall(
            tool_name="translate",
            capability_key="translate",
            listing_id="id-translate",
            listing_title="Translator",
            args={"arg": "translate"},
        )
    ]
    assert result.quota_used_today == 2
    assert result.quota_limit == 10
    assert result.catalog_size == 2
    assert result.candidates_considered == 2
    assert result.model == SIMULATE_MODEL


def test_simulate_planner_uses_default_model_constant():
    rows = [_row("translate")]
    result = simulate_planner(
        rows=rows,
        offer_text="translate",
        quota_used_today=0,
        quota_limit=1,
        llm_call=_empty_llm(),
    )
    assert result.model == SIMULATE_MODEL


def test_simulate_planner_respects_explicit_model_override():
    rows = [_row("translate")]
    result = simulate_planner(
        rows=rows,
        offer_text="translate",
        quota_used_today=0,
        quota_limit=1,
        llm_call=_empty_llm(),
        model="custom-model-id",
    )
    assert result.model == "custom-model-id"


def test_simulate_planner_max_candidates_passes_through():
    rows = [_row(f"cap_{i}", f"desc {i}") for i in range(20)]
    captured_tools: list[list[dict[str, Any]]] = []

    def call(system_prompt: str, tools: list[dict[str, Any]], user_msg: str) -> LLMSimulateResponse:
        captured_tools.append(tools)
        return LLMSimulateResponse(tool_use_blocks=[])

    simulate_planner(
        rows=rows,
        offer_text="cap_0",
        quota_used_today=0,
        quota_limit=10,
        llm_call=call,
        max_candidates=3,
    )
    assert len(captured_tools[0]) == 3


def test_simulate_planner_unknown_tool_name_in_response_yields_blank_listing():
    """If the LLM hallucinates a tool name that didn't survive
    filter_tools_for_anthropic, listing_lookup is empty for that name —
    SimulatedToolCall.listing_id falls back to ''. This mirrors the
    monorepo's defensive `ref.get("listing_id", "")` behavior."""
    rows = [_row("translate")]
    result = simulate_planner(
        rows=rows,
        offer_text="anything",
        quota_used_today=0,
        quota_limit=10,
        llm_call=_ok_llm_picks("hallucinated_tool"),
    )
    assert len(result.predicted_chain) == 1
    assert result.predicted_chain[0].listing_id == ""
    assert result.predicted_chain[0].listing_title == ""
    assert result.predicted_chain[0].tool_name == "hallucinated_tool"


def test_simulate_planner_empty_input_dict_yields_empty_args():
    """An LLM tool_use block with ``input={}`` produces a SimulatedToolCall
    with ``args={}`` (no spurious entries appended). Note ``input`` is
    typed ``dict[str, Any]`` on the dataclass so the literal ``None``
    case can't be passed; the runtime ``or {}`` defense is belt-and-
    suspenders for callers ignoring the type."""
    rows = [_row("translate")]

    def call(system_prompt: str, tools: list[dict[str, Any]], user_msg: str) -> LLMSimulateResponse:
        return LLMSimulateResponse(
            tool_use_blocks=[LLMSimulateToolUseBlock(name="translate", input={})],
        )

    result = simulate_planner(
        rows=rows,
        offer_text="translate this",
        quota_used_today=0,
        quota_limit=10,
        llm_call=call,
    )
    assert result.predicted_chain[0].args == {}


# ---------------------------------------------------------------------------
# Frozen / mutable invariants on the dataclasses
# ---------------------------------------------------------------------------


def test_llm_simulate_response_is_frozen():
    resp = LLMSimulateResponse(tool_use_blocks=[])
    with pytest.raises(Exception):
        resp.error_note = "no"  # type: ignore[misc]


def test_llm_simulate_tool_use_block_is_frozen():
    block = LLMSimulateToolUseBlock(name="t", input={})
    with pytest.raises(Exception):
        block.name = "renamed"  # type: ignore[misc]


def test_simulated_tool_call_is_mutable_for_byte_equivalence():
    """The monorepo's ``SimulatedToolCall`` is a regular (non-frozen)
    dataclass. Pin that — flipping to frozen could break downstream
    callers that mutate ``.args``."""
    call = SimulatedToolCall(
        tool_name="t",
        capability_key="t",
        listing_id="i",
        listing_title="ti",
    )
    call.args["k"] = "v"  # must not raise
    assert call.args == {"k": "v"}


def test_simulation_result_is_mutable():
    """Same for SimulationResult — the platform's API layer mutates
    fields like ``predicted_chain`` after returning from simulate."""
    result = SimulationResult(
        offer_text="x",
        catalog_size=0,
        candidates_considered=0,
        predicted_chain=[],
        model=SIMULATE_MODEL,
        quota_used_today=0,
        quota_limit=10,
    )
    result.predicted_chain.append(
        SimulatedToolCall(tool_name="t", capability_key="t", listing_id="", listing_title="")
    )  # must not raise
    assert len(result.predicted_chain) == 1
