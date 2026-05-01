"""Public parity tests for score_manual_quality.

Closes review item #9 from the v0.2 audit: the README claims
"production runs the same code that's visible here", but until v0.2.4
the parity test backing that claim lived in the private monorepo —
external contributors couldn't verify it themselves.

This test loads four representative tool manuals from
``tests/fixtures/manuals/`` (high / medium / low / structurally-broken
quality) and asserts ``score_manual_quality`` produces the exact frozen
output recorded in ``tests/fixtures/expected_scores.json``.

Why it works as a parity check
------------------------------

The Siglume monorepo's prod runtime imports ``siglume_agent_core`` from
PyPI starting at v0.2.0 (see siglume/pull/205). The PyPI artifact is
built from this repo's tagged release. So:

    score in this fixture
      == score from `siglume_agent_core` installed from PyPI
      == score the Siglume server returns at submission time

If a future PR drifts the scorer, this test fails locally. The same PR
must either fix the regression (preserving the snapshot) or
intentionally update the snapshot (calling out the user-visible change
in the CHANGELOG).

If the assertion message points at unexpected grade drift, that's the
pin doing its job — investigate before bumping the snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from siglume_agent_core.tool_manual_validator import score_manual_quality

FIXTURES_DIR = Path(__file__).parent / "fixtures"
MANUALS_DIR = FIXTURES_DIR / "manuals"
EXPECTED_FILE = FIXTURES_DIR / "expected_scores.json"


def _load_expected() -> dict:
    with EXPECTED_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def _load_manual(filename: str) -> dict:
    with (MANUALS_DIR / filename).open("r", encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize(
    "fixture_name",
    [
        "high_quality.json",
        "medium_quality.json",
        "low_quality.json",
        "structurally_broken.json",
    ],
)
def test_quality_score_matches_frozen_snapshot(fixture_name: str) -> None:
    """The scorer must produce the exact same grade and overall_score
    as the pinned snapshot for each fixture. Drift = either a code
    regression or an intentional behaviour change requiring a snapshot
    update in the same PR (and a CHANGELOG entry)."""
    expected = _load_expected()["fixtures"][fixture_name]
    manual = _load_manual(fixture_name)
    quality = score_manual_quality(manual)

    assert quality.grade == expected["grade"], (
        f"{fixture_name}: grade drifted from {expected['grade']!r} to {quality.grade!r} "
        f"(score={quality.overall_score}). Either fix the scorer regression "
        f"or update tests/fixtures/expected_scores.json with a CHANGELOG note."
    )
    assert quality.overall_score == expected["overall_score"], (
        f"{fixture_name}: overall_score drifted from {expected['overall_score']} "
        f"to {quality.overall_score}. Same remediation: fix or pin."
    )
    assert quality.keyword_coverage_estimate == expected["keyword_coverage_estimate"], (
        f"{fixture_name}: keyword_coverage_estimate drifted from "
        f"{expected['keyword_coverage_estimate']} to {quality.keyword_coverage_estimate}."
    )


def test_grade_thresholds_documented_correctly() -> None:
    """The grade thresholds documented in expected_scores.json's
    `_grade_thresholds` field must agree with the actual scorer
    boundaries. If a future scorer change moves the thresholds, both
    the snapshot's documented bounds AND the scorer must be updated.
    """
    from siglume_agent_core.tool_manual_validator import _overall_to_grade

    cases = [
        (90, "A"),
        (89, "B"),
        (70, "B"),
        (69, "C"),
        (50, "C"),
        (49, "D"),
        (30, "D"),
        (29, "F"),
        (0, "F"),
    ]
    for score, expected_grade in cases:
        assert _overall_to_grade(score) == expected_grade, (
            f"_overall_to_grade({score}) returned {_overall_to_grade(score)!r}, "
            f"expected {expected_grade!r}"
        )


def test_fixture_set_covers_a_through_f_grade_range() -> None:
    """Sanity: the fixtures collectively touch enough of the grading
    spectrum to make drift-detection meaningful. We pin A, B, C, F.
    If you add a D fixture later, append the grade letter here."""
    expected = _load_expected()["fixtures"]
    grades = {entry["grade"] for entry in expected.values()}
    assert grades >= {"A", "B", "F"}, f"Fixture set thinned: only covers {grades}"
