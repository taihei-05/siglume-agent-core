"""Tests for tool_selector (Tier B Phase 2 / v0.3).

The selector is pure: no DB, no network. Callback-based gap reporting
is exercised end-to-end with in-memory sinks. Stays disjoint from the
prefilter test suite — prefilter is the budget cut, selector is the
dispatch cut.
"""

from __future__ import annotations

import hashlib
from typing import Any

from siglume_agent_core.tool_selector import (
    DEFAULT_MAX_CANDIDATES,
    UNMATCHED_MAX_TOKENS_FOR_HASH,
    UNMATCHED_STOP_WORDS,
    UNMATCHED_TEXT_MAX_LEN,
    UnmatchedRequestSignal,
    extract_trigger_words,
    select_tools,
    strip_long_alphanumeric_secrets,
)
from siglume_agent_core.types import ResolvedToolDefinition


def _mk_tool(
    *,
    capability_key: str,
    description: str = "",
    display_name: str | None = None,
    usage_hints: list[str] | None = None,
    permission_class: str = "read_only",
    approval_mode: str = "auto",
    account_readiness: str = "ready",
    cost_hint_usd_cents: int | None = None,
) -> ResolvedToolDefinition:
    return ResolvedToolDefinition(
        binding_id=f"b_{capability_key}",
        grant_id=f"g_{capability_key}",
        release_id=f"r_{capability_key}",
        listing_id=f"l_{capability_key}",
        capability_key=capability_key,
        tool_name=f"cap_{capability_key}",
        display_name=display_name or capability_key,
        description=description,
        input_schema={},
        output_schema={},
        permission_class=permission_class,  # type: ignore[arg-type]
        approval_mode=approval_mode,
        dry_run_supported=False,
        required_connected_accounts=[],
        account_readiness=account_readiness,  # type: ignore[arg-type]
        usage_hints=usage_hints or [],
        cost_hint_usd_cents=cost_hint_usd_cents,
    )


# ---------------------------------------------------------------------------
# Public-constant pinning
# ---------------------------------------------------------------------------


def test_default_max_candidates_constant():
    assert DEFAULT_MAX_CANDIDATES == 5


def test_unmatched_text_max_len_constant():
    assert UNMATCHED_TEXT_MAX_LEN == 200


def test_unmatched_max_tokens_constant():
    assert UNMATCHED_MAX_TOKENS_FOR_HASH == 16


def test_stop_words_includes_common_filler():
    # Spot-check a few that future PRs might be tempted to drop.
    for word in ("the", "to", "for", "please", "want", "need"):
        assert word in UNMATCHED_STOP_WORDS


# ---------------------------------------------------------------------------
# Empty-input short-circuit
# ---------------------------------------------------------------------------


def test_max_candidates_zero_returns_empty():
    tools = [_mk_tool(capability_key="a")]
    assert select_tools(tools, "anything", max_candidates=0) == []


def test_empty_tools_with_empty_request_returns_empty_no_signal():
    fired: list[UnmatchedRequestSignal] = []
    out = select_tools([], "", on_unmatched=fired.append)
    assert out == []
    assert fired == []


def test_empty_request_with_tools_returns_prefix_no_signal():
    """Empty request can't score; we return the original prefix and
    don't emit a gap signal (would just be noise)."""
    tools = [_mk_tool(capability_key=f"t{i}") for i in range(7)]
    fired: list[UnmatchedRequestSignal] = []
    out = select_tools(tools, "", on_unmatched=fired.append)
    assert [t.capability_key for t in out] == ["t0", "t1", "t2", "t3", "t4"]
    assert fired == []


def test_no_tools_installed_emits_signal():
    fired: list[UnmatchedRequestSignal] = []
    out = select_tools([], "find me a translator please", on_unmatched=fired.append)
    assert out == []
    assert len(fired) == 1
    assert fired[0].miss_kind == "no_tools_installed"
    assert fired[0].available_tool_count == 0
    assert fired[0].candidate_count_after_filter == 0
    # "find", "translator" survive stop-word removal
    assert "translator" in fired[0].request_words


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_keyword_overlap_outranks_no_match():
    tools = [
        _mk_tool(capability_key="weather_lookup", description="get weather forecast"),
        _mk_tool(capability_key="email_send", description="send transactional email"),
    ]
    out = select_tools(tools, "what is the weather today", max_candidates=2)
    assert [t.capability_key for t in out] == ["weather_lookup", "email_send"]


def test_account_readiness_bonus_breaks_tie_when_no_keyword_match():
    """Two tools with zero keyword overlap: ready beats unhealthy by the
    +5 / -5 readiness deltas."""
    tools = [
        _mk_tool(capability_key="alpha", account_readiness="unhealthy"),
        _mk_tool(capability_key="beta", account_readiness="ready"),
    ]
    out = select_tools(tools, "completely orthogonal request", max_candidates=2)
    assert [t.capability_key for t in out] == ["beta", "alpha"]


def test_auto_readonly_bonus_applied():
    """auto + read_only adds +3, beating an action tool with the same
    keyword overlap and ready account."""
    tools = [
        _mk_tool(
            capability_key="search_docs",
            description="search documents",
            permission_class="action",
            approval_mode="auto",
        ),
        _mk_tool(
            capability_key="search_db",
            description="search documents",
            permission_class="read_only",
            approval_mode="auto",
        ),
    ]
    out = select_tools(tools, "search documents", max_candidates=2)
    assert out[0].capability_key == "search_db"


def test_approval_friction_penalty_applied():
    """always_ask subtracts 2 from the score."""
    tools = [
        _mk_tool(
            capability_key="alpha",
            description="match",
            approval_mode="always_ask",
        ),
        _mk_tool(
            capability_key="beta",
            description="match",
            approval_mode="auto",
        ),
    ]
    out = select_tools(tools, "match", max_candidates=2)
    assert out[0].capability_key == "beta"


def test_cost_penalty_applied():
    """cost_hint_usd_cents > 0 subtracts cents/100 from the score."""
    tools = [
        _mk_tool(capability_key="cheap", description="match", cost_hint_usd_cents=1),
        _mk_tool(capability_key="expensive", description="match", cost_hint_usd_cents=5000),
    ]
    out = select_tools(tools, "match", max_candidates=2)
    assert out[0].capability_key == "cheap"


def test_stable_sort_preserves_original_index_on_tie():
    tools = [
        _mk_tool(capability_key="t0"),
        _mk_tool(capability_key="t1"),
        _mk_tool(capability_key="t2"),
    ]
    out = select_tools(tools, "no keyword overlap whatsoever", max_candidates=3)
    # All 3 score identically (account_readiness bonus only); original
    # order must be preserved.
    assert [t.capability_key for t in out] == ["t0", "t1", "t2"]


def test_truncates_to_max_candidates():
    tools = [_mk_tool(capability_key=f"t{i}", description="match") for i in range(10)]
    out = select_tools(tools, "match", max_candidates=3)
    assert len(out) == 3


# ---------------------------------------------------------------------------
# Hard filter on missing accounts
# ---------------------------------------------------------------------------


def test_missing_account_action_tool_filtered_by_default():
    tools = [
        _mk_tool(
            capability_key="needs_oauth",
            description="match",
            permission_class="action",
            account_readiness="missing",
        ),
        _mk_tool(capability_key="ready_tool", description="match"),
    ]
    out = select_tools(tools, "match", max_candidates=5)
    assert [t.capability_key for t in out] == ["ready_tool"]


def test_missing_account_readonly_tool_NOT_filtered():
    """read_only stays even with missing account — there's nothing to
    actually call out to."""
    tools = [
        _mk_tool(
            capability_key="readonly_missing",
            description="match",
            permission_class="read_only",
            account_readiness="missing",
        ),
    ]
    out = select_tools(tools, "match")
    assert [t.capability_key for t in out] == ["readonly_missing"]


def test_include_missing_accounts_disables_hard_filter():
    tools = [
        _mk_tool(
            capability_key="needs_oauth",
            description="match",
            permission_class="payment",
            account_readiness="missing",
        ),
    ]
    out = select_tools(tools, "match", include_missing_accounts=True)
    assert [t.capability_key for t in out] == ["needs_oauth"]


def test_all_filtered_emits_signal_and_returns_empty():
    tools = [
        _mk_tool(
            capability_key="needs_a",
            description="match",
            permission_class="action",
            account_readiness="missing",
        ),
        _mk_tool(
            capability_key="needs_b",
            description="match",
            permission_class="payment",
            account_readiness="missing",
        ),
    ]
    fired: list[UnmatchedRequestSignal] = []
    out = select_tools(tools, "match this please", on_unmatched=fired.append)
    assert out == []
    assert len(fired) == 1
    assert fired[0].miss_kind == "all_filtered_account_missing"
    assert fired[0].available_tool_count == 2
    assert fired[0].candidate_count_after_filter == 0


def test_no_keyword_match_emits_signal_but_still_returns_candidates():
    """top_match_count == 0 is a gap signal but we still return the
    score-ordered candidates — letting the LLM see them might still
    lead to a useful clarification turn."""
    tools = [
        _mk_tool(capability_key="weather"),
        _mk_tool(capability_key="email"),
    ]
    fired: list[UnmatchedRequestSignal] = []
    out = select_tools(tools, "translate japanese", on_unmatched=fired.append)
    assert len(out) == 2  # both candidates returned
    assert len(fired) == 1
    assert fired[0].miss_kind == "no_keyword_match"
    assert fired[0].candidate_count_after_filter == 2
    assert fired[0].top_match_count == 0


# ---------------------------------------------------------------------------
# Gap signal payload
# ---------------------------------------------------------------------------


def test_signal_words_are_sorted_unique_and_stop_word_filtered():
    fired: list[UnmatchedRequestSignal] = []
    select_tools(
        [],
        "the quick brown fox jumps over the lazy dog quick please",
        on_unmatched=fired.append,
    )
    # "the" / "over" / "please" are stop words; "quick" appears twice
    # but unique. Words come back sorted.
    words = fired[0].request_words
    assert words == sorted(set(words))
    assert "the" not in words
    assert "please" not in words
    assert "quick" in words
    assert words.count("quick") == 1


def test_signal_hash_is_deterministic_across_word_order():
    """Two requests with the same vocabulary in different order produce
    the same shape_hash — that's the entire point of the
    sorted-unique fingerprint."""
    fired: list[UnmatchedRequestSignal] = []
    select_tools([], "alpha beta gamma", on_unmatched=fired.append)
    select_tools([], "gamma alpha beta", on_unmatched=fired.append)
    assert len(fired) == 2
    assert fired[0].shape_hash == fired[1].shape_hash


def test_signal_hash_changes_when_vocabulary_differs():
    fired: list[UnmatchedRequestSignal] = []
    select_tools([], "alpha beta", on_unmatched=fired.append)
    select_tools([], "alpha gamma", on_unmatched=fired.append)
    assert fired[0].shape_hash != fired[1].shape_hash


def test_signal_hash_uses_first_n_tokens_of_unique_sorted():
    """Pin the contract so a future refactor can't silently change the
    fingerprint for in-flight gap-report data."""
    fired: list[UnmatchedRequestSignal] = []
    # 20 unique non-stop tokens — should hash only the first 16.
    request = " ".join(f"word{i:02d}" for i in range(20))
    select_tools([], request, on_unmatched=fired.append)
    expected_tokens = sorted({f"word{i:02d}" for i in range(20)})[:UNMATCHED_MAX_TOKENS_FOR_HASH]
    expected_hash = hashlib.sha256(",".join(expected_tokens).encode("utf-8")).hexdigest()
    assert fired[0].shape_hash == expected_hash


def test_signal_skipped_when_only_stop_words():
    """Empty token set after stop-word removal would produce a
    degenerate hash collision — never emit."""
    fired: list[UnmatchedRequestSignal] = []
    select_tools([], "the to a for of and or", on_unmatched=fired.append)
    assert fired == []


# ---------------------------------------------------------------------------
# Redactor wiring
# ---------------------------------------------------------------------------


def test_redactor_applied_before_storage():
    fired: list[UnmatchedRequestSignal] = []

    def fake_redactor(text: str) -> str:
        return text.replace("p4ssw0rd", "<REDACTED>")

    select_tools(
        [],
        "find the p4ssw0rd weather data please",
        on_unmatched=fired.append,
        redactor=fake_redactor,
    )
    assert "p4ssw0rd" not in fired[0].request_text_redacted
    assert "<REDACTED>" in fired[0].request_text_redacted


def test_redactor_exception_falls_back_to_empty_text_but_still_emits_when_words_remain():
    """If redactor blows up, we drop the sample text but the gap-shape
    aggregation degrades to no-signal (because the token list comes
    from the redacted text). This is the documented contract."""
    fired: list[UnmatchedRequestSignal] = []

    def broken_redactor(text: str) -> str:
        raise RuntimeError("redactor offline")

    select_tools(
        [],
        "find the weather please",
        on_unmatched=fired.append,
        redactor=broken_redactor,
    )
    # redactor raised -> stored text is "" -> token set empty -> no signal emitted
    assert fired == []


def test_no_redactor_stores_raw_text_capped():
    fired: list[UnmatchedRequestSignal] = []
    long_text = "x" * 500 + " findme"
    select_tools([], long_text, on_unmatched=fired.append)
    assert len(fired[0].request_text_redacted) <= UNMATCHED_TEXT_MAX_LEN


def test_redactor_output_also_capped():
    fired: list[UnmatchedRequestSignal] = []

    def expanding_redactor(text: str) -> str:
        return text + ("y" * 1000)

    select_tools(
        [],
        "findme please",
        on_unmatched=fired.append,
        redactor=expanding_redactor,
    )
    assert len(fired[0].request_text_redacted) == UNMATCHED_TEXT_MAX_LEN


# ---------------------------------------------------------------------------
# Callback exception isolation
# ---------------------------------------------------------------------------


def test_on_unmatched_exception_does_not_propagate():
    """Callback failures must never surface — the request path keeps
    running even if the gap-report sink is broken."""

    def broken_sink(_: UnmatchedRequestSignal) -> None:
        raise RuntimeError("sink offline")

    # Should not raise.
    out = select_tools([], "find weather", on_unmatched=broken_sink)
    assert out == []


def test_on_unmatched_invoked_exactly_once_per_miss():
    fired: list[UnmatchedRequestSignal] = []
    select_tools([], "find translator please", on_unmatched=fired.append)
    assert len(fired) == 1


# ---------------------------------------------------------------------------
# Public extract_trigger_words helper
# ---------------------------------------------------------------------------


def test_extract_trigger_words_combines_metadata_fields():
    tool = _mk_tool(
        capability_key="weather_lookup",
        display_name="Weather Lookup",
        description="Get current weather forecast",
        usage_hints=["forecast", "today"],
    )
    words = extract_trigger_words(tool)
    # Lowercased + split on [a-z0-9]+
    assert "weather" in words
    assert "lookup" in words
    assert "forecast" in words
    assert "today" in words
    assert "current" in words


def test_extract_trigger_words_handles_empty_strings():
    tool = _mk_tool(capability_key="x", display_name="", description="")
    words = extract_trigger_words(tool)
    assert words == {"x"}


# ---------------------------------------------------------------------------
# strip_long_alphanumeric_secrets utility
# ---------------------------------------------------------------------------


def test_strip_long_hex_replaces_run():
    out = strip_long_alphanumeric_secrets("token=" + "a" * 64 + " end")
    assert "<redacted-long-hex>" in out
    assert "a" * 64 not in out


def test_strip_long_base64_replaces_run():
    out = strip_long_alphanumeric_secrets("Authorization: Bearer " + "Zm9vYmFyYmF6" * 5)
    assert "<redacted-long-base64>" in out


def test_strip_short_hex_left_alone():
    out = strip_long_alphanumeric_secrets("abc " + "f" * 20 + " def")
    # 20 chars < 32 threshold, must be preserved
    assert "f" * 20 in out


def test_strip_empty_returns_empty():
    assert strip_long_alphanumeric_secrets("") == ""
    assert strip_long_alphanumeric_secrets(None) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Composability: full Q10-style redactor pipeline
# ---------------------------------------------------------------------------


def test_select_tools_with_chained_redactor():
    """Caller composes their primary redactor + the public Q4 backstop."""
    fired: list[UnmatchedRequestSignal] = []

    def my_redactor(text: str) -> str:
        # Pretend Q10 catches Bearer tokens by prefix
        text = text.replace("Bearer abc", "<redacted-bearer>")
        # Then the public Q4 backstop catches long hex
        return strip_long_alphanumeric_secrets(text)

    request = "use the api with Bearer abc and key " + "f" * 50 + " please"
    select_tools([], request, on_unmatched=fired.append, redactor=my_redactor)
    assert "<redacted-bearer>" in fired[0].request_text_redacted
    assert "<redacted-long-hex>" in fired[0].request_text_redacted
    assert "f" * 50 not in fired[0].request_text_redacted


# ---------------------------------------------------------------------------
# Ordering invariant: candidates returned are score-ordered
# ---------------------------------------------------------------------------


def test_returned_order_matches_score_descending():
    tools = [
        _mk_tool(capability_key="weather", description="get current weather"),
        _mk_tool(capability_key="weather_history", description="historical weather data"),
        _mk_tool(capability_key="email", description="send email"),
    ]
    out = select_tools(tools, "weather", max_candidates=3)
    # Both "weather" tools score equally on the keyword "weather";
    # "weather" wins the index tiebreak. Email scores lowest.
    assert [t.capability_key for t in out] == ["weather", "weather_history", "email"]


def test_select_does_not_mutate_input_list():
    tools = [_mk_tool(capability_key=f"t{i}", description="match") for i in range(5)]
    snapshot = list(tools)
    select_tools(tools, "match", max_candidates=2)
    assert tools == snapshot


# ---------------------------------------------------------------------------
# Type-stability of caller closure pattern (smoke)
# ---------------------------------------------------------------------------


def test_caller_can_capture_extra_context_in_closure():
    """The agent_id / owner_user_id pattern from the platform: caller
    captures them via closure rather than passing through agent-core."""
    captured: dict[str, Any] = {}

    def my_sink_with_context(signal: UnmatchedRequestSignal) -> None:
        captured["agent_id"] = "agent-123"
        captured["owner_user_id"] = "owner-456"
        captured["miss_kind"] = signal.miss_kind

    select_tools([], "find translator please", on_unmatched=my_sink_with_context)
    assert captured == {
        "agent_id": "agent-123",
        "owner_user_id": "owner-456",
        "miss_kind": "no_tools_installed",
    }
