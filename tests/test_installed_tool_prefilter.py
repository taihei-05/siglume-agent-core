"""Tests for installed_tool_prefilter (Tier B v0.2).

The prefilter is pure (no DB, no network) so these are vanilla unit tests
constructing ResolvedToolDefinition instances directly. The platform's
own test suite asserts byte-equivalence with this package's logic.
"""

from __future__ import annotations

from siglume_agent_core.installed_tool_prefilter import (
    DEFAULT_MAX_TOOLS,
    select_top_tools_for_prompt,
)
from siglume_agent_core.types import ResolvedToolDefinition


def _mk_tool(
    *,
    capability_key: str,
    description: str = "",
    compact_prompt: str = "",
    display_name: str | None = None,
    usage_hints: list[str] | None = None,
    result_hints: list[str] | None = None,
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
        permission_class="read_only",
        approval_mode="auto",
        dry_run_supported=False,
        required_connected_accounts=[],
        account_readiness="ready",
        usage_hints=usage_hints or [],
        result_hints=result_hints or [],
        compact_prompt=compact_prompt,
    )


def test_default_max_tools_constant():
    assert DEFAULT_MAX_TOOLS == 50


def test_below_cap_returns_all_in_original_order():
    """Below the cap, scoring is skipped — original order preserved."""
    tools = [
        _mk_tool(capability_key="translate", description="translate text"),
        _mk_tool(capability_key="search", description="web search"),
        _mk_tool(capability_key="summarize", description="summarize docs"),
    ]
    out = select_top_tools_for_prompt(tools, "irrelevant message", max_tools=10)
    assert [t.capability_key for t in out] == ["translate", "search", "summarize"]


def test_above_cap_picks_top_n_by_relevance_and_restores_original_order():
    """Cap=2 across 3 tools — the top-2 by relevance survive, in their
    original list order so downstream prompt rendering is stable."""
    tools = [
        _mk_tool(capability_key="weather", description="check weather forecast"),
        _mk_tool(capability_key="translate", description="translate english to japanese"),
        _mk_tool(capability_key="search", description="web search engine"),
    ]
    out = select_top_tools_for_prompt(tools, "translate this english text", max_tools=2)
    # Top-2: translate is the obvious winner. Second pick is non-obvious
    # (weather and search both share zero terms with the query); deterministic
    # tie-break by original index keeps "weather" (idx 0).
    out_keys = {t.capability_key for t in out}
    assert "translate" in out_keys
    assert len(out) == 2
    # Original order restored among the survivors.
    indices = [tools.index(t) for t in out]
    assert indices == sorted(indices)


def test_empty_user_message_falls_back_to_prefix():
    """No signal -> deterministic prefix slice, not random scoring."""
    tools = [_mk_tool(capability_key=f"t_{i}") for i in range(5)]
    out = select_top_tools_for_prompt(tools, "", max_tools=2)
    assert [t.capability_key for t in out] == ["t_0", "t_1"]


def test_none_user_message_falls_back_to_prefix():
    tools = [_mk_tool(capability_key=f"t_{i}") for i in range(5)]
    out = select_top_tools_for_prompt(tools, None, max_tools=2)
    assert [t.capability_key for t in out] == ["t_0", "t_1"]


def test_zero_score_against_all_tools_falls_back_to_prefix():
    """When the user query shares no tokens with any tool, the prefix wins."""
    tools = [
        _mk_tool(capability_key="weather", description="check forecast"),
        _mk_tool(capability_key="search", description="web engine"),
        _mk_tool(capability_key="email", description="send messages"),
    ]
    out = select_top_tools_for_prompt(tools, "xyzzy plugh frobnicate", max_tools=2)
    assert [t.capability_key for t in out] == ["weather", "search"]


def test_max_tools_zero_returns_empty():
    tools = [_mk_tool(capability_key="t1")]
    assert select_top_tools_for_prompt(tools, "anything", max_tools=0) == []


def test_max_tools_negative_returns_empty():
    tools = [_mk_tool(capability_key="t1")]
    assert select_top_tools_for_prompt(tools, "anything", max_tools=-1) == []


def test_empty_tool_list_returns_empty():
    assert select_top_tools_for_prompt([], "anything", max_tools=10) == []


def test_japanese_user_message_picks_jp_tool():
    """CJK character bigrams should match a JA-described tool when the query
    is also in JA. Latin words and CJK live in the same vocabulary."""
    tools = [
        _mk_tool(capability_key="weather_en", description="weather forecast lookup"),
        _mk_tool(capability_key="weather_jp", description="天気予報を調べる"),
        _mk_tool(capability_key="search", description="web search"),
    ]
    out = select_top_tools_for_prompt(tools, "明日の天気予報を教えて", max_tools=1)
    assert len(out) == 1
    assert out[0].capability_key == "weather_jp"


def test_jtbd_text_combines_all_descriptive_fields():
    """Term frequency is boosted when the same term appears across multiple
    descriptive fields. A tool whose capability_key + description + usage_hints
    all reinforce 'translate' should rank above one that only mentions it once."""
    tools = [
        _mk_tool(
            capability_key="translate_text",
            description="Translate text between languages",
            compact_prompt="Translation utility.",
            usage_hints=["translate this", "do translation"],
        ),
        _mk_tool(
            capability_key="grammar_check",
            description="Check grammar; can also translate occasionally.",
        ),
    ]
    out = select_top_tools_for_prompt(tools, "translate this paragraph", max_tools=1)
    assert out[0].capability_key == "translate_text"


def test_non_string_user_message_falls_back_to_prefix():
    """Defensive: non-string input shouldn't crash _tokenize."""
    tools = [_mk_tool(capability_key=f"t_{i}") for i in range(3)]
    out = select_top_tools_for_prompt(tools, 12345, max_tools=2)  # type: ignore[arg-type]
    assert [t.capability_key for t in out] == ["t_0", "t_1"]


def test_resolved_tool_definition_dataclass_round_trip():
    """The value type is a plain dataclass — fields preserved on construction."""
    t = ResolvedToolDefinition(
        binding_id="b1",
        grant_id="g1",
        release_id="r1",
        listing_id="l1",
        capability_key="cap",
        tool_name="cap_tool",
        display_name="Cap Tool",
        description="x",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permission_class="read_only",
        approval_mode="auto",
        dry_run_supported=False,
        required_connected_accounts=[{"provider": "x"}],
        account_readiness="ready",
        usage_hints=["a", "b"],
        result_hints=["c"],
        cost_hint_usd_cents=10,
        compact_prompt="prompt",
    )
    assert t.capability_key == "cap"
    assert t.cost_hint_usd_cents == 10
    assert t.usage_hints == ["a", "b"]
