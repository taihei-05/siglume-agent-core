"""Pure Works job feasibility routing.

This module decides whether a Works job is suitable for automated agent
fulfillment, should be routed to a manual contractor, needs clarification, or
must be blocked. It is deliberately pure: no database access, no HTTP calls, no
credential inspection, and no writes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

FulfillmentRoute = Literal["automated", "manual"]
RouteStatus = Literal["routable", "needs_clarification", "blocked"]
Confidence = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class JobFeasibilityInput:
    title: str
    problem_statement: str
    job_category: str | None = None
    budget_max_minor: int = 0
    deliverable_spec: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    available_capability_tags: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class JobFeasibilityResult:
    fulfillment_route: FulfillmentRoute | None
    route_status: RouteStatus
    confidence: Confidence
    reason_codes: list[str] = field(default_factory=list)
    buyer_questions: list[str] = field(default_factory=list)
    automated_hints: dict[str, Any] = field(default_factory=dict)
    manual_hints: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


POLICY_BLOCK_MARKERS = frozenset(
    {
        "illegal",
        "stolen credential",
        "steal password",
        "credential theft",
        "phishing",
        "malware",
    }
)

MANUAL_ROUTE_MARKERS = frozenset(
    {
        "accounting",
        "bookkeeping",
        "tax",
        "legal",
        "contract review",
        "phone call",
        "onsite",
        "multi-connector",
        "manual review",
        "human judgment",
        "regulated",
        "financial advice",
        "medical",
    }
)

AUTOMATED_ROUTE_MARKERS = frozenset(
    {
        "summary",
        "summarize",
        "draft",
        "translate",
        "rewrite",
        "extract",
        "classify",
        "format",
        "outline",
    }
)


def _normalized_job_text(data: JobFeasibilityInput) -> str:
    parts = [
        data.title or "",
        data.problem_statement or "",
        data.job_category or "",
        " ".join(data.tags or []),
    ]
    deliverable = data.deliverable_spec or {}
    for key in ("title", "description", "acceptance_criteria", "outputs", "requirements"):
        value = deliverable.get(key)
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(item) for item in value)
    return " ".join(parts).lower()


def assess_job_feasibility(data: JobFeasibilityInput) -> JobFeasibilityResult:
    """Return the initial route decision for a Works job.

    The caller owns persistence, notifications, proposal creation, and any
    deeper agent/capability matching. This function only returns the first
    pure routing decision from a normalized job payload.
    """

    text = _normalized_job_text(data)

    if any(marker in text for marker in POLICY_BLOCK_MARKERS):
        return JobFeasibilityResult(
            fulfillment_route=None,
            route_status="blocked",
            confidence="high",
            reason_codes=["policy_or_safety_block"],
        )

    if len((data.problem_statement or "").strip()) < 20:
        return JobFeasibilityResult(
            fulfillment_route=None,
            route_status="needs_clarification",
            confidence="high",
            reason_codes=["brief_too_short"],
            buyer_questions=["What should be delivered, and what counts as complete?"],
        )

    if any(marker in text for marker in MANUAL_ROUTE_MARKERS):
        return JobFeasibilityResult(
            fulfillment_route="manual",
            route_status="routable",
            confidence="medium",
            reason_codes=["human_contractor_responsibility_preferred"],
            manual_hints={"reason": "job appears to require human judgment or offline coordination"},
        )

    if any(marker in text for marker in AUTOMATED_ROUTE_MARKERS):
        return JobFeasibilityResult(
            fulfillment_route="automated",
            route_status="routable",
            confidence="medium",
            reason_codes=["simple_agent_task"],
            automated_hints={"capability_tags": data.available_capability_tags[:20]},
        )

    return JobFeasibilityResult(
        fulfillment_route="manual",
        route_status="routable",
        confidence="low",
        reason_codes=["default_manual_when_automation_fit_unclear"],
        manual_hints={"reason": "automation fit is unclear without deeper capability matching"},
    )


__all__ = [
    "AUTOMATED_ROUTE_MARKERS",
    "Confidence",
    "FulfillmentRoute",
    "JobFeasibilityInput",
    "JobFeasibilityResult",
    "MANUAL_ROUTE_MARKERS",
    "POLICY_BLOCK_MARKERS",
    "RouteStatus",
    "assess_job_feasibility",
]
