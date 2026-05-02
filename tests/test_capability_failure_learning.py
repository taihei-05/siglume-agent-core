"""Tests for ``siglume_agent_core.capability_failure_learning``.

Pure-function unit tests; mirrors the platform's expected outputs so the
public package and the platform's behaviour stay byte-equivalent.
"""

from __future__ import annotations

import datetime as dt

import pytest

from siglume_agent_core.capability_failure_learning import (
    CAPABILITY_LEARNING_TAG,
    CAPABILITY_PREFERENCE_MEMORY_TYPE,
    SYSTEM_PROMPT_OVERFLOW_KIND,
    api_outcome_from_execution,
    api_outcome_from_output,
    build_learning_content,
    build_system_prompt_overflow_content,
    clip_text,
    failure_kind_from_execution,
    infer_capability_task_family,
    last_tool_output_from_steps,
    learning_expiry_for_kind,
    learning_scores_for_kind,
)
from siglume_agent_core.types import ResolvedToolDefinition


def _make_tool(display_name: str = "Demo Tool") -> ResolvedToolDefinition:
    return ResolvedToolDefinition(
        binding_id="b",
        grant_id="g",
        release_id="r",
        listing_id="l",
        capability_key="demo",
        tool_name="cap_demo",
        display_name=display_name,
        description="x",
        input_schema={},
        output_schema={},
        permission_class="read_only",
        approval_mode="auto",
        dry_run_supported=False,
        required_connected_accounts=[],
        account_readiness="ready",
    )


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_constants_match_platform_values():
    """The three string constants are part of the public contract — the
    platform writes/queries DB rows by these literal values, so a rename
    here would silently invalidate every existing memory card."""
    assert CAPABILITY_PREFERENCE_MEMORY_TYPE == "capability_preference"
    assert CAPABILITY_LEARNING_TAG == "capability_failure_learning"
    assert SYSTEM_PROMPT_OVERFLOW_KIND == "system_prompt_overflow"


# ---------------------------------------------------------------------------
# clip_text
# ---------------------------------------------------------------------------


def test_clip_text_collapses_whitespace_and_strips():
    assert clip_text("  hello\n\nworld\t!  ") == "hello world !"


def test_clip_text_returns_empty_for_none_or_empty():
    assert clip_text(None) == ""
    assert clip_text("") == ""
    assert clip_text("   \n\t  ") == ""


def test_clip_text_passes_through_under_cap():
    assert clip_text("abc", max_chars=10) == "abc"


def test_clip_text_truncates_with_ellipsis_and_rstrips():
    out = clip_text("a b c d e f g h", max_chars=5)
    assert out.endswith("...")
    assert len(out) <= 5 + 3
    # rstrip happens BEFORE the ellipsis is appended — when text[:N] ends
    # with whitespace, the trailing space is stripped so the ellipsis sits
    # flush against the last word.
    assert clip_text("aaaaa bbbbb cccc dddd", max_chars=6) == "aaaaa..."


def test_clip_text_coerces_non_string_inputs():
    assert clip_text(12345, max_chars=10) == "12345"
    assert clip_text({"a": 1}, max_chars=20) == "{'a': 1}"


# ---------------------------------------------------------------------------
# api_outcome_from_output
# ---------------------------------------------------------------------------


def test_api_outcome_from_output_success_path():
    assert api_outcome_from_output({"result": "ok"}) == "success"
    assert api_outcome_from_output({"match_type": "exact"}) == "success"


def test_api_outcome_from_output_fallback_used_flag():
    assert api_outcome_from_output({"fallback_used": True}) == "out_of_coverage"
    # Non-True values do NOT trigger out_of_coverage (truthy strings, etc.)
    assert api_outcome_from_output({"fallback_used": "yes"}) == "success"
    assert api_outcome_from_output({"fallback_used": 1}) == "success"


def test_api_outcome_from_output_match_type_signals():
    assert api_outcome_from_output({"match_type": "fallback"}) == "out_of_coverage"
    assert api_outcome_from_output({"match_type": "identity"}) == "out_of_coverage"
    # Case + whitespace tolerance
    assert api_outcome_from_output({"match_type": "  Fallback  "}) == "out_of_coverage"


def test_api_outcome_from_output_handles_none_and_non_dict():
    assert api_outcome_from_output(None) == "success"
    assert api_outcome_from_output("not a dict") == "success"  # type: ignore[arg-type]
    assert api_outcome_from_output([]) == "success"  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# last_tool_output_from_steps
# ---------------------------------------------------------------------------


def test_last_tool_output_walks_in_reverse():
    steps = [
        {"output": {"first": True}},
        {"output": {"second": True}},
        {"output": {"third": True}},
    ]
    assert last_tool_output_from_steps(steps) == {"third": True}


def test_last_tool_output_skips_non_dict_steps_and_missing_outputs():
    steps = [
        {"output": {"first": True}},
        "not a dict",  # type: ignore[list-item]
        {"no_output_key": "x"},
        {"output": "not a dict either"},
    ]
    # Only the very-first step has a dict output; everything more recent is invalid
    assert last_tool_output_from_steps(steps) == {"first": True}


def test_last_tool_output_returns_empty_for_none_or_invalid():
    assert last_tool_output_from_steps(None) == {}
    assert last_tool_output_from_steps([]) == {}
    assert last_tool_output_from_steps("not a list") == {}  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# api_outcome_from_execution
# ---------------------------------------------------------------------------


def test_api_outcome_from_execution_prefers_last_tool_output():
    structured = {"last_tool_output": {"fallback_used": True}, "match_type": "exact"}
    assert api_outcome_from_execution(structured_output=structured) == "out_of_coverage"


def test_api_outcome_from_execution_falls_back_to_structured_root():
    structured = {"match_type": "fallback"}
    assert api_outcome_from_execution(structured_output=structured) == "out_of_coverage"


def test_api_outcome_from_execution_walks_step_results_in_reverse():
    """The walk is reverse but it returns the FIRST non-success encountered
    in that walk — i.e. any out_of_coverage anywhere in the chain surfaces.
    Pin both orderings so a future refactor can't silently flip from
    "any-non-success" to "most-recent-only" semantics."""
    # Newest step is success, older one is fallback — walk hits success
    # first (skipped because it's success), then the older fallback wins.
    steps = [
        {"output": {"match_type": "fallback"}},
        {"output": {"result": "ok"}},
    ]
    assert (
        api_outcome_from_execution(structured_output=None, step_results=steps) == "out_of_coverage"
    )
    # And when the newer step itself is the fallback, that one surfaces.
    steps_flipped = [
        {"output": {"result": "ok"}},
        {"output": {"match_type": "fallback"}},
    ]
    assert (
        api_outcome_from_execution(structured_output=None, step_results=steps_flipped)
        == "out_of_coverage"
    )
    # All-success → success.
    steps_all_ok = [
        {"output": {"result": "ok"}},
        {"output": {"result": "fine"}},
    ]
    assert (
        api_outcome_from_execution(structured_output=None, step_results=steps_all_ok) == "success"
    )


def test_api_outcome_from_execution_default_success():
    assert api_outcome_from_execution(structured_output={"x": 1}, step_results=None) == "success"
    assert api_outcome_from_execution(structured_output=None, step_results=[]) == "success"
    assert api_outcome_from_execution(structured_output=None, step_results=None) == "success"


# ---------------------------------------------------------------------------
# infer_capability_task_family
# ---------------------------------------------------------------------------


def test_task_family_general_request_for_non_translation_text():
    assert infer_capability_task_family("summarize this paragraph", None) == "general_request"
    assert infer_capability_task_family(None, "search for X") == "general_request"
    assert infer_capability_task_family(None, None) == "general_request"


def test_task_family_short_translation_for_brief_translate_request():
    assert infer_capability_task_family("translate hello to japanese", None) == "short_translation"
    assert infer_capability_task_family("英訳して", None) == "short_translation"


def test_task_family_long_translation_for_long_text():
    long_text = "translate " + ("the quick brown fox " * 30)
    assert infer_capability_task_family(long_text, None) == "long_or_general_translation"


def test_task_family_long_translation_for_high_word_count():
    # Just over 24 tokens but under 140 chars
    text = "translate " + " ".join(f"w{i}" for i in range(30))
    assert len(text) <= 200
    assert infer_capability_task_family(text, None) == "long_or_general_translation"


def test_task_family_long_translation_for_domain_hints():
    # Domain hint trips long even on a short request
    assert (
        infer_capability_task_family("translate this market strategy doc", None)
        == "long_or_general_translation"
    )
    # JA domain hint
    assert infer_capability_task_family("半導体について翻訳", None) == "long_or_general_translation"


def test_task_family_combines_user_message_and_goal():
    """The lookup spans both fields concatenated — translator hint in
    either side flips translation classification on."""
    assert (
        infer_capability_task_family("hello world", "please translate this") == "short_translation"
    )


# ---------------------------------------------------------------------------
# failure_kind_from_execution
# ---------------------------------------------------------------------------


def test_failure_kind_out_of_coverage_wins_over_status():
    """out_of_coverage from api_outcome wins even if status='succeeded' —
    the tool returned but didn't actually do the work."""
    assert (
        failure_kind_from_execution(status="succeeded", api_outcome="out_of_coverage", details="")
        == "out_of_coverage"
    )
    assert (
        failure_kind_from_execution(status="failed", api_outcome="OUT_OF_COVERAGE  ", details="503")
        == "out_of_coverage"
    )


def test_failure_kind_returns_none_when_status_not_failed():
    assert (
        failure_kind_from_execution(status="succeeded", api_outcome="success", details="") is None
    )
    assert failure_kind_from_execution(status=None, api_outcome=None, details="") is None
    assert failure_kind_from_execution(status="running", api_outcome="success", details="") is None


def test_failure_kind_policy_or_limit_keywords():
    for detail in (
        "policy_denied",
        "rate limit exceeded",
        "cap reached",
        "daily cap hit",
        "approval required",
        "quota exhausted",
        "monthly limit",
    ):
        assert (
            failure_kind_from_execution(status="failed", api_outcome="success", details=detail)
            == "policy_or_limit"
        ), f"detail {detail!r} should classify as policy_or_limit"


def test_failure_kind_runtime_unavailable_keywords():
    for detail in (
        "503 Service Unavailable",
        "service unavailable",
        "temporarily down",
        "request timeout",
        "timed out after 30s",
        "no tunnel",
        "tunnel error",
    ):
        assert (
            failure_kind_from_execution(status="failed", api_outcome="success", details=detail)
            == "runtime_unavailable"
        ), f"detail {detail!r} should classify as runtime_unavailable"


def test_failure_kind_generic_execution_failure_when_no_keyword_match():
    assert (
        failure_kind_from_execution(
            status="failed", api_outcome="success", details="some unhandled error"
        )
        == "execution_failure"
    )


def test_failure_kind_empty_details_with_failed_status_is_execution_failure():
    """status='failed' but no details to inspect — neither policy nor
    runtime keyword can match, so the classifier falls through to the
    generic execution_failure label rather than returning None. Pin
    explicitly because empty-details is a real platform call shape
    (the tool may have died before any error message was captured)."""
    for empty in ("", "   ", "\n\t"):
        assert (
            failure_kind_from_execution(status="failed", api_outcome="success", details=empty)
            == "execution_failure"
        ), f"empty details {empty!r} with status=failed should be execution_failure"


def test_failure_kind_policy_priority_over_runtime():
    """Policy/limit is checked first — a string containing both wins for policy."""
    assert (
        failure_kind_from_execution(
            status="failed",
            api_outcome="success",
            details="rate limit + 503 service unavailable",
        )
        == "policy_or_limit"
    )


# ---------------------------------------------------------------------------
# learning_expiry_for_kind
# ---------------------------------------------------------------------------


_FROZEN_NOW = dt.datetime(2026, 5, 2, 12, 0, 0, tzinfo=dt.UTC)


def test_learning_expiry_returns_none_for_out_of_coverage_and_unknown():
    """out_of_coverage cards never expire — the gap is persistent until a
    publisher updates the capability. Unknown kinds also return None
    (fail-open: the card can still be superseded)."""
    assert learning_expiry_for_kind("out_of_coverage", now=_FROZEN_NOW) is None
    assert learning_expiry_for_kind("totally_made_up", now=_FROZEN_NOW) is None
    assert learning_expiry_for_kind("", now=_FROZEN_NOW) is None


@pytest.mark.parametrize(
    "kind,delta",
    [
        ("runtime_unavailable", dt.timedelta(hours=1)),
        ("policy_or_limit", dt.timedelta(hours=6)),
        ("execution_failure", dt.timedelta(days=1)),
        (SYSTEM_PROMPT_OVERFLOW_KIND, dt.timedelta(hours=24)),
    ],
)
def test_learning_expiry_durations(kind, delta):
    expiry = learning_expiry_for_kind(kind, now=_FROZEN_NOW)
    assert expiry is not None
    assert expiry - _FROZEN_NOW == delta


def test_learning_expiry_preserves_caller_tz_shape():
    """Whatever tz-shape the caller hands in (aware or naive), the
    returned expiry has the same shape — the function is a pure offset.
    This pins compatibility with both the platform's actual call site
    (which passes ``utcnow()`` returning tz-aware) and any downstream
    caller that uses ``datetime.utcnow()`` (returns naive). A future
    refactor that introduces tz-coercion would cause a one-off
    ``expires_at`` column shift in production."""
    aware = dt.datetime(2026, 5, 2, 12, 0, 0, tzinfo=dt.UTC)
    naive = dt.datetime(2026, 5, 2, 12, 0, 0)
    aware_expiry = learning_expiry_for_kind("runtime_unavailable", now=aware)
    naive_expiry = learning_expiry_for_kind("runtime_unavailable", now=naive)
    assert aware_expiry is not None and naive_expiry is not None
    assert aware_expiry.tzinfo is dt.UTC
    assert naive_expiry.tzinfo is None
    # Same wall-clock offset regardless of tz shape.
    assert aware_expiry.replace(tzinfo=None) == naive_expiry


def test_learning_expiry_uses_caller_supplied_clock():
    """Pure function: never reads a wall clock. Two calls with two
    different ``now`` values produce two different expiries even though
    no time has actually passed in real life."""
    early = dt.datetime(2020, 1, 1, tzinfo=dt.UTC)
    late = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)
    e1 = learning_expiry_for_kind("runtime_unavailable", now=early)
    e2 = learning_expiry_for_kind("runtime_unavailable", now=late)
    assert e1 is not None and e2 is not None
    assert (e2 - e1) == (late - early)


def test_learning_expiry_now_is_keyword_only():
    with pytest.raises(TypeError):
        learning_expiry_for_kind("runtime_unavailable", _FROZEN_NOW)  # type: ignore[misc]


# ---------------------------------------------------------------------------
# learning_scores_for_kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("out_of_coverage", (0.9, 0.92)),
        ("runtime_unavailable", (0.55, 0.66)),
        ("policy_or_limit", (0.5, 0.62)),
        (SYSTEM_PROMPT_OVERFLOW_KIND, (0.9, 0.95)),
        # Default (unknown) returns the moderate baseline:
        ("execution_failure", (0.7, 0.74)),
        ("totally_made_up", (0.7, 0.74)),
    ],
)
def test_learning_scores_per_kind(kind, expected):
    assert learning_scores_for_kind(kind) == expected


def test_learning_scores_returns_tuple_in_unit_range():
    for kind in (
        "out_of_coverage",
        "runtime_unavailable",
        "policy_or_limit",
        "execution_failure",
        SYSTEM_PROMPT_OVERFLOW_KIND,
    ):
        importance, confidence = learning_scores_for_kind(kind)
        assert 0.0 <= importance <= 1.0
        assert 0.0 <= confidence <= 1.0


# ---------------------------------------------------------------------------
# build_learning_content
# ---------------------------------------------------------------------------


def test_build_learning_content_out_of_coverage_template():
    tool = _make_tool("Acme Translator")
    content = build_learning_content(
        tool=tool,
        failure_kind="out_of_coverage",
        task_family="long_or_general_translation",
        request_preview="translate this market doc",
    )
    assert "Acme Translator" in content
    assert "long_or_general_translation" in content
    assert "translate this market doc" in content
    assert "out of coverage" in content


def test_build_learning_content_runtime_unavailable_template():
    tool = _make_tool("Acme RT")
    content = build_learning_content(
        tool=tool,
        failure_kind="runtime_unavailable",
        task_family="general_request",
        request_preview="run job",
    )
    assert "Acme RT" in content
    assert "transient runtime failure" in content
    # Task family is NOT included in this branch — only out_of_coverage and
    # the default branch interpolate it. Pin that contract.
    assert "general_request" not in content


def test_build_learning_content_policy_or_limit_template():
    tool = _make_tool("Acme Quota")
    content = build_learning_content(
        tool=tool,
        failure_kind="policy_or_limit",
        task_family="general_request",
        request_preview="ping owner",
    )
    assert "Acme Quota" in content
    assert "policy" in content.lower()
    assert "approval/account/limit" in content


def test_build_learning_content_default_template_for_execution_failure():
    tool = _make_tool("Acme Generic")
    content = build_learning_content(
        tool=tool,
        failure_kind="execution_failure",
        task_family="general_request",
        request_preview="do thing",
    )
    assert "Acme Generic" in content
    # Default template DOES interpolate task_family (unlike runtime/policy)
    assert "general_request" in content
    assert "do thing" in content


def test_build_learning_content_default_template_for_unknown_kind():
    """Unknown kinds fall through to the default template — no exception."""
    tool = _make_tool("Acme X")
    content = build_learning_content(
        tool=tool,
        failure_kind="totally_made_up",
        task_family="general_request",
        request_preview="x",
    )
    assert "Acme X" in content
    assert "general_request" in content


# ---------------------------------------------------------------------------
# build_system_prompt_overflow_content
# ---------------------------------------------------------------------------


def test_build_overflow_content_includes_required_vs_budget_when_present():
    content = build_system_prompt_overflow_content(
        request_preview="run my agent",
        fit_meta={
            "estimated_required_system_tokens": 82_500,
            "chat_input_token_budget": 12_000,
        },
    )
    assert "required=82500 tokens vs budget=12000" in content
    assert "run my agent" in content
    assert "configuration issue" in content


def test_build_overflow_content_omits_detail_when_fit_meta_missing():
    content = build_system_prompt_overflow_content(
        request_preview="x",
        fit_meta=None,
    )
    assert "required=" not in content
    # And degrades gracefully when only one side is present
    content_partial = build_system_prompt_overflow_content(
        request_preview="x",
        fit_meta={"estimated_required_system_tokens": 100},
    )
    assert "required=" not in content_partial


def test_build_overflow_content_omits_detail_when_budget_zero_or_non_int():
    """Guard against div-by-zero-style display issues — the platform's
    ``fit_meta`` may carry zero / None / non-int values when the budget
    isn't computable. The detail string is omitted in those cases."""
    for fit in (
        {"estimated_required_system_tokens": 100, "chat_input_token_budget": 0},
        {"estimated_required_system_tokens": "100", "chat_input_token_budget": 12_000},
        {"estimated_required_system_tokens": 100, "chat_input_token_budget": None},
    ):
        content = build_system_prompt_overflow_content(request_preview="x", fit_meta=fit)
        assert "required=" not in content
