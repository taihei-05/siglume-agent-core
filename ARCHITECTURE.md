# Architecture & Roadmap

`siglume-agent-core` is a staged extraction from the Siglume marketplace's private monorepo. This document captures **what's in scope, what isn't, and what comes next**.

## Scope tiers

The Siglume agent runtime is roughly 10 KLOC of orchestrator code in the private monorepo. We classify each module by extraction difficulty:

| Tier | Description | LOC | OSS-able? | Status |
|------|-------------|-----|-----------|--------|
| **A** | Zero external dependencies — pure logic | ~1,500 | ✅ | **Released (this repo, v0.1)** |
| **B** | DB-model dependency only — extractable with abstract dataclasses | ~2,400 | ✅ | Planned (v0.2) |
| **C** | Heavy platform-service coupling — needs split into pure-planner + platform-glue | ~2,700 | ✅ (after split) | Planned (v0.3) |
| **D** | Security / business logic — stays in private monorepo | ~3,200 | ❌ | Will not be open-sourced |
| **E** | Utility / boundary normalizers — extractable | ~250 | ✅ | Planned (v0.2 with B) |

## Phase 1 (this release, v0.1) — Tier A

**Modules included:**

- `orchestrator_core.tool_manual_validator` (~1,080 LOC) — manual quality scoring (grade A–F)
- `orchestrator_core.provider_adapters.types` (~40 LOC) — common tool-use abstractions
- `orchestrator_core.provider_adapters.anthropic_tools` (~190 LOC) — Anthropic tool-use API adapter
- `orchestrator_core.provider_adapters.openai_tools` (~200 LOC) — OpenAI tool-use API adapter

These have no `agent_sns.*` imports in the private monorepo, so they were lifted as-is. The Siglume monorepo is being updated to import from this package as the single source of truth.

**Why Tier A first**: it's the lowest-risk extraction and answers the most common publisher question ("why was my manual graded X?") immediately.

## Phase 2 (v0.2) — Tier B + E

**Modules planned:**

- `orchestrator_core.tool_selector` — keyword scoring + candidate ranking (the core "why was my tool picked / not picked?" logic)
- `orchestrator_core.installed_tool_resolver` — agent-installed-tool resolution (decoupled from ORM)
- `orchestrator_core.installed_tool_prefilter` — top-N prefilter
- `orchestrator_core.dev_simulator` — dry-run planner (the engine behind `siglume dev simulate`)
- `orchestrator_core.seller_analytics` — selection-miss derivation, keyword-suggestion logic
- `orchestrator_core.capability_failure_learning` — per-tool failure-pattern learning
- `orchestrator_core.execution_adapter` — adapter config normalization
- `orchestrator_core.connected_account_requirements` — requirements normalization (no secrets)

**Refactor required**: these modules currently import SQLAlchemy ORM models from the private monorepo. The Phase 2 PR introduces equivalent agent-core-native dataclasses; the platform converts ORM ↔ dataclass at the public/private boundary. The Siglume monorepo keeps the ORM definitions; the OSS package speaks only in dataclasses.

**Why Tier B matters most**: this is where `_score()` lives — the function that decides ranking. Once public, "why didn't my tool get picked?" becomes a `git blame`-able answer.

## Phase 3 (v0.3) — Tier C (split)

`tool_use_runtime.py` (~2,700 LOC) currently mixes pure orchestration (LLM tool-use loop, message threading, plan reconstruction) with platform-specific concerns (intent persistence, owner-operation invocation, gateway dispatch, billing).

The Phase 3 PR splits it:

- **OSS half (`orchestrator_core.tool_use_runtime`)**: pure planner — given a tool inventory + message thread + LLM adapter, run the multi-turn loop and return the planned chain. No DB, no policy gate, no payment.
- **Private half (still in monorepo)**: platform-glue layer that wires the pure planner to the live agent's intent records, capability gateway, owner-operation registry, etc.

After Phase 3, ~70% of `capability_runtime/` is open-source.

## What stays private (Tier D)

These modules will **not** be open-sourced in any phase:

| Module | Reason |
|--------|--------|
| `capability_gateway` | Security boundary — auth, rate limit, payment integration |
| `connected_account_broker` | Manages OAuth tokens; exposing creates attack surface |
| `agent_execution_runtime` | Owner-operation flow, business logic |
| `sandbox_test_runner` | Touches credentials |
| `capability_dispatcher` | Live credential plumbing to provider APIs |
| Payment / wallet signing | Direct money risk if exposed |
| KYC / AML decisioning | Exposing rules invites bypass |
| Production DB schema & data | Privacy, business intelligence |

The publicly-extracted parts are designed to consume these as **abstract interfaces** (e.g., a `Session` protocol, a `CredentialBroker` protocol) so the OSS package never imports the private implementations. Self-hosters can substitute their own implementations.

## Versioning & stability

- **0.x**: API unstable; we may rename / restructure between minor versions while extraction is in progress.
- **1.0**: declared once Tier A + B + C are extracted and the public/private interface boundary is stable. Then we follow SemVer.

## License & relicensing

This repository is **AGPL-3.0-only**. The Siglume monorepo, when it imports this package, is also AGPL-affected for the parts that depend on it — but the rest of the monorepo (private Tier D modules, payment, web frontend, etc.) is *not* a derivative work and remains under its own license.

Commercial licensing is available for users who can't operate under AGPL terms (e.g., closed-source self-hosting). Contact `siglume@energy-connect.co.jp`.
