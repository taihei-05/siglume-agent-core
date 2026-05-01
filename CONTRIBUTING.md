# Contributing to siglume-agent-core

Thank you for considering a contribution! This repository is the open core of the Siglume marketplace agent runtime — improvements here directly affect how publishers' tools get scored and selected in production.

## Quick start

```bash
git clone https://github.com/taihei-05/siglume-agent-core.git
cd siglume-agent-core
python -m venv .venv && source .venv/bin/activate  # or `.venv\Scripts\activate` on Windows
pip install -e ".[dev,anthropic,openai]"
pytest -q
```

## What kinds of PRs we accept

**Most welcome:**

- **Bug fixes** in `tool_manual_validator` heuristics with a regression test that captures the bug
- **New edge-case tests** for any module — especially around real-world tool manuals that grade unexpectedly
- **New provider adapters** — Gemini, Mistral, Cohere, local models (vLLM / llama.cpp). Match the existing `AnthropicToolAdapter` / `OpenAIToolAdapter` shape
- **Performance** improvements with benchmark evidence
- **Documentation** improvements, especially worked examples

**Welcome but please discuss in an issue first:**

- Public API additions / signature changes (we're pre-1.0 but still careful)
- New scoring criteria (changes to grade math affect every publisher's grade)
- Refactors that span multiple modules

**Likely declined without a strong rationale:**

- Removing or weakening privacy / safety filters
- Changes that bypass the AGPL — e.g., dropping copyleft headers, license-laundering moves
- Bringing in heavy new runtime dependencies for marginal value
- Anything that imports private platform code (this package must remain pure)

## Code style

- `ruff check .` and `ruff format .` should pass before commit
- Public API surfaces use type hints
- Comments should explain *why*, not *what* — well-named code already says what

## Tests

- Every PR with a behavior change needs a test
- Tests should run without network — adapters ship with mock harnesses where needed
- `pytest -q` should be green; if you skip a test, justify in the test docstring

## License grant

By submitting a PR, you agree your contribution is licensed under AGPL-3.0-only (see [LICENSE](LICENSE)). For corporate contributors who need a CLA, open an issue and we'll set one up.

## What flows back to the platform?

The Siglume monorepo imports this package as a dependency. Every release of `siglume-agent-core` to PyPI propagates to the platform on the next deploy cycle. So a merged PR here ships in production, typically within a week.

## Tracking issue

The broader publisher-dev-tools initiative is tracked at [`siglume-api-sdk#195`](https://github.com/taihei-05/siglume-api-sdk/issues/195). For context on why this repo exists, that issue plus [`ARCHITECTURE.md`](ARCHITECTURE.md) are the canonical sources.
