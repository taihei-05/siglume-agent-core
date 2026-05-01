"""LLM provider adapters for Anthropic and OpenAI tool-use APIs.

Common surface: ``run_turn(model, messages, tools, max_output_tokens, tool_choice)``
returning a uniform :class:`ToolTurnResult`. See ``types.py`` for the shared
abstractions and the per-provider files for the concrete adapters.
"""
