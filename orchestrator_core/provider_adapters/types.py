"""Shared types for LLM provider tool calling adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class NormalizedToolCall:
    """Provider-neutral representation of a single tool call."""
    id: str
    tool_name: str
    arguments: dict[str, Any]


@dataclass
class ToolMessage:
    """Provider-neutral chat message that may contain tool calls or results."""
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None
    tool_calls: list[NormalizedToolCall] | None = None


@dataclass
class ProviderToolDefinition:
    """Provider-neutral tool/function definition."""
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ToolTurnResult:
    """Normalized result from one turn of tool-augmented LLM conversation."""
    assistant_text: str | None
    tool_calls: list[NormalizedToolCall]
    stop_reason: str  # "end_turn" | "tool_use" | "max_tokens"
    raw_provider_payload: dict[str, Any] = field(default_factory=dict)
