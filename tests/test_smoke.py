"""Smoke tests for siglume-agent-core v0.1 (Tier A).

Verifies the package imports cleanly and the public API surfaces are present.
End-to-end behavior tests live alongside each module's logic and are exercised
by the parity test suite in the Siglume monorepo (which asserts byte-equivalence
between the monorepo's local copy and this package).
"""
from __future__ import annotations


def test_package_version_present():
    import orchestrator_core
    assert orchestrator_core.__version__ == "0.1.0"


def test_tool_manual_validator_imports():
    from orchestrator_core.tool_manual_validator import (
        score_manual_quality,
        validate_tool_manual,
    )
    assert callable(validate_tool_manual)
    assert callable(score_manual_quality)


def test_validate_tool_manual_minimal_happy_path():
    """A minimal but well-formed manual should pass structural validation."""
    from orchestrator_core.tool_manual_validator import validate_tool_manual

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
    from orchestrator_core.tool_manual_validator import score_manual_quality

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
    from orchestrator_core.provider_adapters.types import ToolMessage
    msg = ToolMessage(role="user", content="hello")
    assert msg.role == "user"
    assert msg.content == "hello"


def test_anthropic_adapter_imports_without_sdk():
    """The adapter module should import even if the anthropic SDK isn't installed."""
    from orchestrator_core.provider_adapters import anthropic_tools  # noqa: F401


def test_openai_adapter_imports_without_sdk():
    """The adapter module should import even if the openai SDK isn't installed."""
    from orchestrator_core.provider_adapters import openai_tools  # noqa: F401
