"""siglume-agent-core: open-core orchestrator logic for the Siglume API Store.

Tier A (v0.1):
- ``tool_manual_validator``: grade publisher manuals A-F (same as the platform).
- ``provider_adapters``: Anthropic / OpenAI tool-use adapters.

Tier B Phase 1 (v0.2):
- ``installed_tool_prefilter``: TF-IDF + cosine similarity ranking that picks
  the top-N most-relevant tools when an agent is bound to many. Pure-Python,
  no external embedding service. Used by the platform to keep the chat
  system prompt within the input token budget.
- ``types.ResolvedToolDefinition``: the value-shape used by prefilter (and
  future Tier B modules). Mirrors the platform's resolver output.

Tier B Phase 2 (v0.3):
- ``tool_selector``: dispatch-time keyword scorer. Runs after the prefilter
  trims the catalog: filters out tools with missing connected accounts
  (when their permission_class requires one), scores the remainder against
  the user request, and returns the top-K (default 5) in score order.
  Surfaces "no useful match" gap signals via an ``on_unmatched`` callback
  so the caller can persist them however it wants — agent-core stays
  pure (no DB / no SAVEPOINT). Companion utility
  ``strip_long_alphanumeric_secrets`` is exposed publicly for callers
  composing their own request-text redactor.

Tier B Phase 2 cont. (v0.4, this release):
- ``capability_failure_learning``: pure decision functions feeding the
  platform's tool-failure memory cards. Classify execution outcomes
  (``failure_kind_from_execution``), bucket requests into task families
  (``infer_capability_task_family``), pick avoidance duration / scoring
  per kind (``learning_expiry_for_kind`` / ``learning_scores_for_kind``),
  and render the human-readable advice strings stored on each card
  (``build_learning_content`` / ``build_system_prompt_overflow_content``).
  The DB-bound entry points (record / query / supersede) stay in the
  platform; the public functions compose into them so a publisher can
  read exactly what triggers an avoidance and how long it lives. Clock
  is injected: ``learning_expiry_for_kind`` requires ``now`` as a
  keyword arg so the function is fully pure.

See ARCHITECTURE.md for the staged extraction roadmap.
"""

__version__ = "0.4.0"
