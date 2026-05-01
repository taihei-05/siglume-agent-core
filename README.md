# siglume-agent-core

Open-core orchestrator logic for the [Siglume API Store](https://siglume.com) agent runtime.

This is the **public, AGPL-licensed core** of the algorithms the Siglume marketplace uses to:

- **Score the quality** of a publisher's tool manual (`tool_manual_validator`)
- **Build LLM provider tool definitions** in Anthropic / OpenAI tool-use format (`provider_adapters`)

It is the same code that runs in production — extracted from the private monorepo so publishers, contributors, and self-hosters can read, audit, and improve it.

> **Status:** Phase 1 of a staged extraction. Currently exposes Tier A modules (manual quality scoring + provider adapters). Selection scoring (`installed_tool_resolver`), simulator (`dev_simulator`), and analytics derivations (`seller_analytics`) follow in subsequent releases. See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the roadmap.

---

## Why this exists

The Siglume marketplace agent had been a black box from the publisher side. When a publisher asked "why didn't my API get picked?" or "why is my manual graded B?", the only way to answer was through platform-side reports.

This repository is the **direct answer**: read the source, run the same scorer locally, contribute improvements as PRs.

The platform itself remains a hosted service (publishers, buyers, payments, identity, deployment infrastructure all stay private). Only the **decision logic** — how the agent picks tools, how manuals are scored, how provider tool calls are formatted — is open.

## Install

```bash
pip install siglume-agent-core           # core only
pip install siglume-agent-core[anthropic] # + Anthropic adapter
pip install siglume-agent-core[openai]    # + OpenAI adapter
pip install siglume-agent-core[dev]       # + test/lint deps
```

## What's in this release (v0.2, Tier A + Tier B Phase 1)

### `siglume_agent_core.tool_manual_validator`

The exact same validator Siglume runs to grade publisher-submitted tool manuals (A / B / C / D / F). Use it locally to predict your manual's grade before submission:

```python
from siglume_agent_core.tool_manual_validator import validate_tool_manual, score_manual_quality

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

This is **byte-equivalent** to the server-side scorer. The Siglume monorepo's runtime depends on this PyPI package, so the same code path runs in production. You can verify the claim yourself:

```bash
pip install siglume-agent-core
git clone https://github.com/taihei-05/siglume-agent-core
cd siglume-agent-core
pytest tests/test_quality_score_parity.py
```

The parity test pins `score_manual_quality` output for four representative manuals against a frozen snapshot in `tests/fixtures/expected_scores.json`. If your local grade is B, the server grade is B.

### `siglume_agent_core.installed_tool_prefilter`

TF-IDF + cosine similarity scorer that picks the top-N most-relevant tools when an agent has many bound, so the chat system prompt stays within the input token budget. Pure-Python, no embedding service. Same code the platform runs in production:

```python
from siglume_agent_core.installed_tool_prefilter import select_top_tools_for_prompt
from siglume_agent_core.types import ResolvedToolDefinition

# tools is whatever your code resolved from a binding registry.
top = select_top_tools_for_prompt(tools, user_message="translate this to japanese", max_tools=50)
# `top` is a subset of `tools`, ranked by JTBD relevance, original order preserved.
```

### `siglume_agent_core.provider_adapters`

Provider-specific adapters that convert an internal tool definition + message thread into the format Anthropic's or OpenAI's tool-use API expects, and parse the response back into a uniform shape.

The provider SDKs are **optional extras** — install only the ones you use:

```bash
pip install siglume-agent-core[anthropic]   # + Anthropic SDK
pip install siglume-agent-core[openai]      # + OpenAI SDK
```

Without the matching extra, importing the adapter raises a clear `ImportError` telling you which extra to install. Then:

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

`tool_choice="none"` means **no tool use this turn** — the adapter elides the `tools` array entirely, matching the contract you'd expect from OpenAI. Use the same adapter the platform uses, so you can prototype tool-use applications against either provider with consistent behavior.

## What's **not** in this repo

The following stays in the private platform monorepo because exposing them creates security or business risk:

- Authentication / OAuth credential leasing (`connected_account_broker`)
- Payment processing & wallet signing
- Production database schema & data
- Per-buyer KYC / AML decisioning
- Marketplace pricing & fee logic
- The execution gateway (`capability_gateway`) — security/policy boundary

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for what's planned to come next vs. what stays private.

## License

[AGPL-3.0-only](LICENSE).

If you self-host the orchestrator, the AGPL terms apply: changes you make to this code that you operate as a network service must be made available under AGPL-3.0 to your users. Commercial licensing for proprietary deployment is available — contact `siglume@energy-connect.co.jp`.

## Contributing

We accept PRs. See [`CONTRIBUTING.md`](CONTRIBUTING.md). The most useful contribution paths today:

- **Improve `tool_manual_validator` heuristics** — many graders are currently keyword-rule based; ML-driven or more nuanced scoring is welcome
- **Add edge-case tests** — anything you've seen the platform mishandle
- **Add new provider adapters** — Gemini, Mistral, local models

Tracking issue for the broader publisher-dev-tools initiative: [`siglume-api-sdk#195`](https://github.com/taihei-05/siglume-api-sdk/issues/195).
