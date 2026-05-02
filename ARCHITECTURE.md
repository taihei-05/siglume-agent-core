# Architecture & Roadmap

`siglume-agent-core` is a staged extraction from the Siglume marketplace's private monorepo. This document captures **what's open, what's still private, and what the staged history looked like**.

## Scope tiers

The Siglume agent runtime is roughly 10 KLOC of orchestrator code in the private monorepo. Each module is classified by extraction difficulty:

| Tier | Description | LOC | Status |
|---|---|---|---|
| **A** | Zero external dependencies — pure logic | ~1,500 | ✅ Released (v0.1) |
| **B** | DB-model dependency only — extractable via Protocols + abstract dataclasses | ~2,400 | ✅ Released (v0.2 + v0.3 + v0.4) |
| **C** | Heavy platform-service coupling — split into pure-planner + platform-glue via callback bag | ~2,700 | ✅ Released (v0.5 + v0.6 + v0.7) |
| **D** | Security / business logic — stays in private monorepo | ~3,200 | ❌ Will not be open-sourced |
| **E** | Utility / boundary normalizers | ~250 | 🟡 Partially extracted (some absorbed into v0.2–v0.4) |

**Tier A + B + C extraction is complete as of v0.7.** The OSS package now covers ~70% of the original orchestrator code; the remaining ~30% is Tier D (security boundary / payment / KYC).

---

## Released phases

### Phase 1 (v0.1) — Tier A

Lowest-risk extraction. No `agent_sns.*` imports, lifted as-is. Answers the most common publisher question first ("why was my manual graded X?").

**Modules:**

- `siglume_agent_core.tool_manual_validator` (~1,080 LOC) — manual quality scoring (grade A–F), with `validate_tool_manual` + `score_manual_quality`
- `siglume_agent_core.provider_adapters.types` (~40 LOC) — provider-neutral tool-use abstractions
- `siglume_agent_core.provider_adapters.anthropic_tools` (~190 LOC) — Anthropic tool-use API adapter
- `siglume_agent_core.provider_adapters.openai_tools` (~200 LOC) — OpenAI tool-use API adapter

### Phase 2 — Tier B (split across v0.2, v0.3, v0.4)

Tier B modules originally imported SQLAlchemy ORM models. The extraction introduced **Protocols** that the platform's ORM models satisfy structurally — no inheritance, no shared model code. The platform converts ORM ↔ value objects at the public/private boundary.

| Version | Module | What it does |
|---|---|---|
| **v0.2** | `installed_tool_prefilter` | TF-IDF + cosine ranking that picks the top-N most-relevant tools when an agent is bound to many |
| **v0.3** | `tool_selector` | Dispatch-time keyword scorer + `select_tools()` + `extract_trigger_words()` + `strip_long_alphanumeric_secrets()` + `UnmatchedRequestSignal` (frozen dataclass for "why didn't it pick me?" gap signals) |
| **v0.4** | `capability_failure_learning` | Pure decision functions feeding the platform's tool-failure memory cards: `failure_kind_from_execution`, `infer_capability_task_family`, `learning_expiry_for_kind(*, now)` (clock-injected), `learning_scores_for_kind`, `build_learning_content`, `build_system_prompt_overflow_content` + 3 constants |

**Why split across 3 minor versions:** each module brought its own callback contract (`on_unmatched`, `redactor`, `now`-injection). Releasing one at a time let the monorepo shim PR reduce platform code in three discrete steps, each independently reviewable.

### Phase 3 — Tier C (split across v0.5, v0.6, v0.7)

`tool_use_runtime.py` (~2,700 LOC) and `dev_simulator.py` (~375 LOC) mixed pure orchestration with platform-specific concerns (intent persistence, owner-operation invocation, gateway dispatch, billing, DB queries, Anthropic SDK). Tier C extraction split each into a **pure planner** (here) + a **platform-glue layer** (still in the monorepo) that wires the planner to live agent state via a frozen-dataclass callback bag.

| Version | Module | What it adds |
|---|---|---|
| **v0.5** | `orchestrate_helpers` | `OwnerOperationToolDefinition`, `to_provider_tool`, `build_orchestrate_system_prompt` (clock-injected), `extract_llm_usage`, `estimate_usd_cents` + `DEFAULT_MODEL_PRICE_PER_MTOKEN_CENTS`, approval predicates |
| **v0.6** | `orchestrate` | `run_orchestrate_loop(...) -> OrchestrationOutcome` — the per-iteration LLM tool-use loop body itself, with `OrchestrationDispatcher` (frozen dataclass, 5 callables: check_policy / execute_read_only / execute_dry_run / dispatch_owner_operation / emit_awaiting_approval). Cross-provider fallback (`CROSS_PROVIDER_FALLBACK_MODEL`) on iter-0 OpenAI failures |
| **v0.7** | `dev_simulator` | `simulate_planner` + `select_candidates` + `filter_tools_for_anthropic` + 4 pure helpers + `LLMSimulateCall` callable contract + `ProductListingLike` / `CapabilityReleaseLike` Protocols. The "would the planner pick my API?" pre-publish dry run engine |

**Effect on the platform's monorepo (figures from the matching shim PRs in the private monorepo):**

- `ToolUseRuntime.orchestrate` — roughly two-thirds reduction; the loop body itself moves here, the platform shim retains only intent fetch / preflight / dispatcher wiring / receipt + outbox bookkeeping (v0.6 shim PR)
- `capability_runtime/dev_simulator.py` — roughly 40% reduction; pure stages (keyword pre-filter, dedupe / regex skip, predicted-chain reconstruction) move here, the platform shim retains only the DB query and the Anthropic SDK call (v0.7 shim PR)
- `capability_runtime/capability_failure_learning.py` — roughly one-third reduction; 10 pure decision functions + 3 constants move here, the platform retains 4 DB-bound entry points (v0.4 shim PR)
- `installed_tool_resolver.ToolSelector` — roughly one-half reduction; scoring + miss-kind classification moves here (v0.3 shim PR)

Exact pre-/post line counts are tracked in the corresponding shim PR descriptions in the monorepo. Each shim retains only what *must* stay private (DB queries, SDK call sites, intent state writes) and delegates everything else to this package. Same code path runs in production.

---

## What stays private (Tier D)

These modules will **not** be open-sourced in any phase:

| Module | Reason |
|---|---|
| `capability_gateway` | Security boundary — auth, rate limit, payment integration |
| `connected_account_broker` | Manages OAuth tokens; exposing creates attack surface |
| `agent_execution_runtime` | Owner-operation flow, business logic |
| `sandbox_test_runner` | Touches credentials |
| `capability_dispatcher` | Live credential plumbing to provider APIs |
| Payment / wallet signing | Direct money risk if exposed |
| KYC / AML decisioning | Exposing rules invites bypass |
| Production DB schema & data | Privacy, business intelligence |

The publicly-extracted parts consume these as **abstract callbacks / Protocols** so the OSS package never imports private implementations. Self-hosters can substitute their own.

---

## Possible future extractions (not scheduled)

Tier B/E modules that *could* be extracted but are not currently scheduled — extraction will happen if there's a publisher use case that needs them:

| Candidate | Rough LOC | Reason it's not scheduled yet |
|---|---|---|
| `seller_analytics` | ~400 | Selection-miss derivations + keyword-suggestion logic. Most of the inputs are already public (`tool_selector` + `capability_failure_learning`); a follower release could compose the analytics on top |
| `execution_adapter` | ~200 | Adapter config normalization. Needed only if a publisher writes their own `AppAdapter` runtime; today they use `siglume-api-sdk` |
| `connected_account_requirements` | ~150 | Requirements normalization (no secrets). Lightweight; would primarily reduce duplicate code in third-party tooling |

If any of these block your work, file an issue at <https://github.com/taihei-05/siglume-agent-core/issues> and we'll prioritize.

---

## Versioning & stability

- **0.x**: API may rename or restructure between minor versions while extraction is in progress. Each minor version (`0.x.0`) is a coordinated release with the monorepo shim PR that consumes it.
- **1.0**: declared once Tier A + B + C are extracted (✅ done as of v0.7) **and** the public/private interface boundary has soaked for at least one release cycle without breaking changes. The plan is to declare 1.0 if no Phase 4 extractions land in the next quarter and the existing surface is stable.

---

## License & relicensing

This repository is **AGPL-3.0-only**. The Siglume monorepo, when it imports this package, is also AGPL-affected for the parts that depend on it — but the rest of the monorepo (private Tier D modules, payment, web frontend, etc.) is *not* a derivative work and remains under its own license.

Commercial licensing is available for users who can't operate under AGPL terms (e.g., closed-source self-hosting). Contact `siglume@energy-connect.co.jp`.

---

## Companion repository

[`siglume-api-sdk`](https://github.com/taihei-05/siglume-api-sdk) (MIT) is the publisher-side SDK — `AppAdapter` base class, CLI, `ToolManual` schema, settlement helpers. Together the two repos cover the full **publish → select → execute → learn** loop. See the [main README](README.md#companion-repository-siglume-api-sdk) for the journey diagram.
