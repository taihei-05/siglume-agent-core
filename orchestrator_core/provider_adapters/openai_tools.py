"""OpenAI provider adapter for tool/function calling.

Abstracts the OpenAI chat completions API for Siglume's capability runtime,
converting between the internal normalized tool format and OpenAI's
function-calling protocol.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import openai
from agent_sns.application.capability_runtime.provider_adapters.types import (
    NormalizedToolCall,
    ProviderToolDefinition,
    ToolMessage,
    ToolTurnResult,
)

# OpenAI reasoning / GPT-5 model families reject the legacy ``max_tokens``
# request kwarg with HTTP 400 ``"Use 'max_completion_tokens'"``. The 2026-04
# stress test (119 skills bound to demo agent) hit this for every tool turn
# because this adapter was the one OpenAI call site missed when the rest of
# the codebase migrated to ``max_completion_tokens``. The dual-path guard
# tests in ``apps/api/tests/unit/test_no_dual_openai_path.py`` freeze that
# regression class so the next migration cannot leave this path behind.
#
# Family-prefix boundary: after the prefix we require either a separator
# (``-``, ``.``, ``o``) or end-of-string. This catches every observed
# variant — ``gpt-5``, ``gpt-5.4``, ``gpt-5.4-mini``, ``gpt-5o``,
# ``gpt-5-mini``, ``o1``, ``o1-preview``, ``o3``, ``o3-mini`` — without
# false-matching a hypothetical future non-reasoning ``o10`` / ``o30``
# family. ``o4`` and beyond fall through to the unknown-model default
# (``max_completion_tokens``) which is the safe forward direction.
_REASONING_MODEL_FAMILY_PREFIX_RE = re.compile(
    r"^(?:gpt-5|o1|o3)(?:[-.o]|$)", re.IGNORECASE
)


def _max_output_tokens_kwarg_for_model(
    model: str, max_output_tokens: int
) -> dict[str, int]:
    """Return the correct token-cap kwarg for ``model``.

    GPT-5 / o1 / o3 reasoning families require ``max_completion_tokens``.
    Legacy ``gpt-3.5-*`` / ``gpt-4-*`` (non-reasoning) families still accept
    ``max_tokens``. When the model id is empty or unrecognised we default to
    the new ``max_completion_tokens`` form because that is what the rest of
    the codebase uses and it is forward-compatible with future model
    families.
    """
    if _REASONING_MODEL_FAMILY_PREFIX_RE.match(model or ""):
        return {"max_completion_tokens": max_output_tokens}
    if (model or "").lower().startswith(("gpt-3.5", "gpt-4")):
        return {"max_tokens": max_output_tokens}
    return {"max_completion_tokens": max_output_tokens}


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------

class OpenAIToolAdapter:
    """Wraps the OpenAI chat-completions API for tool-augmented turns."""

    def __init__(self, api_key: str | None = None) -> None:
        resolved_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = openai.OpenAI(api_key=resolved_key)

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
        """Execute one turn of tool-augmented conversation."""
        try:
            openai_messages = self._convert_messages(messages)
            openai_tools = self._convert_tools(tools)

            kwargs: dict[str, Any] = {
                "model": model,
                "messages": openai_messages,
                **_max_output_tokens_kwarg_for_model(model, max_output_tokens),
            }
            if openai_tools:
                kwargs["tools"] = openai_tools
                kwargs["tool_choice"] = tool_choice

            response = self._client.chat.completions.create(**kwargs)
            return self._parse_response(response)

        except openai.APIError as exc:
            raise RuntimeError(
                f"OpenAI API error during tool turn: {exc}"
            ) from exc
        except Exception as exc:
            raise RuntimeError(
                f"Unexpected error in OpenAI tool turn: {exc}"
            ) from exc

    # -- internal ----------------------------------------------------------

    @staticmethod
    def _convert_messages(messages: list[ToolMessage]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for msg in messages:
            if msg.role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": msg.content,
                })
            elif msg.role == "assistant" and msg.tool_calls:
                tc_list = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.tool_name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ]
                out.append({
                    "role": "assistant",
                    "content": msg.content or None,
                    "tool_calls": tc_list,
                })
            else:
                out.append({
                    "role": msg.role,
                    "content": msg.content,
                })
        return out

    @staticmethod
    def _convert_tools(
        tools: list[ProviderToolDefinition],
    ) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in tools
        ]

    @staticmethod
    def _parse_response(response: Any) -> ToolTurnResult:
        choice = response.choices[0]
        message = choice.message

        # Extract text
        assistant_text = message.content

        # Extract and normalize tool calls
        normalized: list[NormalizedToolCall] = []
        if message.tool_calls:
            for tc in message.tool_calls:
                try:
                    arguments = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, TypeError):
                    arguments = {}
                normalized.append(
                    NormalizedToolCall(
                        id=tc.id,
                        tool_name=tc.function.name,
                        arguments=arguments,
                    )
                )

        # Map finish_reason to our normalized stop_reason
        finish_reason = choice.finish_reason
        stop_reason_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
            "content_filter": "end_turn",
        }
        stop_reason = stop_reason_map.get(finish_reason, "end_turn")

        return ToolTurnResult(
            assistant_text=assistant_text,
            tool_calls=normalized,
            stop_reason=stop_reason,
            raw_provider_payload=response.model_dump(),
        )
