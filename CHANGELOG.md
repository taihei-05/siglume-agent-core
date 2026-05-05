# Changelog

All notable changes to `siglume-agent-core` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches v1.0. Until then, minor versions (v0.x) may rename or restructure
public API while extraction from the private monorepo is in progress.

## [Unreleased]

## [0.9.0] - 2026-05-05

### Added

- **`siglume_agent_core.works_candidate_selector`** — pure Works
  auto-pitch candidate selection logic. The module exports stable match
  fingerprinting, terminal-match reuse policy, positive-match reuse policy,
  and top-N candidate ranking without importing the platform database,
  scheduler, notification system, payment rails, credentials, or LLM clients.

### Security

- Keeps production-only side effects out of agent-core. The public module does
  not expose proposal/order ids, API keys, connected account state, scheduler
  cadence, prompt text, or live agent logs. Callers pass normalized candidate
  facts and own all persistence and execution.

## [0.8.0] - 2026-05-05

### Added

- **`siglume_agent_core.job_feasibility`** — pure initial Works job route
  decision logic. The new module exports `JobFeasibilityInput`,
  `JobFeasibilityResult`, and `assess_job_feasibility(...)`.
  It returns whether a normalized Works job should start as `automated`,
  `manual`, `needs_clarification`, or `blocked` without importing platform
  models, touching the database, calling HTTP/LLM providers, or writing
  proposals/orders.

### Changed

- Version bumped to `0.8.0` because the public agent-core surface now covers
  pre-execution Works routing in addition to API selection and orchestration.

## [0.7.1] - 2026-05-02

Documentation-only patch release. No runtime / API changes — the
v0.7.1 wheel is byte-equivalent to v0.7.0 except for the README and
ARCHITECTURE rendered on PyPI. Cut so that
`pip show siglume-agent-core` and the project's pypi.org page reflect
the doc fixes that already landed on `main` after v0.7.0 was tagged.

### Fixed

- **README — `tool_selector` miss-kind names match the implementation.**
  Previous text listed `no_candidates_after_prefilter` /
  `no_candidates_after_permission_filter` / `no_keyword_overlap`. The
  `MissKind` `Literal` actually exports `no_tools_installed` /
  `all_filtered_account_missing` / `no_keyword_match`; an
  `on_unmatched` callback written against the README names would
  never fire. Updated to the actual names with one-line definitions
  and the four trigger-word source fields (`capability_key` /
  `display_name` / `description` / `usage_hints`).

- **README — `capability_failure_learning` example matches the v0.4
  signatures.** Previous snippet used an old positional shape
  (`failure_kind_from_execution(execution_result)`,
  `build_learning_content(kind, family, intent_text)`, single
  `scores` return). Replaced with a runnable shape using the actual
  keyword-only signatures: `failure_kind_from_execution(*, status,
  api_outcome, details)`, `infer_capability_task_family(user_message,
  goal)`, `build_learning_content(*, tool, failure_kind, task_family,
  request_preview)` (which takes a `ResolvedToolDefinition`), and
  `learning_scores_for_kind(kind) -> tuple[float, float]`. Walks
  `api_outcome` → `failure_kind` → `task_family` → expiry / scores /
  content, with a sentence calling out where each input variable
  comes from in an orchestrator.

- **README — `siglume dev simulate` is current, not "upcoming".** The
  CLI is already shipped at `dev_command.command("simulate")` in
  `siglume-api-sdk`. Updated wording and added a short
  `LLMSimulateCall` self-host example that follows the documented
  "MUST NOT raise" contract by returning `LLMSimulateResponse(
  tool_use_blocks=[], error_note="...")` instead of bubbling the
  exception (since `simulate_planner` does not catch exceptions out
  of `llm_call`).

- **README — `tool_choice="none"` description is provider-accurate.**
  Previous one-liner said the adapter elides `tools` entirely. That's
  true for Anthropic (no native `"none"` mode) but the OpenAI adapter
  passes OpenAI's native `tool_choice="none"` alongside the `tools`
  array. Updated to call out the per-adapter mechanism while noting
  both still produce zero tool calls.

## [0.7.0] - 2026-05-02

Tier C Phase 3. The publisher dev-simulator's pure stages move into
agent-core. The platform's
``packages/shared-python/agent_sns/application/capability_runtime/dev_simulator.py``
shrinks from ~375 lines to a thin shim that fetches catalog rows from
the DB and wraps the Anthropic SDK behind an ``LLMSimulateCall`` —
everything else (keyword pre-filter, dedupe / Anthropic-regex filter,
predicted-chain reconstruction, all four diagnostic ``note`` strings)
now sources from this package.

### Added

- **``siglume_agent_core.dev_simulator``** — new module with the pure
  publisher dev-simulator helpers.

  - ``simulate_planner(rows, *, offer_text, quota_used_today,
    quota_limit, llm_call, max_candidates=10, model=SIMULATE_MODEL)
    -> SimulationResult`` — high-level entry point. Composes
    ``select_candidates`` + ``filter_tools_for_anthropic`` + the
    injected LLM call into a single ``SimulationResult``. Catalog
    selection (which DB rows to pass) stays the caller's
    responsibility.

  - Composable primitives: ``select_candidates`` (score + truncate to
    top-N), ``filter_tools_for_anthropic`` (dedupe + Anthropic
    name-regex skip with first-wins-by-score order, returns the
    ``listing_lookup`` so the caller can re-attach listing metadata
    to the LLM's emitted blocks).

  - Tested-in-isolation pure helpers: ``extract_keywords`` (regex +
    stop-word filter), ``score_candidate`` (keyword overlap count),
    ``build_tool_def`` (field-fallback chain
    ``tool_prompt_compact`` -> ``manual.compact_prompt`` ->
    ``manual.description`` -> ``manual.summary_for_model`` ->
    ``listing.description`` -> ``listing.title`` -> ``capability_key``
    with a 600-char description clip),
    ``sanitize_input_schema_for_anthropic`` (recursive bad-key drop +
    ``required`` pruning, never mutates the caller's schema).

  - Provider-neutral message types: ``LLMSimulateResponse`` /
    ``LLMSimulateToolUseBlock`` (frozen dataclasses) +
    ``LLMSimulateCall`` (type alias for the injected callable
    signature ``(system_prompt, tools, user_message) ->
    LLMSimulateResponse``). The callable contract is "MUST NOT raise"
    — provider failures surface as ``error_note`` so
    ``simulate_planner`` can propagate them verbatim.

  - Result records: ``SimulatedToolCall`` / ``SimulationResult``
    (kept non-frozen — pinned by tests so future drift to ``frozen=True``
    is caught at CI rather than at runtime in callers that mutate
    ``.args`` or ``.predicted_chain``).

  - Catalog-row Protocols: ``ProductListingLike`` /
    ``CapabilityReleaseLike``. Platform ORM models satisfy them
    structurally — agent-core never imports SQLAlchemy.

  - Public constants: ``SIMULATE_MODEL`` (default Anthropic Haiku),
    ``SIMULATE_SYSTEM_PROMPT`` (the exact prompt the monorepo
    shipped), ``STOP_WORDS`` (closed list — drift here moves the
    scoring distribution silently),
    ``ANTHROPIC_PROPERTY_KEY_RE`` / ``ANTHROPIC_TOOL_NAME_RE``
    (Anthropic-side input validation patterns; tighter regex on tool
    name (no dots) than property key).

### Behavior preserved (byte-equivalent)

- All four diagnostic ``SimulationResult.note`` strings produced
  verbatim: ``"empty offer_text"``,
  ``"no candidate tools — catalog empty or all listings unusable"``,
  ``"no candidate tools survived Anthropic schema validation
  (skipped X duplicate, Y non-conforming name)"``,
  ``"LLM picked no tools (offer may not match any catalog entry)"``.
  Any ``error_note`` from the injected ``LLMSimulateCall`` is also
  propagated as-is — including the platform shim's
  ``"anthropic SDK not available"`` and ``"llm error: <ExcType>"``
  payloads.

- Dedupe + skip semantics (PR #203 in the monorepo): first-wins by
  score order, ``skipped_dup`` and ``skipped_bad_name`` counters
  surfaced into the no-survivors note exactly as before.

- Schema sanitization (PR #201 in the monorepo): bad property keys
  dropped from this level (and from any nested ``properties``),
  matching ``required`` entries pruned, original schema untouched.

- 600-char description clip on ``build_tool_def`` and the field
  fallback order through manual / release / listing fields.

- ``select_candidates`` clamps ``max_candidates`` to a minimum of 1
  via ``max(1, max_candidates)`` — same shape callers passing 0 saw
  pre-extraction.

### Notes

- The Anthropic SDK stays out of this package. The platform shim
  imports ``anthropic`` lazily inside its ``LLMSimulateCall`` factory
  and converts ``response.content[i]`` (where ``type == "tool_use"``)
  into ``LLMSimulateToolUseBlock``. SDK-missing and call-raised paths
  return an ``LLMSimulateResponse`` with ``error_note`` set so the
  pure loop here never has to know about provider failures.

## [0.6.0] - 2026-05-02

Tier C Phase 2. The orchestrate inner loop body itself moves into
agent-core. The platform's ``ToolUseRuntime.orchestrate`` shrinks from
~990 lines to a ~300-line shim that does intent fetch / capability
preflight / tool resolution before the loop and receipt / outbox /
failure-learning bookkeeping after.

### Added

- **``siglume_agent_core.orchestrate``** — new module with the pure
  per-iteration tool-use loop.

  - ``OrchestrationDispatcher`` (frozen dataclass) — five callables
    bridging the loop to the platform's gateway / DB / outbox:
    ``check_policy`` (Decision), ``execute_read_only`` (ExecutionResult),
    ``execute_dry_run`` (DryResult), ``dispatch_owner_operation``
    (ExecutionResult — wrapper internally writes ``intent.plan_jsonb``
    on ``approval_required``), ``emit_awaiting_approval``
    (ExecutionResult — wrapper internally mutates intent.status /
    metadata_jsonb / plan_jsonb + emits the outbox event).

  - ``OrchestrationOutcome`` (frozen dataclass) — the loop's return
    value: ``final_text`` / ``step_results`` / ``last_tool_output`` /
    ``total_tool_calls`` / ``iterations_used`` / ``llm_input_tokens_total``
    / ``llm_output_tokens_total`` / ``final_status`` (one of
    ``"completed" / "failed" / "approval_required"``) /
    ``failure_error_class`` / ``failure_error_message`` /
    ``resolved_model`` / ``early_return_result`` (set when an approval
    short-circuits the loop; the platform shim returns it verbatim).

  - ``run_orchestrate_loop(*, intent, resolved_model, tool_by_name,
    provider_tools, system_prompt, initial_user_message, max_iterations,
    max_tool_calls, max_output_tokens, exec_ctx,
    require_approval_for_actions, dispatcher, make_adapter)
    -> OrchestrationOutcome``.

  - Public constants: ``OPENAI_MODEL_PREFIXES`` /
    ``ANTHROPIC_MODEL_PREFIXES`` / ``CROSS_PROVIDER_FALLBACK_MODEL``.
    Cross-provider fallback fires only on iteration 0 when the
    primary model is OpenAI and the adapter raises — same policy the
    platform had inline. A second failure (Anthropic also down)
    propagates honestly so capability_failure_learning records the
    right ``error_class``.

### Behavior preserved (byte-equivalent)

- ``step_results`` dict shape per tool call — exact key set, exact
  ordering across owner_operation / installed_tool / unknown_tool /
  policy_denied / approval_required paths.
- ``messages`` ToolMessage construction order across iterations.
- LLM usage accumulation via ``extract_llm_usage`` (already in v0.5)
  every turn including the cross-provider fallback turn.
- ``final_status`` derivation: any successful step => completed;
  otherwise the last failed (non-approval_required) step's
  ``error_class`` / ``error_message`` win, with the same
  ``"tool_execution_failed"`` / ``"All orchestrated tool invocations
  failed."`` fallbacks the platform used.
- ``iterations_used`` matches the monorepo's ``min(iteration + 1,
  max_iterations) if last_turn else 0`` formula.

### Notes

- The platform's ``_fail_intent("approval_preview_failed")`` path
  (dry-run preview itself fails) surfaces as ``OrchestrationOutcome(
  final_status="failed", failure_error_class="approval_preview_failed",
  early_return_result=None)``. The platform shim is expected to route
  this specific error class through ``_fail_intent`` rather than the
  normal post-loop receipt path. This keeps the loop pure (no
  ``_fail_intent`` callback in the dispatcher) at the cost of one
  branch in the shim.
- ``intent`` is opaque to the loop except for one read:
  ``intent.status`` is consulted in the installed-tool approval guard
  to skip the dry-run when the intent has already been approved
  out-of-band.
- ``make_adapter`` is injected so callers can swap in their own
  adapter factory; the platform shim continues to use its existing
  ``_make_adapter`` which already imports from
  ``siglume_agent_core.provider_adapters``.

## [0.5.0] - 2026-05-02

Tier C Phase 1. The pure helpers feeding the platform's
``tool_use_runtime`` orchestrate path land as public source. The
orchestrate inner loop body itself stays in the platform for now — see
the v0.5 recon notes; a follow-up release will lift the loop into
agent-core via a callback bag (``OrchestrationDispatcher``).

### Added

- **``siglume_agent_core.orchestrate_helpers``** — pure helpers the
  platform composes into its ``ToolUseRuntime.orchestrate`` body. Public
  API:
  - ``OwnerOperationToolDefinition``: value shape for first-party owner
    operations exposed to the orchestrator alongside installed APIs.
    The ``safety`` field is typed ``Any`` so agent-core stays free of
    the platform's ``OperationSafetyMetadata`` dependency. Pure helpers
    do not read ``safety``; the platform shim accesses it directly.
  - ``to_provider_tool(tool, *, learned_guidance=None)``: dispatch by
    type — ``OwnerOperationToolDefinition`` uses a strict empty-schema
    fallback (``additionalProperties: false``); ``ResolvedToolDefinition``
    composes display_name + description + compact_prompt + usage_hints
    + optional learned-guidance, joins with ``\n``, clips to 1024 chars,
    and falls back to ``tool_name`` when every piece is empty. The
    description composition order is byte-equivalent with the
    pre-v0.5 platform helper.
  - ``build_orchestrate_system_prompt(*, goal, manifest_text, tool_count,
    now, input_schema_map=None, client_input_keys=None,
    planned_tool_names=None, is_revision=False)``: render the manifest +
    role + format-rules + multi-capability buyer-input mapping +
    revision guard + goal sections. Clock is injected via ``now`` so
    the helper is fully pure and testable with a frozen instant
    (mirrors v0.4 ``learning_expiry_for_kind``'s clock-injection
    pattern). Whitespace-only ``manifest_text`` is treated as empty so
    the OWNER DIRECTIVES header is not emitted for blank manifests.
  - ``extract_llm_usage(raw_payload)``: provider-neutral usage
    normaliser — Anthropic ``input_tokens / output_tokens`` and OpenAI
    ``prompt_tokens / completion_tokens`` both fold into
    ``{input_tokens, output_tokens}``. Anthropic-style keys win when
    both shapes are present.
  - ``estimate_usd_cents(model, input_tokens, output_tokens, *,
    price_table=None, fallback_price=FALLBACK_PRICE_PER_MTOKEN_CENTS)``:
    preflight USD-cents estimator for the daily-cap check. Reads the
    public ``DEFAULT_MODEL_PRICE_PER_MTOKEN_CENTS`` table by default,
    rounds UP so the cap stays slightly conservative, and lower-cases
    ``model`` before lookup so casing drift in the caller doesn't
    bypass the cap.
  - ``execution_context_requires_approval(execution_context)`` /
    ``permission_can_run_without_approval(permission_class)``: small
    policy predicates with the same truthy-string set
    (``"true" / "yes" / "1" / "on" / "y"``) as the platform.
    ``permission_can_run_without_approval`` normalises hyphenated
    permission_class strings (``"read-only"``) to the underscore form
    (``"read_only"``) so legacy callers pre-dating the v0.2.2 spelling
    fix don't accidentally force-gate free-to-execute tools behind
    approval.
- Public price-table data: ``DEFAULT_MODEL_PRICE_PER_MTOKEN_CENTS``
  (input/output cents per million tokens for the documented Claude
  + GPT models) and ``FALLBACK_PRICE_PER_MTOKEN_CENTS`` (the mid-tier
  tuple used for unknown models so they neither bypass nor block the
  daily cap).

### Tests

- 39 new unit tests (``tests/test_orchestrate_helpers.py``) covering
  description composition order, schema-fallback strictness divergence
  between owner-operation and installed tool branches, system-prompt
  rendering with frozen clock, manifest whitespace handling,
  multi-capability input-schema-map rendering, planned-tools and
  revision guards, Anthropic vs OpenAI usage shapes, round-up rounding
  in the cents estimator, model-id case insensitivity, custom
  price-table override, the documented price-table pin, and both
  approval predicates' truthy / falsy / drift cases.
- Smoke test pinned to ``__version__ == "0.5.0"``.

## [0.4.0] - 2026-05-02

Tier B Phase 2 cont. The pure decision functions feeding the platform's
``capability_failure_learning`` surface land as public source. The DB
write itself stays in the platform (it needs a SQLAlchemy ``Session``
and a SAVEPOINT for the supersede + insert pair), but the *decisions*
the row encodes — which failure kind, which task family, how long the
avoidance lasts, what the advice text says — are now fully readable as
source.

### Added

- **``siglume_agent_core.capability_failure_learning``** — pure helpers
  the platform composes into its DB-bound record/query entry points.
  Public API:
  - ``failure_kind_from_execution(*, status, api_outcome, details)``:
    classify an execution into one of ``"out_of_coverage"`` /
    ``"policy_or_limit"`` / ``"runtime_unavailable"`` /
    ``"execution_failure"`` / ``None``. Priority: out_of_coverage wins
    over status (a tool that returned but did nothing useful is still
    a coverage gap), then policy/limit keywords, then transient
    runtime keywords, then a generic execution_failure fallback.
  - ``infer_capability_task_family(user_message, goal)``: bucket
    requests into ``"short_translation"`` / ``"long_or_general_translation"``
    / ``"general_request"``. The translator-vs-general split partitions
    learnings so an out_of_coverage card from a long-form domain
    translation doesn't poison short-translation availability.
  - ``learning_expiry_for_kind(failure_kind, *, now)``: return the
    absolute expiry instant (1h transient / 6h policy / 1d execution /
    24h overflow / never for out_of_coverage). ``now`` is a required
    keyword arg so the function is pure — callers (the platform DB
    layer, tests, eval harnesses) supply their own clock.
  - ``learning_scores_for_kind(failure_kind)``: ``(importance,
    confidence)`` tuple in [0, 1] used to rank cards and signal
    LLM weight. Out-of-coverage and overflow score highest because
    their false-positive rate is low.
  - ``build_learning_content(*, tool, failure_kind, task_family,
    request_preview)``: render the human-readable advice string stored
    on the card. Branches by ``failure_kind`` so each kind nudges the
    LLM toward a different next step (try a different tool, retry
    later, satisfy the missing condition).
  - ``build_system_prompt_overflow_content(*, request_preview, fit_meta)``:
    render the advice text for the systemic system-prompt-overflow
    failure (Bug A class). Surfaces required-vs-budget detail when
    available so an operator can size the gap immediately.
  - ``clip_text(value, max_chars=900)``: collapse whitespace and
    truncate with an ellipsis tail. Used for short storage previews.
  - ``api_outcome_from_output(output)`` /
    ``last_tool_output_from_steps(steps)`` /
    ``api_outcome_from_execution(*, structured_output, step_results=None)``:
    walk a multi-step execution and surface ``"out_of_coverage"``
    if any layer affirmatively reports it.
  - Public constants ``CAPABILITY_PREFERENCE_MEMORY_TYPE``,
    ``CAPABILITY_LEARNING_TAG``, ``SYSTEM_PROMPT_OVERFLOW_KIND`` —
    these are the literal strings the platform writes to the
    ``memory_type`` column / ``tags_jsonb`` array / failure_kind
    metadata field, so a rename here would silently invalidate every
    existing memory card.

### Repository pattern (callback-injection, same as v0.3)

The DB-bound entry points (``record_capability_failure_learning``,
``record_system_prompt_overflow_learning``, ``capability_learning_by_tool``,
``should_avoid_tool_for_request``) stay in the platform — they need a
SQLAlchemy ``Session`` and a SAVEPOINT around the supersede + insert
pair. They compose the v0.4 helpers and persist the result however
they like. A future eval harness or CLI replay tool can compose the
same helpers against a different store (in-memory list, JSON file)
without pulling in any DB dependency.

### Clock injection

``learning_expiry_for_kind`` requires ``now`` as a keyword argument so
the function is fully pure — no implicit ``utcnow()``. The platform
passes ``utcnow()`` from its infrastructure layer; tests pass a
frozen instant for deterministic expiry assertions.

### Notes

- 51 new tests in ``tests/test_capability_failure_learning.py`` covering:
  text-clip whitespace + ellipsis behaviour, all branches of the
  outcome / failure-kind classifiers, both keyword priority orders
  (policy > runtime), task-family translation classification (short
  vs long via length / token-count / domain hint, JA + EN),
  per-kind expiry deltas with a frozen clock, the ``now``-keyword-only
  contract, per-kind importance/confidence scoring, all four
  ``build_learning_content`` branches, and the overflow-content
  detail-string toggling (present when both required+budget are
  positive ints, omitted otherwise). Plus a smoke-test entry pinning
  the public import surface.
- Test count: 93 → 147 (53 in the new module + 1 smoke import).
- The platform's ``capability_failure_learning.py`` becomes a thin
  shim: pure helpers re-export from this package, the four DB-bound
  entry points stay private and import the helpers from agent-core.
  Production runs on the same decision code that ships here.

### Roadmap update

The v0.3 entry advertised v0.4 as ``capability_failure_learning`` +
``dev_simulator``. ``dev_simulator`` is **deferred to v0.5+** — its DB
touchpoint is a single capability lookup (cheap to factor) but the
SchemaSanitizer + LLM-call wrapper layered on top of it have absorbed
several monorepo-only changes since the recon (sanitization rules,
Anthropic-name regex, dedupe). Re-extracting cleanly is half-a-day of
work that's better batched with the larger Tier C ``orchestrate`` cut
than rushed into v0.4. v0.4 ships ``capability_failure_learning``
only; the open-readable surface still grows by one full module this
release.

- **v0.5 (Tier C)**: split ``tool_use_runtime.orchestrate`` into a
  pure-planner half (open) and a platform-glue half (private), and
  bring ``dev_simulator`` along in the same cut. The selector +
  prefilter + failure-learning together cover catalog shaping +
  post-execution feedback; orchestrate covers the multi-turn loop in
  between.

## [0.3.0] - 2026-05-02

Tier B Phase 2 release. The keyword-based tool selector — the second
half of the answer to "why didn't my listing get picked?" — lands as
public source. The first half (``installed_tool_prefilter``) shipped
in v0.2.0; together they cover the full LLM-visible tool catalog
shaping path.

### Added

- **``siglume_agent_core.tool_selector``** — dispatch-time keyword
  scorer extracted from the platform's ``ToolSelector`` class. Pure
  Python, no DB, no I/O. Public API:
  - ``select_tools(tools, request_text, *, max_candidates=5,
    include_missing_accounts=False, on_unmatched=None,
    redactor=None) -> list[ResolvedToolDefinition]``: rank candidates
    by overlap, readiness, approval friction, and cost; hard-filter
    missing-account action / payment tools by default; emit gap
    signals via the optional callback for the three documented
    miss conditions.
  - ``UnmatchedRequestSignal``: frozen dataclass surfaced to the
    caller's ``on_unmatched`` sink. Carries the redacted request
    sample, sorted-unique stop-word-filtered token list, and a
    SHA-256 shape hash over the first 16 tokens — same shape as
    the platform's ``UnmatchedCapabilityRequest`` row, minus
    host-specific identifiers (caller captures those in closure).
  - ``strip_long_alphanumeric_secrets(text)``: Q4-style backstop
    that catches long hex / base64 runs a primary key-pattern
    redactor might miss. Public so callers can chain it after
    their own redactor in ``select_tools``'s ``redactor`` arg.
  - ``MissKind`` Literal, ``OnUnmatchedCallback`` /
    ``RedactorCallback`` aliases, plus the ``DEFAULT_MAX_CANDIDATES``
    / ``UNMATCHED_STOP_WORDS`` / ``UNMATCHED_MAX_TOKENS_FOR_HASH``
    / ``UNMATCHED_TEXT_MAX_LEN`` public constants.

### Repository pattern (callback-injection, not full DI)

The platform's ``ToolSelector`` persisted gap signals via SQLAlchemy
``session.begin_nested()`` (SAVEPOINT). Extracting that meant
choosing between two repository patterns:

1. Inject a full ``UnmatchedRequestRepository`` interface that
   agent-core depends on at the type level.
2. Inject a single ``on_unmatched: Callable[[UnmatchedRequestSignal],
   None]`` callback that the caller wraps however it wants.

This release ships option 2: a plain callable, no abstract base
class, no protocol. Rationale: the platform writes one row per miss;
a CLI / eval harness might just append to a list; a queue worker
might enqueue a message. A formal interface would impose a single
storage shape on every host, which is overkill for a single-method
repository. If a future module ever needs richer query-back semantics
(``load_recent_misses(...)``, ``aggregate_by_shape(...)``) it'll
introduce a proper protocol then; this module never reads back.

Exception isolation: callback failures are caught and logged at
WARNING. agent-core never raises out of a telemetry sink — the
request path keeps running even if the gap-report storage is offline.

### Notes

- 41 new tests in ``tests/test_tool_selector.py`` covering empty-input
  short-circuits, scoring (overlap + readiness + auto-readonly +
  approval-friction + cost), the missing-account hard filter (and its
  ``include_missing_accounts`` escape hatch), all three miss-kinds,
  hash determinism across word-order, the 16-token cap, the 200-char
  text cap, redactor exception isolation, callback exception
  isolation, and the closure-capture pattern for caller context.
  Plus a smoke-test entry pinning the public import surface.
- Test count: 51 → 93.
- The platform's ``ToolSelector`` becomes a thin shim that wires the
  ``session.begin_nested()`` SAVEPOINT into the ``on_unmatched``
  callback and lazy-imports the existing ``preview_redaction``
  module as the ``redactor``. Production runs on the same selection
  code that ships here.

### Roadmap (no change)

- **v0.4 (Tier B Phase 2 cont.)**: pure halves of ``dev_simulator``
  and ``capability_failure_learning``. Same callback pattern applies
  for their (smaller) DB touchpoints.
- **v0.5 (Tier C)**: split ``tool_use_runtime.orchestrate`` into a
  pure-planner half (open) and a platform-glue half (private). The
  selector + prefilter together cover catalog shaping; orchestrate
  covers the multi-turn loop.

## [0.2.6] - 2026-05-02

Older codex-bot review findings on PRs #1 (release.yml) and #2
(installed_tool_prefilter) — the v0.2.5 batch only covered #4-#6, this
release closes the rest.

### Fixed

- **release.yml mistag risk (P2, was in PR #1).** The Trusted
  Publishing workflow ran on any `v*` tag without checking that the
  pushed tag matched `project.version` in `pyproject.toml`. A mistag
  (e.g. pushing `v0.3.0` while `pyproject.toml` was still `0.2.5`)
  would silently publish 0.2.5 under the wrong release notes, or
  fail later with a confusing 400 if the version was already on
  PyPI. Added a "Verify tag matches pyproject.toml version" step
  before `python -m build` that compares the two and aborts with
  a remediation hint if they disagree. Belt-and-suspenders against
  what is so far a hand-walked process.
- **Latin tokenizer fragmented non-ASCII words (P2, was in PR #2).**
  The v0.2.0 `installed_tool_prefilter._LATIN_TOKEN_RE` matched only
  `[A-Za-z0-9_]`, so words with diacritics fragmented:
  `überweisung` → `berweisung`, `tradução` → `tradu`. TF-IDF then
  failed to match a multilingual user query against a tool whose
  description used the same word with diacritics, forcing the
  prefix fallback when relevance was actually high. Widened to
  `\w` with re.UNICODE, but with a negative lookahead that excludes
  CJK code points so the CJK matcher's bigram path does not
  double-count Japanese / Chinese text.

### Notes

- Test count: 49 → 51 (+2 regression tests for diacritics and
  CJK-vs-Latin disjointness).
- Parity fixtures unchanged — `score_manual_quality` uses an
  ASCII-only word extractor which is unaffected by the prefilter
  tokenizer change.
- All v0.2 audit findings now closed:
  - #1 README v0.2 → v0.2.1 (partial), full reflection across releases
  - #2-#10 → v0.2.1 / v0.2.2 / v0.2.3 / v0.2.4 / v0.2.5 / v0.2.6

## [0.2.5] - 2026-05-02

Codex-bot review pass on PRs #4 / #5 / #6 (v0.2.2 / v0.2.3 / v0.2.4)
flagged four real issues. All four are fixed in this release.

### Fixed

- **CI matrix bug (P1, was in v0.2.2).** `.github/workflows/ci.yml`
  declared `python-version` as a matrix dimension and listed extras
  via `matrix.include`. Per GitHub Actions semantics each `include`
  entry merges into existing combinations and a later `include` with
  the same shape overwrites earlier ones — so both extras entries
  collapsed onto the same `python-version` combinations and the
  second (`all-extras`) overwrote the first (`core-only`). Net
  effect: the core-only install was never exercised, defeating the
  whole point of the v0.2.1 lazy-import contract verification.
  Switched to two real matrix dimensions
  (`python-version × extras`) so the four-job matrix actually runs.
- **Injection blacklist false-positive (P1, was in v0.2.3).** The
  marker `"act as if"` is too common in legitimate technical copy
  ("if omitted, treat as if value is 0") to remain in a
  hard-rejection list. Removed; other 28 markers retained.
- **Description check over-broad (P2, was in v0.2.3).**
  `_check_property_descriptions` walked the schema starting from the
  root and checked every node's `description` — including the root
  `input_schema.description`, which is not a property description
  and never reaches the LLM tool catalog block. A long or
  marker-containing root description that was always accepted before
  v0.2.3 would now fail validation, a backward-compat regression.
  Refactored: the helper now only checks descriptions on actual
  properties, array `items` schemas, and composition-branch schemas
  (`oneOf` / `anyOf` / `allOf` entries). Root description is left
  alone, restoring v0.2.2 behaviour for legitimate publisher copy.
- **Parity coverage guard (P2, was in v0.2.4).**
  `test_fixture_set_covers_a_through_f_grade_range` previously
  required only `{"A", "B", "F"}` even though the docstring claimed
  to pin `A, B, C, F`. A future PR could silently drop the C-grade
  fixture without failing the test. Tightened to require all four
  documented grades.

### Notes

- Test count: 46 → 49 (+3 regression tests for the v0.2.5 fixes).
- All four findings credit chatgpt-codex-connector[bot]'s automated
  review on PRs #4 / #5 / #6.

## [0.2.4] - 2026-05-02

Public parity fixture release. Closes review item #9 from the v0.2
external audit. No code changes; new fixture set + parity test +
README update only.

### Added

- `tests/fixtures/manuals/` — four representative tool manuals
  (high / medium / low / structurally-broken quality) covering the
  A / B / C / F grade range.
- `tests/fixtures/expected_scores.json` — frozen snapshot pinning
  each fixture's `grade`, `overall_score`, and
  `keyword_coverage_estimate` from the v0.2.4 scorer.
- `tests/test_quality_score_parity.py` — parametrized parity tests
  asserting exact byte-equivalent output. External contributors can
  now verify the README claim "production runs the same code that's
  visible here" themselves: `pip install siglume-agent-core`, clone
  this repo, `pytest tests/test_quality_score_parity.py`. The
  Siglume monorepo's runtime depends on the same PyPI package since
  v0.2.0, so the parity is real, not advertised.
- README quickstart updated with the verification recipe inline.

### Notes

- 6 new tests in `test_quality_score_parity.py`. Test count: 40 -> 46.
- The snapshot's `_grade_thresholds` field is cross-checked against
  `_overall_to_grade` so the documentation cannot drift away from
  the scorer's actual boundaries.
- Future scorer changes that move any score must update both the
  fixture's expected entry AND mention the user-visible behaviour
  change in the CHANGELOG. The test message guides PR authors to
  the right remediation.

## [0.2.3] - 2026-05-02

Validator hardening release. Closes review items #7 and #8 from the
v0.2 external audit. Forward-compatible: every legitimate publisher
manual that passed v0.2.2 still passes; only adversarial / malformed
schemas are newly rejected.

### Added

- **Property-description length cap** (`MAX_PROPERTY_DESCRIPTION_LEN
  = 500`). Property descriptions in `input_schema` get embedded into
  the LLM tool catalog block at runtime, so a malicious publisher
  could otherwise plant a manual page worth of instructions there.
  500 chars is generous enough that legitimate field documentation
  (units, formats, examples) fits; the bar is "no manual page
  disguised as a description".
- **Prompt-injection pattern detection** in property descriptions.
  29-pattern allowlist of known jailbreak / prompt-leak markers
  ("ignore previous instructions", chat-template tokens like
  `<|im_start|>` / `[INST]`, JA equivalents like `前の指示を無視`).
  Conservative — substring match only, exact-marker focus, zero
  false positives on the legitimate-publisher-copy regression set.
- **Recursive platform-injected fields check.** `validate_input_schema`
  previously checked only root-level `properties` for collisions with
  `PLATFORM_INJECTED_FIELDS` (`execution_id`, `trace_id`,
  `connected_account_id`, `dry_run`, `idempotency_key`,
  `budget_snapshot`). v0.2.3 walks every nesting level — nested
  object schemas, array `items`, and `oneOf`/`anyOf`/`allOf` branches
  — so a publisher cannot smuggle `trace_id` under a nested object
  to collide with platform-set values at runtime. Mirrors the
  existing `_check_forbidden_key` traversal.

### Notes

- 14 new tests in `tests/test_validator_hardening.py` covering: at-
  limit / over-limit length boundaries; nested + array-items
  description checks; case-insensitive injection match;
  Japanese-language injection; chat-template marker patterns;
  legitimate-copy false-positive guards; recursive platform-injected
  detection at nested / array / `oneOf` paths; non-flagged similar
  property names.
- Test count: 26 → 40.
- v0.2.2's CI workflow exercises both core-only and all-extras
  installs against this release.

## [0.2.2] - 2026-05-02

Repository hygiene release. Closes review items #4, #6, #10 from the
v0.2 external audit. No behavior changes; type tightening only.

### Added

- **`SECURITY.md`** — vulnerability disclosure policy. Reports go to
  `siglume@energy-connect.co.jp`. Acknowledgement within 3 business
  days, remediation plan within 14 calendar days for confirmed
  issues. Scope limited to this package; out-of-scope reports
  forwarded.
- **`.github/workflows/ci.yml`** — pytest + ruff (lint + format check)
  + sdist/wheel build on every PR and push to `main`. Runs against a
  matrix of `(Python 3.11 / 3.12) × (core-only / all-extras)` so the
  v0.2.1 lazy-import contract is verified on each change. Catches the
  class of bug v0.2.1 fixed (broken README example, top-level imports
  of optional extras) before a release goes out.
- **`PermissionClass` and `AccountReadiness` typed `Literal` aliases**
  in `siglume_agent_core.types`. Replaces the previous `str` typing on
  `ResolvedToolDefinition.permission_class` / `.account_readiness`,
  preventing the historical `read-only` (hyphen) / `read_only`
  (underscore) drift from re-occurring. The underscore form
  (`read_only`) is canonical, matching `tool_manual_validator`'s
  `VALID_PERMISSION_CLASSES` set.

### Fixed

- **Test inconsistency**: `tests/test_installed_tool_prefilter.py`
  used `permission_class="read-only"` (hyphen) while the validator
  accepts only `"read_only"` (underscore). Tests now use the
  canonical underscore form.

### Notes

- New regression tests pin `PermissionClass` against
  `VALID_PERMISSION_CLASSES` so future drift fails CI immediately
  instead of slipping through to a downstream submission.
- Test count: 24 → 26.

## [0.2.1] - 2026-05-02

Bugfix release addressing three findings from external review of the v0.2
public surface. No new public APIs; all changes are corrections to v0.2
behaviour and documentation.

### Fixed

- **README example was broken.** The v0.2 quickstart referenced
  `quality.publishable`, a field that does not exist on `QualityScore`.
  Replaced with a `quality.grade in ("A", "B")` check that matches the
  platform's actual publish-time gate.
- **Provider adapters could not be imported in core-only installs.**
  `siglume_agent_core.provider_adapters.anthropic_tools` and
  `openai_tools` imported the SDK at module top level, so
  `pip install siglume-agent-core` (without the `[anthropic]` /
  `[openai]` extras) failed at module-import time even when the user
  never intended to construct an adapter. The SDK is now lazy-imported
  inside the adapter constructor; if the matching extra is missing the
  constructor raises `ImportError` with the exact `pip install` command
  to fix it. Module imports always succeed.
- **`AnthropicToolAdapter` did not honour `tool_choice="none"`.**
  Anthropic's API has no direct `"none"` mode, and the adapter mapped
  `"none"` -> `{"type": "auto"}` while still sending the tools array,
  so the LLM remained able to emit `tool_use` blocks. The adapter now
  elides both `tools` and `tool_choice` from the request when
  `tool_choice="none"`, matching OpenAI's `"none"` semantics. Critical
  for action / payment-class capabilities sharing the adapter — relying
  on textual hints to "not use tools" is unreliable.

### Added

- README v0.2 reflection: `installed_tool_prefilter` quickstart added,
  the v0.1-era `Phase 1 / Tier A` heading replaced with
  `v0.2, Tier A + Tier B Phase 1`, optional-extras install instructions
  documented.
- 4 new regression tests in `tests/test_smoke.py`:
  - Constructor raises actionable `ImportError` when SDK is absent
    (Anthropic + OpenAI).
  - `tool_choice="none"` elides `tools` and `tool_choice` from the
    Anthropic request payload.
  - `tool_choice="auto"` still sends them (counter-test against
    accidental over-elision).

## [0.2.0] - 2026-05-01

### Added

- **Tier B extraction (Phase 1).** The first Tier B module lands as the
  cleanest cut: a 100% pure scorer with zero DB / network dependency.
- `siglume_agent_core.installed_tool_prefilter` — TF-IDF + cosine
  similarity over Latin words and CJK character bigrams. Picks the top-N
  most-relevant tools when an agent has many bound, so the chat system
  prompt stays within the input token budget. Public surface:
  `select_top_tools_for_prompt(installed_tools, user_message, *, max_tools=50)`.
- `siglume_agent_core.types.ResolvedToolDefinition` — the value-shape
  prefilter and (future) Tier B modules accept. Pure dataclass, mirrors
  the platform's resolver output.

### Background

The recurring publisher question for v0.2 was "why didn't my listing get
picked when it matched?" — the answer is partly in this module. The
selection scorer is the open-readable half. Future v0.2.x releases will
add the keyword-trigger scorer (currently `ToolSelector._score`) once
its DB-coupled portions are factored behind a repository interface.

The siglume monorepo's `installed_tool_prefilter.py` becomes a thin
re-export shim of this package's symbols — production runs on the same
code that ships here.

### Roadmap

- **v0.3 (Tier B Phase 2)**: extract `tool_selector` keyword scorer,
  pure halves of `dev_simulator` and `capability_failure_learning`.
  Requires a repository-interface pattern (`SaveUnmatchedRequest`,
  `LoadMemoryCardsByAgent`) for the DB-touching code paths.
- **v0.4 (Tier C)**: split `tool_use_runtime.orchestrate` into a
  pure-planner half (open) and a platform-glue half (private).

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full extraction plan.

### Distribution

Install:
```bash
pip install siglume-agent-core            # core only
pip install siglume-agent-core[anthropic] # + Anthropic adapter
pip install siglume-agent-core[openai]    # + OpenAI adapter
```

## [0.1.0] - 2026-05-01

### Added

- **Initial public release.** Phase 1 (Tier A) extraction of the Siglume API Store
  agent runtime's open-core orchestrator logic from the private monorepo.
- `siglume_agent_core.tool_manual_validator` — the same tool-manual quality
  scorer (grade A-F) the platform runs at submission. Use locally with
  `validate_tool_manual()` and `score_manual_quality()` to predict your
  manual's grade before publishing.
- `siglume_agent_core.provider_adapters.types` — common abstractions
  (`ToolMessage`, `ToolTurnResult`, `NormalizedToolCall`, `ProviderToolDefinition`).
- `siglume_agent_core.provider_adapters.anthropic_tools` — Anthropic tool-use
  API adapter (`AnthropicToolAdapter.run_turn(...)`).
- `siglume_agent_core.provider_adapters.openai_tools` — OpenAI tool-use API
  adapter (`OpenAIToolAdapter.run_turn(...)`) with the model-aware kwarg
  dispatcher (`max_completion_tokens` for the GPT-5 / o1 / o3 reasoning
  families, `max_tokens` for GPT-4 / 3.5).

### Background

Triggered by the publisher-dev-tools observability initiative tracked in
[`siglume-api-sdk#195`](https://github.com/taihei-05/siglume-api-sdk/issues/195).
The recurring publisher question "how do I know if my API will get picked
by the orchestrator?" surfaced a deeper need: the most useful answer was
to make the planner's logic readable as source, not just summarized via
new dashboards. This package is that answer.

The siglume monorepo now imports from this package as the single source
of truth for Tier A modules — production runs on the same code that's
visible here.

### Roadmap

- **v0.2 (Tier B)**: extract `tool_selector`, `installed_tool_resolver`,
  `installed_tool_prefilter`, `dev_simulator`, `seller_analytics`,
  `capability_failure_learning`. The keyword-based selection scorer that
  decides "which tool wins for this offer" lands here — once public,
  "why didn't my listing get picked?" becomes a `git blame`-able answer.
- **v0.3 (Tier C)**: split the orchestrator's tool-use loop
  (`tool_use_runtime`) into a pure-planner half (open) and a
  platform-glue half (private).

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full extraction plan.

### License

[AGPL-3.0-only](LICENSE). Commercial license available for users who
can't operate under AGPL terms (e.g., closed-source self-hosting) —
contact `siglume@energy-connect.co.jp`.

### Distribution

PyPI: https://pypi.org/project/siglume-agent-core/

Install:
```bash
pip install siglume-agent-core           # core only
pip install siglume-agent-core[anthropic] # + Anthropic adapter
pip install siglume-agent-core[openai]    # + OpenAI adapter
```
