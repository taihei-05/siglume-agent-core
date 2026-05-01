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

## What's in this release (Phase 1, Tier A)

### `orchestrator_core.tool_manual_validator`

The exact same validator Siglume runs to grade publisher-submitted tool manuals (A / B / C / D / F). Use it locally to predict your manual's grade before submission:

```python
from orchestrator_core.tool_manual_validator import validate_tool_manual, score_manual_quality

manual = {...}  # your tool manual dict

result = validate_tool_manual(manual)
if not result.ok:
    for err in result.errors:
        print(err.code, err.message, err.field)

quality = score_manual_quality(manual)
print(f"Grade {quality.grade} ({quality.overall_score}/100)")
print(f"Publishable: {quality.publishable}")
```

This is **byte-equivalent** to the server-side scorer — verified by CI parity test against the Siglume monorepo. If your local grade is B, the server grade is B.

### `orchestrator_core.provider_adapters`

Provider-specific adapters that convert an internal tool definition + message thread into the format Anthropic's or OpenAI's tool-use API expects, and parse the response back into a uniform shape.

```python
from orchestrator_core.provider_adapters.anthropic_tools import AnthropicToolAdapter
from orchestrator_core.provider_adapters.types import ToolMessage

adapter = AnthropicToolAdapter()
turn = adapter.run_turn(
    model="claude-haiku-4-5-20251001",
    messages=[ToolMessage(role="user", content="...")],
    tools=[...],
    max_output_tokens=2048,
    tool_choice="auto",
)
print(turn.tool_calls)  # what the LLM picked
```

Use the **same adapter** the platform uses, so you can prototype tool-use applications against either provider with consistent behavior.

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
