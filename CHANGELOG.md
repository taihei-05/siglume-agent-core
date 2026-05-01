# Changelog

All notable changes to `siglume-agent-core` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches v1.0. Until then, minor versions (v0.x) may rename or restructure
public API while extraction from the private monorepo is in progress.

## [Unreleased]

(no changes)

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
