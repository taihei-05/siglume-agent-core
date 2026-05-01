# Security Policy

## Scope

`siglume-agent-core` is the open-core orchestrator logic for the
[Siglume API Store](https://siglume.com) — agents, tool selection,
LLM provider adapters, tool-manual quality scoring. This repository
**does not** contain authentication, OAuth credential leasing, payment
or wallet handling, or per-buyer KYC; those live in the private
platform monorepo (see `README.md` "What's not in this repo").

Vulnerabilities in this package nevertheless do warrant private
disclosure when they:

- enable an attacker to bypass `tool_choice="none"` and trigger
  unintended tool calls in the platform's runtime,
- enable a publisher to exfiltrate or manipulate buyer-side prompt
  context via `tool_manual` metadata (prompt injection through
  validator-accepted manuals),
- cause incorrect output from `score_manual_quality` such that a
  manual that should be rejected at publish time is accepted, or
  vice-versa, in a way that a malicious publisher could exploit,
- introduce dependency-resolution or supply-chain risks that would
  affect downstream installs of `siglume-agent-core` from PyPI.

## Reporting

**Please do NOT open a public GitHub issue for security reports.**

Email `siglume@energy-connect.co.jp` with subject
`[siglume-agent-core security] <one-line summary>` and include:

- A description of the vulnerability and the failure mode it produces.
- A reproduction (input that triggers it, expected vs actual behaviour).
- Any constraints on exploitability (auth required, network position,
  user interaction, etc.).
- The package version (`pip show siglume-agent-core`) and the affected
  module/function path.

We will acknowledge receipt within **3 business days** and aim to
provide a remediation plan or initial fix within **14 calendar days**
for confirmed issues.

PGP-encrypted reports are accepted but not required; if you'd like to
encrypt, request our public key in your initial email and we will
respond before sharing details.

## What we'll do

- Treat the report as private until a fix is shipped to PyPI and the
  Siglume platform is patched.
- Credit the reporter in the release notes (if they want credit).
- Coordinate disclosure timing with the reporter when the impact
  exceeds this repository — for example, when the same class of bug
  affects the closed-source Siglume runtime.

We do not currently run a paid bug-bounty program. We do publicly
acknowledge meaningful reports in `CHANGELOG.md`.

## Out of scope

- Issues affecting only the closed-source Siglume runtime — please
  report those to the same email but mention the scope.
- Best-practice / defense-in-depth feedback that doesn't describe a
  concrete exploit path: open a regular GitHub issue or PR.
- Vulnerabilities in dependencies (`anthropic`, `openai`) themselves
  — report those upstream and we'll bump our pins once the fix
  releases.
