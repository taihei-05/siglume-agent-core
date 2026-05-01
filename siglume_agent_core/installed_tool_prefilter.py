"""Pre-filter installed tools to top-N before injection into the system prompt.

When an agent has many bound tools, rendering every tool into the system
prompt blows past the chat input token budget. This module's job is to
choose the top-N tools most likely to be relevant to the current user
turn so the prompt stays within budget while the LLM still has a useful
catalog to choose from.

Algorithm (v1):
    Smoothed TF-IDF + cosine similarity over the union of (user message)
    and (per-tool JTBD text). JTBD text is the concatenation of the
    tool's compact_prompt + description + capability_key + display_name
    + usage_hints + result_hints. Tokenization handles Latin words and
    CJK character bigrams in the same pass so JA / EN / mixed messages
    all score sensibly.

    Pure-Python — no scikit-learn dependency. Deterministic. O(N * |vocab|)
    per request, which is fine for N <= a few hundred tools.

Out of scope for v1:
    External embedding service, learned re-ranking, recency weighting.
    Those become useful once telemetry on which tools the LLM actually
    picks under the new pre-filter is available.
"""
from __future__ import annotations

import math
import re
from typing import Iterable, Sequence

from .types import ResolvedToolDefinition

# Default cap on tools rendered into the system prompt's tool catalog.
# Calibrated empirically: with the platform's 32K input budget and per-tool
# clipping, 50 entries fit comfortably (50 lines * ~150 tokens = ~7.5K
# tokens for the catalog block). Callers can override per-request.
DEFAULT_MAX_TOOLS = 50

# Latin words: 2+ word characters in a row, ASCII or Unicode letter.
_LATIN_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}", re.UNICODE)
# CJK code-point ranges: Hiragana, Katakana, CJK Unified Ideographs (the
# blocks Siglume actually sees in JA prompts). Tokens become character
# bigrams from contiguous runs.
_CJK_RE = re.compile(r"[぀-ゟ゠-ヿ一-鿿]+")


def _tokenize(text: str) -> list[str]:
    """Lowercased Latin words + CJK character bigrams.

    Returns an empty list for empty / None / non-string input. The output
    is a multiset (duplicates preserved) — the caller computes term
    frequency from it.
    """
    if not text or not isinstance(text, str):
        return []
    lowered = text.lower()
    tokens: list[str] = [m.group(0) for m in _LATIN_TOKEN_RE.finditer(lowered)]
    for run_match in _CJK_RE.finditer(lowered):
        run = run_match.group(0)
        if len(run) == 1:
            tokens.append(run)
            continue
        for i in range(len(run) - 1):
            tokens.append(run[i:i + 2])
    return tokens


def _tool_jtbd_text(tool: ResolvedToolDefinition) -> str:
    """Concatenate every field that describes what a tool is for.

    Order matters only insofar as duplication boosts a term's TF — which
    is the desired behavior (a term repeated across compact_prompt and
    description is more salient than one mentioned only once).
    """
    parts: list[str] = []
    for field_value in (
        tool.compact_prompt,
        tool.description,
        tool.capability_key,
        tool.display_name,
    ):
        if field_value:
            parts.append(str(field_value))
    for hint_list in (tool.usage_hints, tool.result_hints):
        if hint_list:
            parts.extend(str(h) for h in hint_list if h)
    return " ".join(parts)


def _term_frequency(tokens: Sequence[str]) -> dict[str, float]:
    """Raw count -> length-normalized TF map. Empty input -> empty map."""
    if not tokens:
        return {}
    counts: dict[str, int] = {}
    for token in tokens:
        counts[token] = counts.get(token, 0) + 1
    total = float(len(tokens))
    return {token: count / total for token, count in counts.items()}


def _smoothed_idf(doc_token_sets: Iterable[set[str]], n_docs: int) -> dict[str, float]:
    """Smoothed IDF = log((N+1) / (df+1)) + 1 (sklearn-style)."""
    df: dict[str, int] = {}
    for token_set in doc_token_sets:
        for token in token_set:
            df[token] = df.get(token, 0) + 1
    return {
        token: math.log((n_docs + 1) / (count + 1)) + 1.0
        for token, count in df.items()
    }


def _tfidf_vector(
    tf: dict[str, float],
    idf: dict[str, float],
) -> dict[str, float]:
    return {token: weight * idf.get(token, 0.0) for token, weight in tf.items()}


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if not a or not b:
        return 0.0
    # Iterate over the shorter dict for the dot product.
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    for token, weight in a.items():
        other = b.get(token)
        if other:
            dot += weight * other
    if dot == 0.0:
        return 0.0
    norm_a = math.sqrt(sum(w * w for w in a.values()))
    norm_b = math.sqrt(sum(w * w for w in b.values()))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def select_top_tools_for_prompt(
    installed_tools: Sequence[ResolvedToolDefinition],
    user_message: str | None,
    *,
    max_tools: int = DEFAULT_MAX_TOOLS,
) -> list[ResolvedToolDefinition]:
    """Return at most ``max_tools`` tools, ranked by JTBD relevance.

    Behavior contract:
      * If the input is already <= ``max_tools``, return a list copy with
        the original order preserved (no scoring needed; we never want to
        re-order tools below the cap because original order encodes
        binding creation history that the LLM may rely on weakly).
      * If the user_message is empty or scores zero against every tool,
        we fall back to the original-order prefix of length ``max_tools``.
        Trying to "guess" relevance with no signal would just produce
        non-determinism.
      * Within the selected top-N, the *original* order is restored before
        return so the catalog rendering downstream stays stable across
        scoring noise (only membership changes turn-to-turn, not order).
    """
    if max_tools <= 0:
        return []
    tools = list(installed_tools or [])
    if len(tools) <= max_tools:
        return tools

    user_tokens = _tokenize(user_message or "")
    if not user_tokens:
        return tools[:max_tools]

    tool_tokens = [_tokenize(_tool_jtbd_text(tool)) for tool in tools]
    # Include the user query as one of the documents for IDF estimation
    # so a query-only term still gets a reasonable weight.
    doc_sets = [set(tt) for tt in tool_tokens] + [set(user_tokens)]
    idf = _smoothed_idf(doc_sets, n_docs=len(doc_sets))

    user_vec = _tfidf_vector(_term_frequency(user_tokens), idf)
    scored: list[tuple[float, int]] = []
    for idx, tt in enumerate(tool_tokens):
        if not tt:
            scored.append((0.0, idx))
            continue
        tool_vec = _tfidf_vector(_term_frequency(tt), idf)
        scored.append((_cosine(user_vec, tool_vec), idx))

    if all(score == 0.0 for score, _ in scored):
        return tools[:max_tools]

    # Highest score first; ties broken by original index (stable, lower
    # index first). Take top max_tools, then restore original order.
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    selected_indices = sorted(idx for _, idx in scored[:max_tools])
    return [tools[i] for i in selected_indices]


__all__ = [
    "DEFAULT_MAX_TOOLS",
    "select_top_tools_for_prompt",
]
