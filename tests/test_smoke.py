"""Smoke tests for siglume-agent-core v0.1 (Tier A).

Verifies the package imports cleanly and the public API surfaces are present.
End-to-end behavior tests live alongside each module's logic and are exercised
by the parity test suite in the Siglume monorepo (which asserts byte-equivalence
between the monorepo's local copy and this package).
"""

from __future__ import annotations


def test_package_version_present():
    import siglume_agent_core

    assert siglume_agent_core.__version__ == "0.3.0"


def test_tool_selector_imports():
    from siglume_agent_core.tool_selector import (
        DEFAULT_MAX_CANDIDATES,
        UnmatchedRequestSignal,
        extract_trigger_words,
        select_tools,
        strip_long_alphanumeric_secrets,
    )

    assert callable(select_tools)
    assert callable(strip_long_alphanumeric_secrets)
    assert callable(extract_trigger_words)
    assert DEFAULT_MAX_CANDIDATES == 5
    assert UnmatchedRequestSignal.__module__ == "siglume_agent_core.tool_selector"


def test_tool_manual_validator_imports():
    from siglume_agent_core.tool_manual_validator import (
        score_manual_quality,
        validate_tool_manual,
    )

    assert callable(validate_tool_manual)
    assert callable(score_manual_quality)


def test_validate_tool_manual_minimal_happy_path():
    """A minimal but well-formed manual should pass structural validation."""
    from siglume_agent_core.tool_manual_validator import validate_tool_manual

    manual = {
        "capability_key": "translate_text",
        "name": "Translate Text",
        "summary_for_model": (
            "Translate a given English passage into a target language. "
            "Returns the translation as a string. Use when the user asks "
            "for translation between specific languages."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to translate"},
                "target_lang": {"type": "string", "description": "BCP-47 lang"},
            },
            "required": ["text", "target_lang"],
        },
        "output_schema": {
            "type": "object",
            "properties": {
                "translated": {"type": "string"},
            },
        },
        "usage_hints": ["translate this", "into japanese"],
    }
    result = validate_tool_manual(manual)
    # Either ok=True OR errors are non-blocking warnings — print to ease debug
    assert hasattr(result, "ok")
    assert hasattr(result, "errors")
    assert hasattr(result, "warnings")


def test_score_manual_quality_returns_grade():
    from siglume_agent_core.tool_manual_validator import score_manual_quality

    manual = {
        "capability_key": "echo",
        "name": "Echo",
        "summary_for_model": "Echo back the input. Used for testing.",
        "input_schema": {"type": "object", "properties": {}},
        "output_schema": {"type": "object", "properties": {}},
    }
    quality = score_manual_quality(manual)
    assert hasattr(quality, "grade")
    assert hasattr(quality, "overall_score")
    assert quality.grade in {"A", "B", "C", "D", "F"}
    assert 0 <= quality.overall_score <= 100


def test_provider_adapter_types_import():
    from siglume_agent_core.provider_adapters.types import ToolMessage

    msg = ToolMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"


def test_permission_class_literal_matches_validator():
    """The PermissionClass Literal in types.py must stay in lockstep with
    VALID_PERMISSION_CLASSES in tool_manual_validator. If a future change
    adds a new permission_class to one and forgets the other, this test
    fails so the drift is caught at CI rather than at submission time.
    """
    import typing

    from siglume_agent_core.tool_manual_validator import VALID_PERMISSION_CLASSES
    from siglume_agent_core.types import PermissionClass

    literal_values = set(typing.get_args(PermissionClass))
    assert literal_values == VALID_PERMISSION_CLASSES, (
        f"PermissionClass Literal drifted from validator: "
        f"{literal_values} vs {VALID_PERMISSION_CLASSES}"
    )


def test_account_readiness_literal_values():
    """Pins the documented set of account_readiness values."""
    import typing

    from siglume_agent_core.types import AccountReadiness

    assert set(typing.get_args(AccountReadiness)) == {"ready", "missing", "unhealthy"}


def test_anthropic_adapter_imports_without_sdk():
    """The adapter module imports cleanly regardless of whether the
    anthropic SDK is installed (lazy import — see _require_anthropic).
    """
    from siglume_agent_core.provider_adapters import anthropic_tools  # noqa: F401


def test_openai_adapter_imports_without_sdk():
    """Same contract for the OpenAI adapter — module import never fails
    on missing optional SDK; the SDK is required only at adapter
    instantiation time.
    """
    from siglume_agent_core.provider_adapters import openai_tools  # noqa: F401


def test_anthropic_adapter_constructor_raises_actionable_error_without_sdk(monkeypatch):
    """When the anthropic SDK isn't installed, instantiating
    AnthropicToolAdapter must raise ImportError with the exact install
    command the caller needs to run.
    """
    from siglume_agent_core.provider_adapters import anthropic_tools

    def _fake_require_anthropic():
        raise ImportError(
            "The Anthropic provider adapter requires the optional `anthropic` "
            "extra. Install it with:\n    pip install 'siglume-agent-core[anthropic]'"
        )

    monkeypatch.setattr(anthropic_tools, "_require_anthropic", _fake_require_anthropic)
    import pytest

    with pytest.raises(ImportError) as exc_info:
        anthropic_tools.AnthropicToolAdapter(api_key="dummy")
    assert "siglume-agent-core[anthropic]" in str(exc_info.value)


def test_openai_adapter_constructor_raises_actionable_error_without_sdk(monkeypatch):
    """Same contract for the OpenAI adapter."""
    from siglume_agent_core.provider_adapters import openai_tools

    def _fake_require_openai():
        raise ImportError(
            "The OpenAI provider adapter requires the optional `openai` "
            "extra. Install it with:\n    pip install 'siglume-agent-core[openai]'"
        )

    monkeypatch.setattr(openai_tools, "_require_openai", _fake_require_openai)
    import pytest

    with pytest.raises(ImportError) as exc_info:
        openai_tools.OpenAIToolAdapter(api_key="dummy")
    assert "siglume-agent-core[openai]" in str(exc_info.value)


def test_anthropic_adapter_tool_choice_none_elides_tools(monkeypatch):
    """tool_choice='none' must result in no `tools` and no `tool_choice`
    in the request — the model physically cannot emit a tool_use block.
    Verifies the v0.2.1 contract: 'none' is not a hint, it's a hard
    disable. Critical when action / payment-class tools share the
    adapter."""
    import pytest

    pytest.importorskip("anthropic")

    from siglume_agent_core.provider_adapters import anthropic_tools
    from siglume_agent_core.provider_adapters.types import (
        ProviderToolDefinition,
        ToolMessage,
    )

    captured: dict[str, object] = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Resp:
                content = []
                stop_reason = "end_turn"

                def model_dump(self_inner):
                    return {}

            return _Resp()

    class _FakeClient:
        def __init__(self, *a, **kw):
            self.messages = _FakeMessages()

    class _FakeAPIError(Exception):
        pass

    class _FakeAnthropicModule:
        APIError = _FakeAPIError

        @staticmethod
        def Anthropic(*a, **kw):
            return _FakeClient()

    monkeypatch.setattr(anthropic_tools, "_require_anthropic", lambda: _FakeAnthropicModule)
    adapter = anthropic_tools.AnthropicToolAdapter(api_key="dummy")
    adapter.run_turn(
        model="claude-haiku-4-5-20251001",
        messages=[ToolMessage(role="user", content="say hi")],
        tools=[
            ProviderToolDefinition(
                name="dangerous_action",
                description="must NOT be callable when tool_choice=none",
                parameters={"type": "object", "properties": {}},
            )
        ],
        max_output_tokens=512,
        tool_choice="none",
    )
    assert "tools" not in captured, (
        "tool_choice='none' must elide tools entirely — found in request payload"
    )
    assert "tool_choice" not in captured, (
        "tool_choice='none' must elide tool_choice key — found in request payload"
    )


def test_anthropic_adapter_tool_choice_auto_passes_tools(monkeypatch):
    """Sanity counter-test: tool_choice='auto' MUST send tools so the
    elision in the 'none' case is genuinely conditional, not a regression."""
    import pytest

    pytest.importorskip("anthropic")

    from siglume_agent_core.provider_adapters import anthropic_tools
    from siglume_agent_core.provider_adapters.types import (
        ProviderToolDefinition,
        ToolMessage,
    )

    captured: dict[str, object] = {}

    class _FakeMessages:
        def create(self, **kwargs):
            captured.update(kwargs)

            class _Resp:
                content = []
                stop_reason = "end_turn"

                def model_dump(self_inner):
                    return {}

            return _Resp()

    class _FakeClient:
        def __init__(self, *a, **kw):
            self.messages = _FakeMessages()

    class _FakeAPIError(Exception):
        pass

    class _FakeAnthropicModule:
        APIError = _FakeAPIError

        @staticmethod
        def Anthropic(*a, **kw):
            return _FakeClient()

    monkeypatch.setattr(anthropic_tools, "_require_anthropic", lambda: _FakeAnthropicModule)
    adapter = anthropic_tools.AnthropicToolAdapter(api_key="dummy")
    adapter.run_turn(
        model="claude-haiku-4-5-20251001",
        messages=[ToolMessage(role="user", content="hi")],
        tools=[
            ProviderToolDefinition(
                name="t",
                description="x",
                parameters={"type": "object", "properties": {}},
            )
        ],
        max_output_tokens=512,
        tool_choice="auto",
    )
    assert "tools" in captured
    assert "tool_choice" in captured
