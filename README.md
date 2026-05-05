# siglume-agent-core

[![PyPI](https://img.shields.io/pypi/v/siglume-agent-core.svg)](https://pypi.org/project/siglume-agent-core/)
[![License: AGPL-3.0](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

**The decision logic of the [Siglume API Store](https://siglume.com) agent runtime, extracted from the private monorepo as an AGPL-licensed open-core package.**

If you publish APIs to the Siglume API Store via [`siglume-api-sdk`](https://github.com/taihei-05/siglume-api-sdk), this repository is the answer to **"how does my API actually get picked, scored, and called?"** — the same code path runs in production, byte-for-byte.

---

## What this is, and what it isn't

| Open here (this repo, AGPL-3.0) | Stays private (the platform) |
|---|---|
| Tool manual quality scoring (A–F grade) | Authentication / OAuth credential leasing |
| Tool selection (TF-IDF prefilter + keyword scorer) | Payment processing & wallet signing |
| Orchestrate loop (LLM tool-use loop, system prompt build) | Production database schema & data |
| Per-provider adapters (Anthropic / OpenAI) | Per-buyer KYC / AML decisioning |
| Per-tool failure learning | Marketplace pricing & fee logic |
| Publisher dev simulator (pre-publish dry run) | The execution gateway (security/policy boundary) |

The platform is a hosted service — publishers, buyers, payments, identity, and deployment infrastructure all stay private. Only the **algorithms that decide things** are open: how manuals are scored, how tools are picked, how the LLM loop runs, how failures are learned from.

---

## How your API actually gets selected — the full pipeline

Every module in this repo plays one stage of the pipeline below. **The decision stages listed here are open source**, byte-equivalent with production. The platform's monorepo imports this package; the same code path runs whether you `pip install` it locally or hit `siglume.com`.

```
   ┌────────────────────────────────────────────────────────────┐
   │  Pre-publish (you, on your machine)                        │
   ├────────────────────────────────────────────────────────────┤
   │   tool_manual_validator   ── grade A-F your tool manual    │  v0.1
   │   dev_simulator           ── dry-run "would the planner    │  v0.7
   │                              pick my API for this offer?"  │
   └────────────────────────────────────────────────────────────┘
                              │
                              │   you publish via siglume-api-sdk
                              ▼
   ┌────────────────────────────────────────────────────────────┐
   │  Runtime (a buyer's agent receives a request)              │
   ├────────────────────────────────────────────────────────────┤
   │   1. installed_tool_prefilter                              │  v0.2
   │      TF-IDF top-N from the agent's installed tool pool     │
   │                              │                             │
   │                              ▼                             │
   │   2. tool_selector                                         │  v0.3
   │      Keyword score + permission gate → top-K candidates    │
   │      (this is the "why was my tool picked / not picked?"   │
   │       function — `select_tools()`)                         │
   │                              │                             │
   │                              ▼                             │
   │   3. orchestrate_helpers + orchestrate                     │  v0.5 / v0.6
   │      Build the system prompt, run the multi-turn LLM       │
   │      tool-use loop, accumulate token usage / cost          │
   │                              │                             │
   │                              ▼                             │
   │   4. provider_adapters (anthropic_tools / openai_tools)    │  v0.1
   │      Convert the planned call to the provider's tool-use   │
   │      format and parse the response                         │
   │                              │                             │
   │                              ▼                             │
   │   5. capability_failure_learning                           │  v0.4
   │      On failure: write a learning card so the agent avoids │
   │      this tool for this kind of request for N hours        │
   └────────────────────────────────────────────────────────────┘
```

**Pre-condition for stage 1**: the buyer's agent has to have *installed* your API first — that's the SDK side (`siglume-api-sdk`) — earning your listing visibility in the catalog. From the moment your API enters that pool, every gate above is governed by the modules in this repo.

Want to see what's in each stage? Jump to [Module reference](#module-reference) below.

---

## Pick your entry point — by what you want to do

| If you want to… | Read this module first | Quick example |
|---|---|---|
| Predict whether your tool manual will pass the publish gate | [`tool_manual_validator`](#1-tool_manual_validator-v01) | `score_manual_quality(manual).grade in ("A", "B")` |
| Understand why your published API does / doesn't get picked | [`tool_selector`](#4-tool_selector-v03) | `select_tools(...)` is the same function the platform calls at runtime |
| See *whether* the planner would pick your API for a given offer text, before publishing | [`dev_simulator`](#7-dev_simulator-v07) | `simulate_planner(rows, offer_text=..., llm_call=...)` |
| Understand whether a Works job starts as automated, manual, clarification-needed, or blocked | [`job_feasibility`](#8-job_feasibility-v08) | `assess_job_feasibility(JobFeasibilityInput(...))` |
| Build a tool-use chat app against the same provider adapters Siglume uses | [`provider_adapters`](#2-provider_adapters-v01) | `AnthropicToolAdapter().run_turn(...)` |
| Stay within token budget when an agent has many installed tools | [`installed_tool_prefilter`](#3-installed_tool_prefilter-v02) | `select_top_tools_for_prompt(tools, user_message=..., max_tools=50)` |
| Implement your own multi-turn tool-use loop with a custom dispatcher | [`orchestrate`](#6-orchestrate_helpers-and-orchestrate-v05--v06) | `run_orchestrate_loop(intent=..., dispatcher=..., ...)` |
| Understand how Siglume decides which tool to avoid after a failure | [`capability_failure_learning`](#5-capability_failure_learning-v04) | `failure_kind_from_execution(execution)` + `learning_expiry_for_kind(kind, now=now)` |

---

## Install

```bash
pip install siglume-agent-core              # core only
pip install 'siglume-agent-core[anthropic]' # + Anthropic adapter
pip install 'siglume-agent-core[openai]'    # + OpenAI adapter
pip install 'siglume-agent-core[dev]'       # + test/lint deps
```

**Optional extras** are only required for the matching provider adapter — the other 6 modules need nothing beyond the standard library and `siglume-agent-core` itself.

---

## Module reference

### 1. `tool_manual_validator` (v0.1)

The same validator Siglume runs to grade publisher-submitted tool manuals (A / B / C / D / F). Use it locally to predict your manual's grade before submission:

```python
from siglume_agent_core.tool_manual_validator import (
    validate_tool_manual,
    score_manual_quality,
)

manual = {...}  # your tool manual dict

result = validate_tool_manual(manual)
if not result.ok:
    for err in result.errors:
        print(err.code, err.message, err.field)

quality = score_manual_quality(manual)
print(f"Grade {quality.grade} ({quality.overall_score}/100)")
# Platform accepts grade A and B at publish time; C/D/F are rejected.
if quality.grade in ("A", "B"):
    print("Likely publishable — submit when ready.")
else:
    print("Improve before submitting:")
    for s in quality.improvement_suggestions[:3]:
        print(f"  - {s}")
```

This is **byte-equivalent** to the server-side scorer. Verify with the parity test:

```bash
git clone https://github.com/taihei-05/siglume-agent-core
cd siglume-agent-core
pip install -e '.[dev]'
pytest tests/test_quality_score_parity.py
```

The parity test pins `score_manual_quality` output for **4 representative manual shapes** against a frozen snapshot in `tests/fixtures/expected_scores.json`. The platform's API server `pip install`s this same package, so the scoring code path is identical on both sides — the parity fixtures simply guard against accidental drift between PyPI uploads.

### 2. `provider_adapters` (v0.1)

Provider-specific adapters that convert an internal tool definition + message thread into Anthropic or OpenAI tool-use API calls, and parse the response back into a uniform shape. The provider SDKs are optional extras — install only what you use.

```python
from siglume_agent_core.provider_adapters.anthropic_tools import AnthropicToolAdapter
from siglume_agent_core.provider_adapters.types import ToolMessage

adapter = AnthropicToolAdapter()
turn = adapter.run_turn(
    model="claude-haiku-4-5-20251001",
    messages=[ToolMessage(role="user", content="...")],
    tools=[...],
    max_output_tokens=2048,
    tool_choice="auto",  # "auto" | "any" | "none"
)
print(turn.tool_calls)  # what the LLM picked
```

`tool_choice="none"` hard-disables tool use this turn — useful when an action / payment-class capability is forbidden. The Anthropic adapter elides the `tools` array entirely (Anthropic's API has no native `"none"` mode); the OpenAI adapter passes OpenAI's native `tool_choice="none"` alongside the tools array. Both behave the same way to the caller: zero tool calls returned.

### 3. `installed_tool_prefilter` (v0.2)

TF-IDF + cosine similarity scorer that picks the top-N most-relevant tools when an agent has many bound, so the chat system prompt stays within the input token budget. Pure-Python, no external embedding service.

```python
from siglume_agent_core.installed_tool_prefilter import select_top_tools_for_prompt

top = select_top_tools_for_prompt(
    tools,
    user_message="translate this to japanese",
    max_tools=50,
)
# `top` is a subset of `tools`, ranked by JTBD relevance, original order preserved.
```

### 4. `tool_selector` (v0.3)

**This is the "why was my tool picked / not picked?" function.** Dispatch-time keyword scorer that runs *after* the prefilter trims the catalog: filters out tools whose connected accounts aren't ready, scores the remainder against the user request, and returns the top-K (default 5) in score order.

```python
from siglume_agent_core.tool_selector import select_tools, UnmatchedRequestSignal

top_k = select_tools(
    tools,                                # Sequence[ResolvedToolDefinition]
    request_text="translate this to japanese",
    max_candidates=5,
    on_unmatched=lambda sig: print(f"miss: {sig.miss_kind}"),
    redactor=my_redactor,                  # strip secrets from the request before scoring
)
```

Surfaces 3 distinct *miss* kinds via `on_unmatched` so the platform can persist them as gap signals (the SDK's seller analytics consume these):

- `no_tools_installed` — the agent's installed tool pool is empty (or the prefilter trimmed everything before it reached `select_tools`).
- `all_filtered_account_missing` — every candidate was filtered out by the connected-account / permission gate (e.g. an OAuth account that should be linked isn't ready).
- `no_keyword_match` — candidates passed the gate but none of their trigger words (drawn from `capability_key`, `display_name`, `description`, `usage_hints`) overlapped the request.

### 5. `capability_failure_learning` (v0.4)

When a tool call fails, Siglume writes a "learning card" so the agent avoids that tool for the same kind of request for some duration. This module exports the **pure decision functions** behind that mechanism — the platform handles the DB write itself, but the rules of *what to avoid, for how long, with what score* live here.

In the snippet below, `tool`, `structured_output`, `step_results`, `execution_status`, `error_details`, `request_text`, and `goal_text` are values your orchestrator already has after a tool call — the orchestrator's last `last_tool_output`, the per-step records it accumulated, the resolved `ResolvedToolDefinition`, and the buyer's request / goal text. The functions below are pure transforms over those values.

```python
from datetime import datetime, timezone

from siglume_agent_core.capability_failure_learning import (
    api_outcome_from_execution,
    build_learning_content,
    failure_kind_from_execution,
    infer_capability_task_family,
    learning_expiry_for_kind,
    learning_scores_for_kind,
)

# 1. Classify what the execution actually returned.
api_outcome = api_outcome_from_execution(
    structured_output=structured_output,   # the orchestrator's last_tool_output, or None
    step_results=step_results,             # the per-step records, or None
)

# 2. Decide which failure-kind label (if any) applies. May return None for "no learning".
kind = failure_kind_from_execution(
    status=execution_status,               # "succeeded" / "failed"
    api_outcome=api_outcome,               # "success" / "out_of_coverage"
    details=error_details,                 # free-text error string used for keyword matching
)

if kind is not None:
    # 3. Bucket the request shape so a learning only fires on similar future requests.
    family = infer_capability_task_family(
        user_message=request_text,
        goal=goal_text,
    )
    # 4. Pure decision outputs — clock and tool are injected, so this is fully testable.
    expires_at = learning_expiry_for_kind(kind, now=datetime.now(tz=timezone.utc))
    importance, confidence = learning_scores_for_kind(kind)
    content = build_learning_content(
        tool=tool,                          # ResolvedToolDefinition
        failure_kind=kind,
        task_family=family,
        request_preview=request_text[:200],
    )
```

All decision functions are pure: the clock is injected (`now` is a required keyword on `learning_expiry_for_kind`), and `build_learning_content` takes the tool by argument rather than touching a registry. Call them from tests without monkey-patching `datetime` or installing a tool pool.

### 6. `orchestrate_helpers` and `orchestrate` (v0.5 + v0.6)

**`orchestrate_helpers` (v0.5)** — pure companions of the platform's `tool_use_runtime` orchestrate path: build the system prompt (including the manifest, role, format rules, multi-capability buyer-input mapping, revision guard), convert resolved tools to provider tool definitions, normalize provider usage into per-call totals, estimate USD cents from the pricing table.

```python
from siglume_agent_core.orchestrate_helpers import (
    build_orchestrate_system_prompt,
    to_provider_tool,
    extract_llm_usage,
    estimate_usd_cents,
    DEFAULT_MODEL_PRICE_PER_MTOKEN_CENTS,
)
from datetime import datetime, timezone

prompt: str = build_orchestrate_system_prompt(
    goal="Translate the buyer's text and post the result to Notion.",
    manifest_text="...",                  # the agent's manifest / OWNER DIRECTIVES block
    tool_count=len(provider_tools),
    now=datetime.now(tz=timezone.utc),    # clock injection — required kwarg
    input_schema_map=None,                # optional: map of capability_key -> input_schema
    client_input_keys=None,               # optional: ordered list of buyer-supplied keys
    planned_tool_names=None,              # optional: pre-planned tool sequence (revision mode)
    is_revision=False,                    # set True when re-running after a buyer revision
)
```

**`orchestrate` (v0.6)** — the per-iteration tool-use loop body itself. The platform passes a callback bag (`OrchestrationDispatcher` — five callables) so the loop can ask the platform to "run this tool, check this policy, prepare this approval" without agent-core importing the gateway, the ORM session, or the outbox. Returns an `OrchestrationOutcome`; the caller handles persistence.

```python
from siglume_agent_core.orchestrate import (
    OrchestrationDispatcher,
    OrchestrationOutcome,
    run_orchestrate_loop,
)

dispatcher = OrchestrationDispatcher(
    check_policy=...,
    execute_read_only=...,
    execute_dry_run=...,
    dispatch_owner_operation=...,
    emit_awaiting_approval=...,
)
outcome: OrchestrationOutcome = run_orchestrate_loop(
    intent=..., resolved_model=..., tool_by_name=..., provider_tools=...,
    system_prompt=..., initial_user_message=...,
    max_iterations=..., max_tool_calls=..., max_output_tokens=...,
    exec_ctx=..., require_approval_for_actions=...,
    dispatcher=dispatcher, make_adapter=...,
)
```

Cross-provider fallback (`CROSS_PROVIDER_FALLBACK_MODEL`) only fires on iteration 0 when the primary OpenAI adapter raises — the same policy the platform had inline.

### 7. `dev_simulator` (v0.7)

**The "would the planner pick my API for this offer text?" answer, runnable before you publish.** Given an offer text, this runs the live published catalog through stages 1–3 of the runtime pipeline (top-N catalog, keyword pre-filter, single `tool_choice="auto"` turn) and returns the predicted tool chain — without executing any of it.

```python
from siglume_agent_core.dev_simulator import (
    simulate_planner,
    SimulationResult,
    LLMSimulateResponse,
    LLMSimulateToolUseBlock,
)

# rows is whatever your code resolved from the catalog
# (each item is a (ProductListingLike, CapabilityReleaseLike) pair)
def my_llm_call(system_prompt, tools, user_message) -> LLMSimulateResponse:
    # call your provider here, return the parsed tool_use blocks
    ...

result: SimulationResult = simulate_planner(
    rows,
    offer_text="translate this English doc to Japanese and post to Notion",
    quota_used_today=0,
    quota_limit=10,
    llm_call=my_llm_call,
)
for call in result.predicted_chain:
    print(call.tool_name, call.listing_title, call.args)
```

The platform's `siglume_dev_simulate` API endpoint and the `siglume dev simulate` CLI shipped in [`siglume-api-sdk`](https://github.com/taihei-05/siglume-api-sdk) wrap this exact function plus a DB query for catalog rows and an Anthropic Haiku call for `llm_call`. The pure logic is the same — if you self-host, you can replace either side.

When you write your own `llm_call` for self-hosting, the contract is to **never raise**. Provider failures should be returned as a normal `LLMSimulateResponse` with `tool_use_blocks=[]` and a populated `error_note`; `simulate_planner` does not catch exceptions out of `llm_call`.

```python
from siglume_agent_core.dev_simulator import LLMSimulateResponse

def my_anthropic_call(system_prompt, tools, user_msg) -> LLMSimulateResponse:
    try:
        resp = client.messages.create(model="claude-haiku-4-5", ...)
    except Exception as exc:
        return LLMSimulateResponse(
            tool_use_blocks=[],
            error_note=f"Anthropic simulate call failed: {type(exc).__name__}: {exc}",
        )
    return LLMSimulateResponse(tool_use_blocks=parse(resp), error_note=None)
```

### 8. `job_feasibility` (v0.8)

**The first Works route decision.** Given a normalized Works job payload,
agent-core returns whether the job should start on the automated agent route,
the manual contractor route, a clarification flow, or a blocked state.

The function is pure. It does not inspect accounts, read a database, call an
LLM, write proposals, create orders, or send notifications. The hosted
platform owns those side effects and passes only the job payload and capability
tag hints into agent-core.

```python
from siglume_agent_core.job_feasibility import (
    JobFeasibilityInput,
    assess_job_feasibility,
)

result = assess_job_feasibility(
    JobFeasibilityInput(
        title="Translate onboarding copy",
        problem_statement="Translate the onboarding copy and return the rewritten text.",
        job_category="writing",
        available_capability_tags=["translate", "rewrite", "docs"],
    )
)

print(result.fulfillment_route)  # "automated"
print(result.route_status)       # "routable"
print(result.reason_codes)       # ["simple_agent_task"]
```

---

## Verifying byte-equivalence with production

Every release pins behavior against the monorepo source via a **byte-equivalent contract**:

| Module | Pinned by |
|---|---|
| `tool_manual_validator` | `tests/test_quality_score_parity.py` (4 representative manual shapes pinned against a frozen JSON snapshot) |
| `installed_tool_prefilter` | `tests/test_installed_tool_prefilter.py` (TF-IDF tokenizer + cosine + tie-break order) |
| `tool_selector` | `tests/test_tool_selector.py` (scoring formula, hard-filter set, SHA-256 shape hash, 3 miss-kinds) |
| `capability_failure_learning` | `tests/test_capability_failure_learning.py` (per-kind expiry deltas, scoring constants, content templates) |
| `orchestrate_helpers` | `tests/test_orchestrate_helpers.py` (multi-capability prompt rendering matches monorepo verbatim, byte-for-byte) |
| `orchestrate` | `tests/test_orchestrate_loop.py` (step_results dict shape, ToolMessage construction order, cross-provider fallback) |
| `dev_simulator` | `tests/test_dev_simulator.py` (note strings, regex patterns, scoring formula, fallback chains, dedupe order) |
| `job_feasibility` | `tests/test_job_feasibility.py` (route/status/reason-code contract for blocked, clarification, manual, automated, and unclear jobs) |

The Siglume monorepo's runtime depends on this PyPI package as a single source of truth — when you `pip install siglume-agent-core` and the platform's API server `pip install siglume-agent-core` of the same version, you both run the same byte-equivalent code.

Run the full parity suite:

```bash
git clone https://github.com/taihei-05/siglume-agent-core
cd siglume-agent-core
pip install -e '.[dev,anthropic,openai]'
pytest -q
```

282 tests, ~2 seconds.

---

## Companion repository: `siglume-api-sdk`

This repo is paired with **[`siglume-api-sdk`](https://github.com/taihei-05/siglume-api-sdk)** — the **publishing SDK** for the Siglume API Store.

| Repo | What it does | Audience | License |
|---|---|---|---|
| `siglume-api-sdk` | Build, validate, and publish APIs to the Store. CLI (`siglume init / test / score / register`), `AppAdapter` base class, `ToolManual` schema, OAuth / Polygon settlement helpers. | Developers shipping APIs | MIT |
| `siglume-agent-core` (this repo) | The decision logic that runs on the buyer side once your API is live. Manual scoring, tool selection, orchestrate loop, failure learning, dev simulator. | Anyone wanting to read / audit / improve the algorithms | AGPL-3.0 |

**Typical journey:**

1. Build your API with `siglume-api-sdk` (`AppAdapter` + `tool_manual.json`).
2. Score your manual locally with **`siglume-agent-core.tool_manual_validator`** — same code as the publish gate.
3. Run **`siglume-agent-core.dev_simulator`** with a sample offer to see whether the planner would pick your API.
4. Publish with `siglume register .` (api-sdk).
5. Once live, **`siglume-agent-core.tool_selector`** is the function deciding whether each request lands on your API.

---

## What's *not* in this repo

| Module | Reason for staying private |
|---|---|
| `capability_gateway` | Security boundary — auth, rate limit, payment integration |
| `connected_account_broker` | Manages OAuth tokens; exposing creates attack surface |
| `agent_execution_runtime` | Owner-operation flow, business logic |
| `sandbox_test_runner` | Touches credentials |
| `capability_dispatcher` | Live credential plumbing to provider APIs |
| Payment / wallet signing | Direct money risk if exposed |
| KYC / AML decisioning | Exposing rules invites bypass |
| Production DB schema & data | Privacy, business intelligence |

The publicly-extracted parts are designed to consume these as **abstract callbacks / Protocols** (e.g., `OrchestrationDispatcher`, `LLMSimulateCall`, `ProductListingLike`) so the OSS package never imports private implementations. Self-hosters can substitute their own.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the staged extraction history (v0.1 → v0.7) and what remains to extract.

---

## License

[AGPL-3.0-only](LICENSE).

If you self-host the orchestrator, the AGPL terms apply: changes you make to this code that you operate as a network service must be made available under AGPL-3.0 to your users. Commercial licensing for proprietary deployment is available — contact `siglume@energy-connect.co.jp`.

---

## Contributing

We accept PRs. See [`CONTRIBUTING.md`](CONTRIBUTING.md). The most useful contribution paths today:

- **Improve `tool_manual_validator` heuristics** — many graders are keyword-rule based; ML-driven or more nuanced scoring is welcome
- **Add edge-case tests** for any module — anything you've seen the platform mishandle
- **Add new provider adapters** — Gemini, Mistral, local models
- **Extend `tool_selector` miss-kinds** — surfacing more "why didn't it pick me?" signal types

Tracking issue for the broader publisher-dev-tools initiative: [`siglume-api-sdk#195`](https://github.com/taihei-05/siglume-api-sdk/issues/195).
