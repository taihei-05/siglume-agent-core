"""Anthropic provider adapter for tool/function calling.

Abstracts the Anthropic messages API for Siglume's capability runtime,
converting between the internal normalized tool format and Anthropic's
tool-use protocol.
"""

from __future__ import annotations

import os
from typing import Any

from siglume_agent_core.provider_adapters.types import (
    NormalizedToolCall,
    ProviderToolDefinition,
    ToolMessage,
    ToolTurnResult,
)

# Anthropic SDK is an OPTIONAL extra. Don't import at module top level —
# core users (e.g. only using tool_manual_validator or installed_tool_prefilter)
# would otherwise hit ImportError on `import siglume_agent_core.provider_adapters.anthropic_tools`.
# We resolve the SDK lazily inside the adapter so importing this module
# never fails; constructing the adapter without the SDK installed raises
# a precise error pointing at the install command.

# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


def _require_anthropic() -> Any:
    """Import the anthropic SDK on demand, with an actionable error."""
    try:
        import anthropic  # noqa: WPS433 — lazy import is intentional
    except ImportError as exc:  # pragma: no cover — environment-dependent
        raise ImportError(
            "The Anthropic provider adapter requires the optional `anthropic` "
            "extra. Install it with:\n"
            "    pip install 'siglume-agent-core[anthropic]'"
        ) from exc
    return anthropic


class AnthropicToolAdapter:
    """Wraps the Anthropic messages API for tool-augmented turns."""

    def __init__(self, api_key: str | None = None) -> None:
        anthropic = _require_anthropic()
        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self._client = anthropic.Anthropic(api_key=resolved_key)
        # Cache the module reference so APIError exception class is the
        # same object the SDK actually raised (avoids subtle import-cache
        # mismatches under reload).
        self._anthropic_module = anthropic

    # -- public ------------------------------------------------------------

    def run_turn(
        self,
        *,
        model: str,
        messages: list[ToolMessage],
        tools: list[ProviderToolDefinition],
        max_output_tokens: int = 4096,
        tool_choice: str = "auto",
    ) -> ToolTurnResult:
        """Execute one turn of tool-augmented conversation.

        ``tool_choice`` accepts ``"auto"`` (LLM picks 0+ tools), ``"any"``
        (LLM must pick at least one tool), or ``"none"`` (LLM may not call
        tools this turn). Anthropic's tool-use API has no direct "none"
        mode, so for ``"none"`` the adapter elides ``tools`` and
        ``tool_choice`` entirely from the request — the model receives no
        tool definitions and physically cannot emit a ``tool_use`` block.
        This matches OpenAI's ``tool_choice="none"`` semantics; relying on
        a textual hint to "not use tools" is unreliable for action /
        payment-class capabilities and must not be the contract here.
        """
        try:
            system_text, anthropic_messages = self._convert_messages(messages)
            anthropic_tools = self._convert_tools(tools)

            kwargs: dict[str, Any] = dict(
                model=model,
                messages=anthropic_messages,
                max_tokens=max_output_tokens,
            )
            if system_text:
                kwargs["system"] = system_text
            # tool_choice="none" must HARD-disable tool use, not just hint at it.
            # Drop the tools array entirely so the model has nothing to call.
            if anthropic_tools and tool_choice != "none":
                kwargs["tools"] = anthropic_tools
                kwargs["tool_choice"] = self._convert_tool_choice(tool_choice)

            response = self._client.messages.create(**kwargs)
            return self._parse_response(response)

        except self._anthropic_module.APIError as exc:
            raise RuntimeError(
                f"Anthropic API error during tool turn: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Unexpected error in Anthropic tool turn: {exc}"
            ) from exc

    # -- internal ----------------------------------------------------------

    @staticmethod
    def _convert_messages(
        messages: list[ToolMessage],
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Extract system prompt and convert messages to Anthropic format.

        Returns (system_text, messages_list).
        """
        system_text: str | None = None
        out: list[dict[str, Any]] = []

        for msg in messages:
            if msg.role == "system":
                # Anthropic uses a dedicated system parameter
                system_text = msg.content
                continue

            if msg.role == "tool":
                # Tool results are sent as a user message with tool_result block
                out.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": msg.tool_call_id,
                            "content": msg.content,
                        }
                    ],
                })
            elif msg.role == "assistant" and msg.tool_calls:
                # Reconstruct assistant message with text + tool_use blocks
                content_blocks: list[dict[str, Any]] = []
                if msg.content:
                    content_blocks.append({
                        "type": "text",
                        "text": msg.content,
                    })
                for tc in msg.tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.tool_name,
                        "input": tc.arguments,
                    })
                out.append({
                    "role": "assistant",
                    "content": content_blocks,
                })
            else:
                out.append({
                    "role": msg.role,
                    "content": msg.content,
                })

        return system_text, out

    @staticmethod
    def _convert_tools(
        tools: list[ProviderToolDefinition],
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters,
            }
            for t in tools
        ]

    @staticmethod
    def _convert_tool_choice(tool_choice: str) -> dict[str, str]:
        # NOTE: "none" is handled at the call site in `run_turn` by eliding
        # the tools array entirely (Anthropic has no direct "none"). This
        # mapping is only consulted when tools ARE being sent, so "none"
        # should never reach here in normal flow. Keep "none" mapped to
        # "auto" as a safe fallback if the call-site guard is ever
        # bypassed: at worst the LLM may decide to call a tool, but
        # callers asking for "none" should rely on the elision contract,
        # not on this fallback.
        mapping = {
            "auto": {"type": "auto"},
            "any": {"type": "any"},
            "none": {"type": "auto"},
        }
        return mapping.get(tool_choice, {"type": "auto"})

    @staticmethod
    def _parse_response(response: Any) -> ToolTurnResult:
        assistant_text_parts: list[str] = []
        normalized: list[NormalizedToolCall] = []

        for block in response.content:
            if block.type == "text":
                assistant_text_parts.append(block.text)
            elif block.type == "tool_use":
                normalized.append(
                    NormalizedToolCall(
                        id=block.id,
                        tool_name=block.name,
                        arguments=block.input if isinstance(block.input, dict) else {},
                    )
                )

        assistant_text = "\n".join(assistant_text_parts) if assistant_text_parts else None

        # Map Anthropic stop_reason to normalized values
        stop_reason_map = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "max_tokens",
        }
        stop_reason = stop_reason_map.get(response.stop_reason, "end_turn")

        return ToolTurnResult(
            assistant_text=assistant_text,
            tool_calls=normalized,
            stop_reason=stop_reason,
            raw_provider_payload=response.model_dump(),
        )
