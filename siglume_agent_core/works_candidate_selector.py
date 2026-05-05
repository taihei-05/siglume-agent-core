"""Pure Works candidate selection helpers.

The hosted platform owns database rows, proposal creation, notifications,
payments, and LLM calls. This module exposes only the deterministic policy used
before those side effects: fingerprinting, whether an existing match decision
can be reused, and how agent candidates are ranked for a Works job.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

WorksMatchStatus = Literal["declined", "skipped", "pitch_failed", "pitched", "error", "matched"]

TERMINAL_REUSE_STATUSES: frozenset[WorksMatchStatus] = frozenset(
    {"declined", "skipped", "pitch_failed", "pitched", "error"}
)


@dataclass(frozen=True)
class WorksJobFingerprintInput:
    need_id: str
    title: str
    problem_statement: str
    category_key: str | None = None
    title_en: str | None = None
    title_ja: str | None = None
    problem_statement_en: str | None = None
    problem_statement_ja: str | None = None
    budget_min_minor: int | None = None
    budget_max_minor: int | None = None
    requirement: dict[str, Any] = field(default_factory=dict)
    job_category: str | None = None
    required_capabilities: list[str] = field(default_factory=list)
    deliverable_spec: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    capability_snapshot: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorksAgentFingerprintInput:
    agent_id: str
    capabilities: list[str] = field(default_factory=list)
    description: str = ""
    reputation: dict[str, Any] = field(default_factory=dict)
    read_only_release_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorksCandidateInput:
    agent_id: str
    display_name: str = ""
    capability_keys: list[str] = field(default_factory=list)
    normalized_capability_keys: list[str] = field(default_factory=list)
    inferred_release_ids: list[str] = field(default_factory=list)
    completed_count: int = 0
    average_rating: float = 0.0
    fingerprint: str = ""
    existing_match_status: WorksMatchStatus | None = None
    existing_match_fingerprint: str | None = None
    existing_next_recheck_at: dt.datetime | None = None


@dataclass(frozen=True)
class WorksCandidateSelection:
    agent_id: str
    score: float
    rank: int
    fingerprint: str
    inferred_release_ids: list[str]
    overlap: float
    reasons: list[str] = field(default_factory=list)


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def make_works_match_fingerprint(
    agent: WorksAgentFingerprintInput,
    job: WorksJobFingerprintInput,
) -> str:
    """Return a stable hash for a single agent/job match input.

    The hash intentionally excludes platform-only state such as proposal ids,
    order ids, credentials, API keys, scheduler timestamps, and logs.
    """

    return _json_hash(
        {
            "agent_id": agent.agent_id,
            "agent_caps": sorted(str(c) for c in agent.capabilities),
            "agent_description": agent.description or "",
            "agent_reputation": agent.reputation or {},
            "job": {
                "need_id": job.need_id,
                "title": job.title,
                "title_en": job.title_en,
                "title_ja": job.title_ja,
                "problem_statement": job.problem_statement,
                "problem_statement_en": job.problem_statement_en,
                "problem_statement_ja": job.problem_statement_ja,
                "category_key": job.category_key,
                "budget_min_minor": job.budget_min_minor,
                "budget_max_minor": job.budget_max_minor,
                "requirement_jsonb": job.requirement or {},
            },
            "extension": {
                "job_category": job.job_category,
                "required_capabilities_jsonb": job.required_capabilities or [],
                "deliverable_spec_jsonb": job.deliverable_spec or {},
                "tags_jsonb": job.tags or [],
                "capability_snapshot_jsonb": job.capability_snapshot or {},
            },
            "read_only_release_ids": sorted(str(rid) for rid in agent.read_only_release_ids),
        }
    )


def should_reuse_works_match(
    *,
    existing_status: WorksMatchStatus | None,
    existing_fingerprint: str | None,
    current_fingerprint: str,
    next_recheck_at: dt.datetime | None,
    now: dt.datetime,
) -> bool:
    """Return true when a previous terminal decision should suppress re-checking."""

    if existing_status not in TERMINAL_REUSE_STATUSES:
        return False
    if existing_fingerprint != current_fingerprint:
        return False
    return next_recheck_at is None or next_recheck_at > now


def should_reuse_matched_works_match(
    *,
    existing_status: WorksMatchStatus | None,
    existing_fingerprint: str | None,
    current_fingerprint: str,
    next_recheck_at: dt.datetime | None,
    now: dt.datetime,
) -> bool:
    """Return true when a prior positive match can skip the fit check."""

    if existing_status != "matched":
        return False
    if existing_fingerprint != current_fingerprint:
        return False
    return next_recheck_at is None or next_recheck_at > now


def rank_works_agent_candidates(
    candidates: list[WorksCandidateInput],
    *,
    required_normalized_capability_keys: set[str] | list[str] | tuple[str, ...],
    has_category_tags: bool,
    max_candidates: int = 5,
    minimum_overlap_without_release: float = 0.2,
    now: dt.datetime,
) -> list[WorksCandidateSelection]:
    """Rank agent candidates for one Works job.

    Agents with a reusable terminal match are omitted. If the job has category
    tags and no deterministic release match, candidates below the minimum
    overlap are omitted before LLM fit checks can be considered.
    """

    required = {str(c) for c in required_normalized_capability_keys if c}
    rows: list[tuple[float, str, WorksCandidateInput, float, list[str]]] = []
    for candidate in candidates:
        if not candidate.fingerprint:
            continue
        if should_reuse_works_match(
            existing_status=candidate.existing_match_status,
            existing_fingerprint=candidate.existing_match_fingerprint,
            current_fingerprint=candidate.fingerprint,
            next_recheck_at=candidate.existing_next_recheck_at,
            now=now,
        ):
            continue

        inferred_release_ids = [str(rid) for rid in candidate.inferred_release_ids if rid]
        overlap = 0.0
        normalized_caps = {str(c) for c in candidate.normalized_capability_keys if c}
        if required:
            overlap = len(normalized_caps & required) / max(1, len(required))
        if has_category_tags and not inferred_release_ids and overlap < minimum_overlap_without_release:
            continue

        completed_bonus = min(0.5, max(0.0, float(candidate.completed_count)) * 0.05)
        rating_bonus = min(0.3, max(0.0, float(candidate.average_rating) - 3.0) * 0.1)
        score = (2.0 if inferred_release_ids else 0.0) + overlap + completed_bonus + rating_bonus
        reasons: list[str] = []
        if inferred_release_ids:
            reasons.append("deterministic_release_match")
        if overlap > 0:
            reasons.append("category_capability_overlap")
        if completed_bonus:
            reasons.append("works_completion_history")
        if rating_bonus:
            reasons.append("works_rating_history")
        rows.append((score, candidate.display_name or "", candidate, overlap, reasons))

    rows.sort(key=lambda row: (-row[0], row[1]))
    return [
        WorksCandidateSelection(
            agent_id=row[2].agent_id,
            score=row[0],
            rank=index,
            fingerprint=row[2].fingerprint,
            inferred_release_ids=list(row[2].inferred_release_ids),
            overlap=row[3],
            reasons=row[4],
        )
        for index, row in enumerate(rows[:max_candidates], start=1)
    ]


def candidate_selection_to_dict(selection: WorksCandidateSelection) -> dict[str, Any]:
    return asdict(selection)


__all__ = [
    "TERMINAL_REUSE_STATUSES",
    "WorksAgentFingerprintInput",
    "WorksCandidateInput",
    "WorksCandidateSelection",
    "WorksJobFingerprintInput",
    "WorksMatchStatus",
    "candidate_selection_to_dict",
    "make_works_match_fingerprint",
    "rank_works_agent_candidates",
    "should_reuse_matched_works_match",
    "should_reuse_works_match",
]
