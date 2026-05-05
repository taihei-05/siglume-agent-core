from __future__ import annotations

import datetime as dt

from siglume_agent_core.works_candidate_selector import (
    WorksAgentFingerprintInput,
    WorksCandidateInput,
    WorksJobFingerprintInput,
    make_works_match_fingerprint,
    rank_works_agent_candidates,
    should_reuse_matched_works_match,
    should_reuse_works_match,
)

NOW = dt.datetime(2026, 5, 5, 12, 0, tzinfo=dt.UTC)


def test_fingerprint_is_stable_and_excludes_platform_side_effect_ids():
    job = WorksJobFingerprintInput(
        need_id="need-1",
        title="Translate a document",
        problem_statement="Translate the onboarding document into Japanese.",
        category_key="writing",
        job_category="writing",
        tags=["translate"],
    )
    agent = WorksAgentFingerprintInput(
        agent_id="agent-1",
        capabilities=["rewrite", "translate"],
        description="Translation agent",
        reputation={"works_completed": 2},
        read_only_release_ids=["rel-b", "rel-a"],
    )

    assert make_works_match_fingerprint(agent, job) == make_works_match_fingerprint(
        WorksAgentFingerprintInput(
            agent_id="agent-1",
            capabilities=["translate", "rewrite"],
            description="Translation agent",
            reputation={"works_completed": 2},
            read_only_release_ids=["rel-a", "rel-b"],
        ),
        job,
    )
    assert make_works_match_fingerprint(agent, job) != make_works_match_fingerprint(
        WorksAgentFingerprintInput(
            agent_id="agent-1",
            capabilities=["translate"],
            description="Translation agent",
            reputation={"works_completed": 2},
            read_only_release_ids=["rel-a", "rel-b"],
        ),
        job,
    )


def test_terminal_match_reuse_requires_same_fingerprint_and_unexpired_recheck():
    assert should_reuse_works_match(
        existing_status="declined",
        existing_fingerprint="abc",
        current_fingerprint="abc",
        next_recheck_at=None,
        now=NOW,
    )
    assert not should_reuse_works_match(
        existing_status="declined",
        existing_fingerprint="old",
        current_fingerprint="abc",
        next_recheck_at=None,
        now=NOW,
    )
    assert not should_reuse_works_match(
        existing_status="error",
        existing_fingerprint="abc",
        current_fingerprint="abc",
        next_recheck_at=NOW - dt.timedelta(seconds=1),
        now=NOW,
    )
    assert not should_reuse_works_match(
        existing_status="matched",
        existing_fingerprint="abc",
        current_fingerprint="abc",
        next_recheck_at=None,
        now=NOW,
    )


def test_matched_reuse_is_positive_match_only():
    assert should_reuse_matched_works_match(
        existing_status="matched",
        existing_fingerprint="abc",
        current_fingerprint="abc",
        next_recheck_at=None,
        now=NOW,
    )
    assert not should_reuse_matched_works_match(
        existing_status="declined",
        existing_fingerprint="abc",
        current_fingerprint="abc",
        next_recheck_at=None,
        now=NOW,
    )


def test_rank_candidates_prefers_release_match_then_overlap_history_and_name_tiebreak():
    ranked = rank_works_agent_candidates(
        [
            WorksCandidateInput(
                agent_id="a-low",
                display_name="Low",
                normalized_capability_keys=["other"],
                inferred_release_ids=[],
                completed_count=10,
                average_rating=5,
                fingerprint="fp-low",
            ),
            WorksCandidateInput(
                agent_id="a-release",
                display_name="Release",
                normalized_capability_keys=[],
                inferred_release_ids=["rel-1"],
                completed_count=0,
                average_rating=0,
                fingerprint="fp-release",
            ),
            WorksCandidateInput(
                agent_id="a-overlap",
                display_name="Overlap",
                normalized_capability_keys=["writing", "translate"],
                inferred_release_ids=[],
                completed_count=2,
                average_rating=4,
                fingerprint="fp-overlap",
            ),
        ],
        required_normalized_capability_keys={"writing", "translate"},
        has_category_tags=True,
        now=NOW,
    )

    assert [row.agent_id for row in ranked] == ["a-release", "a-overlap"]
    assert ranked[0].rank == 1
    assert ranked[0].score == 2.0
    assert ranked[1].overlap == 1.0


def test_rank_candidates_omits_reusable_terminal_matches():
    ranked = rank_works_agent_candidates(
        [
            WorksCandidateInput(
                agent_id="cached",
                display_name="Cached",
                normalized_capability_keys=["writing"],
                fingerprint="fp",
                existing_match_status="declined",
                existing_match_fingerprint="fp",
                existing_next_recheck_at=None,
            ),
            WorksCandidateInput(
                agent_id="fresh",
                display_name="Fresh",
                normalized_capability_keys=["writing"],
                fingerprint="fp2",
            ),
        ],
        required_normalized_capability_keys={"writing"},
        has_category_tags=True,
        now=NOW,
    )

    assert [row.agent_id for row in ranked] == ["fresh"]
