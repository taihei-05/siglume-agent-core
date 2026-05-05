from __future__ import annotations

from siglume_agent_core.job_feasibility import (
    JobFeasibilityInput,
    JobFeasibilityResult,
    assess_job_feasibility,
)


def _input(**overrides):
    data = {
        "title": "Prepare a useful deliverable",
        "problem_statement": "Create the requested output with clear acceptance criteria.",
        "job_category": "general",
        "budget_max_minor": 10_000,
        "deliverable_spec": {},
        "tags": [],
        "available_capability_tags": ["writing", "translation"],
    }
    data.update(overrides)
    return JobFeasibilityInput(**data)


def test_blocks_policy_or_safety_markers():
    result = assess_job_feasibility(
        _input(problem_statement="Help me steal password credentials from another account.")
    )

    assert result == JobFeasibilityResult(
        fulfillment_route=None,
        route_status="blocked",
        confidence="high",
        reason_codes=["policy_or_safety_block"],
    )
    assert result.to_dict()["route_status"] == "blocked"


def test_short_brief_needs_clarification():
    result = assess_job_feasibility(_input(problem_statement="Do this."))

    assert result.fulfillment_route is None
    assert result.route_status == "needs_clarification"
    assert result.confidence == "high"
    assert result.reason_codes == ["brief_too_short"]
    assert result.buyer_questions


def test_manual_route_for_human_judgment_work():
    result = assess_job_feasibility(
        _input(
            title="Review a legal contract",
            problem_statement="Review the contract, summarize risks, and make judgment calls.",
            tags=["legal", "manual review"],
        )
    )

    assert result.fulfillment_route == "manual"
    assert result.route_status == "routable"
    assert result.confidence == "medium"
    assert result.reason_codes == ["human_contractor_responsibility_preferred"]
    assert result.manual_hints["reason"]


def test_automated_route_for_simple_agent_task():
    result = assess_job_feasibility(
        _input(
            title="Translate onboarding copy",
            problem_statement="Translate the onboarding copy and return the rewritten text.",
            tags=["translate"],
            available_capability_tags=["translate", "rewrite", "docs"],
        )
    )

    assert result.fulfillment_route == "automated"
    assert result.route_status == "routable"
    assert result.confidence == "medium"
    assert result.reason_codes == ["simple_agent_task"]
    assert result.automated_hints == {"capability_tags": ["translate", "rewrite", "docs"]}


def test_unclear_automation_defaults_to_manual():
    result = assess_job_feasibility(
        _input(
            title="Launch support project",
            problem_statement="Coordinate the launch work and decide the right next actions.",
            tags=["operations"],
        )
    )

    assert result.fulfillment_route == "manual"
    assert result.route_status == "routable"
    assert result.confidence == "low"
    assert result.reason_codes == ["default_manual_when_automation_fit_unclear"]
