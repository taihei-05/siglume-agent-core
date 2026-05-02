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

Tier B Phase 2 cont. (v0.4):
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

Tier C Phase 1 (v0.5):
- ``orchestrate_helpers``: byte-equivalent companions of the platform's
  ``tool_use_runtime`` orchestrate path with no DB / gateway / SDK
  dependency. ``OwnerOperationToolDefinition`` (value shape for first-party
  owner operations exposed to the orchestrator), ``to_provider_tool``
  (convert resolved or owner-operation tool to ProviderToolDefinition with
  description composition mirrored byte-for-byte), ``build_orchestrate_system_prompt``
  (render the manifest + role + format rules + multi-capability buyer-input
  mapping + revision guard + goal — clock is injected via ``now``),
  ``extract_llm_usage`` / ``estimate_usd_cents`` (provider-neutral usage
  normaliser + preflight USD-cents estimator with public
  ``DEFAULT_MODEL_PRICE_PER_MTOKEN_CENTS`` table), and the approval predicates
  ``execution_context_requires_approval`` / ``permission_can_run_without_approval``.

Tier C Phase 2 (v0.6):
- ``orchestrate``: the orchestrate inner loop, lifted out of the
  platform's ~990-line ``ToolUseRuntime.orchestrate`` method. The platform
  passes a callback bag (``OrchestrationDispatcher`` — five callables for
  policy / read-only execute / dry-run / owner-op dispatch / awaiting-
  approval emit) so the loop can ask the gateway to do its DB / outbox
  side-effects without agent-core importing the gateway, the ORM session,
  or the outbox. The loop returns an ``OrchestrationOutcome`` record;
  the platform shim reads it and persists receipt / outbox / failure-
  learning rows itself. ``early_return_result`` carries the
  ExecutionResult to return verbatim when an approval short-circuits the
  loop. Cross-provider fallback (``CROSS_PROVIDER_FALLBACK_MODEL``) only
  fires on iteration 0 when the primary OpenAI adapter raises — the same
  policy the platform had inline.

Tier C Phase 3 (v0.7, this release):
- ``dev_simulator``: pure helpers for the publisher dev-simulator —
  the read-only "what would the planner have done?" preview. Stages 2-4
  of the four-stage pipeline (keyword pre-filter, dedupe / Anthropic-
  regex filter, single tool_choice="auto" turn, predicted-chain
  reconstruction) move out of the platform's
  ``capability_runtime.dev_simulator``. The platform passes already-
  fetched ``(ProductListingLike, CapabilityReleaseLike)`` rows (Protocols
  — no SQLAlchemy import) plus an injected ``LLMSimulateCall`` callable
  so agent-core never touches an ORM session or the Anthropic SDK.
  Public surface: ``simulate_planner`` (high-level entry),
  ``select_candidates`` / ``filter_tools_for_anthropic`` (composable
  primitives), ``extract_keywords`` / ``score_candidate`` /
  ``build_tool_def`` / ``sanitize_input_schema_for_anthropic`` (tested
  in isolation), public constants ``SIMULATE_MODEL`` /
  ``SIMULATE_SYSTEM_PROMPT`` / ``STOP_WORDS`` /
  ``ANTHROPIC_PROPERTY_KEY_RE`` / ``ANTHROPIC_TOOL_NAME_RE``, and
  the ``LLMSimulateCall`` / ``LLMSimulateResponse`` /
  ``LLMSimulateToolUseBlock`` provider-neutral message types. All four
  diagnostic ``SimulationResult.note`` strings the monorepo shipped
  pre-extraction are produced verbatim — empty offer, empty catalog,
  all-filtered, LLM picked nothing, and any caller-supplied
  ``error_note`` propagated as-is.

See ARCHITECTURE.md for the staged extraction roadmap.
"""

__version__ = "0.7.0"
