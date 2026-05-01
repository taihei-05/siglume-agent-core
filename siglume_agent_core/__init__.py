"""siglume-agent-core: open-core orchestrator logic for the Siglume API Store.

Tier A (v0.1):
- ``tool_manual_validator``: grade publisher manuals A-F (same as the platform).
- ``provider_adapters``: Anthropic / OpenAI tool-use adapters.

Tier B (v0.2, this release):
- ``installed_tool_prefilter``: TF-IDF + cosine similarity ranking that picks
  the top-N most-relevant tools when an agent is bound to many. Pure-Python,
  no external embedding service. Used by the platform to keep the chat
  system prompt within the input token budget.
- ``types.ResolvedToolDefinition``: the value-shape used by prefilter (and
  future Tier B modules). Mirrors the platform's resolver output.

See ARCHITECTURE.md for the staged extraction roadmap.
"""

__version__ = "0.2.3"
