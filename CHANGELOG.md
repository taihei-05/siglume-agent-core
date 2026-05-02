# Changelog

All notable changes to `siglume-agent-core` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
once it reaches v1.0. Until then, minor versions (v0.x) may rename or restructure
public API while extraction from the private monorepo is in progress.

## [Unreleased]

(no changes)

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
