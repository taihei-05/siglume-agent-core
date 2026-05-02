"""Behaviour tests for the v0.6 ``run_orchestrate_loop`` function.

Each test builds a fake adapter (records turns it returns) and a fake
``OrchestrationDispatcher`` (records calls it received and returns
canned ExecutionResult / Decision / DryResult shapes). Assertions then
check the resulting ``OrchestrationOutcome`` plus the recorded
side-effects on the fakes — same approach the v0.4 / v0.5 helper test
suite uses.

The byte-equivalence assertions (step_results dict shape, message
construction order) live in the monorepo's parity test suite, which
exercises the loop against the same fixtures the platform's old
in-line orchestrate body used. Here we cover the dispatching graph and
the early-return contracts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from siglume_agent_core.orchestrate import (
    ANTHROPIC_MODEL_PREFIXES,
    CROSS_PROVIDER_FALLBACK_MODEL,
    OPENAI_MODEL_PREFIXES,
    OrchestrationDispatcher,
    OrchestrationOutcome,
    run_orchestrate_loop,
)
from siglume_agent_core.orchestrate_helpers import OwnerOperationToolDefinition
from siglume_agent_core.provider_adapters.types import (
    NormalizedToolCall,
    ProviderToolDefinition,
    ToolTurnResult,
)
from siglume_agent_core.types import ResolvedToolDefinition

# ---------------------------------------------------------------------------
# Shared fakes
# ---------------------------------------------------------------------------


@dataclass
class _FakeIntent:
    """Opaque intent the loop only reads ``.status`` from."""

    id: str = "intent-1"
    status: str = "executing"


@dataclass
class _FakeExecutionResult:
    ok: bool
    status: str
    summary: str | None = None
    structured_output: dict[str, Any] | None = None
    error_class: str | None = None
    error_message: str | None = None


@dataclass
class _FakeDecision:
    allowed: bool
    reason: str | None = None


@dataclass
class _FakeDryResult:
    ok: bool
    preview: dict[str, Any] = field(default_factory=dict)
    approval_snapshot_hash: str = ""


class _FakeAdapter:
    """Adapter that emits a queued list of ``ToolTurnResult``s, one per turn."""

    def __init__(self, turns: list[ToolTurnResult]) -> None:
        self._turns = list(turns)
        self.run_turn_calls: list[dict[str, Any]] = []

    def run_turn(self, **kwargs: Any) -> ToolTurnResult:
        self.run_turn_calls.append(kwargs)
        if not self._turns:
            raise RuntimeError("no more queued turns")
        return self._turns.pop(0)


class _FakeAdapterRaising:
    """Adapter that raises on the first call (and optionally on subsequent
    calls). Drives the cross-provider fallback test."""

    def __init__(self, raise_on: set[int], turns_after: list[ToolTurnResult]) -> None:
        self._raise_on = raise_on
        self._turns_after = list(turns_after)
        self.run_turn_calls: list[dict[str, Any]] = []

    def run_turn(self, **kwargs: Any) -> ToolTurnResult:
        idx = len(self.run_turn_calls)
        self.run_turn_calls.append(kwargs)
        if idx in self._raise_on:
            raise RuntimeError(f"primary adapter failed at call {idx}")
        if not self._turns_after:
            raise RuntimeError("no more queued turns")
        return self._turns_after.pop(0)


def _make_resolved_tool(
    name: str = "search_news", permission_class: str = "read_only"
) -> ResolvedToolDefinition:
    return ResolvedToolDefinition(
        binding_id=f"binding-{name}",
        grant_id=f"grant-{name}",
        release_id=f"release-{name}",
        listing_id=f"listing-{name}",
        capability_key=name,
        tool_name=name,
        display_name=name.replace("_", " ").title(),
        description="search news",
        input_schema={"type": "object", "properties": {}},
        output_schema={"type": "object", "properties": {}},
        permission_class=permission_class,
        approval_mode="auto",
        dry_run_supported=True,
        required_connected_accounts=[],
        account_readiness="ready",
    )


def _make_owner_op_tool(name: str = "owner_op_tool") -> OwnerOperationToolDefinition:
    return OwnerOperationToolDefinition(
        tool_name=name,
        operation_name="owner.test",
        display_name="Owner Test",
        description="owner test op",
        input_schema={"type": "object", "properties": {}},
        permission_class="read_only",
        safety=None,
        page_href=None,
    )


def _identity_dispatcher(
    *,
    check_policy_decision: _FakeDecision | None = None,
    read_only_result: _FakeExecutionResult | None = None,
    dry_run_result: _FakeDryResult | None = None,
    owner_op_result: _FakeExecutionResult | None = None,
    awaiting_approval_result: _FakeExecutionResult | None = None,
) -> tuple[OrchestrationDispatcher, dict[str, list[Any]]]:
    """Build a dispatcher whose callables record their args and return canned
    values. The returned ``records`` dict lets the test assert how many
    times each callback fired and with what arguments."""

    records: dict[str, list[Any]] = {
        "check_policy": [],
        "execute_read_only": [],
        "execute_dry_run": [],
        "dispatch_owner_operation": [],
        "emit_awaiting_approval": [],
    }

    def _check_policy(intent: Any, tool: Any) -> _FakeDecision:
        records["check_policy"].append({"intent": intent, "tool": tool})
        return check_policy_decision or _FakeDecision(allowed=True)

    def _execute_read_only(
        intent: Any, tool: Any, args: dict, exec_ctx: dict
    ) -> _FakeExecutionResult:
        records["execute_read_only"].append(
            {"intent": intent, "tool": tool, "args": args, "exec_ctx": exec_ctx}
        )
        return read_only_result or _FakeExecutionResult(
            ok=True,
            status="completed",
            structured_output={"items": [1, 2, 3]},
        )

    def _execute_dry_run(intent: Any, tool: Any, args: dict) -> _FakeDryResult:
        records["execute_dry_run"].append({"intent": intent, "tool": tool, "args": args})
        return dry_run_result or _FakeDryResult(
            ok=True,
            preview={"shape": "ok"},
            approval_snapshot_hash="hash-1",
        )

    def _dispatch_owner_operation(
        intent: Any, tool: Any, args: dict, *, require_approval: bool
    ) -> _FakeExecutionResult:
        records["dispatch_owner_operation"].append(
            {"intent": intent, "tool": tool, "args": args, "require_approval": require_approval},
        )
        return owner_op_result or _FakeExecutionResult(
            ok=True,
            status="completed",
            structured_output={"posted": True},
            summary="Posted",
        )

    def _emit_awaiting_approval(
        intent: Any,
        tool: Any,
        *,
        preview: dict,
        approval_snapshot_hash: str,
        args: dict,
        total_tool_calls: int,
        step_results: list,
        resolved_model: str,
    ) -> _FakeExecutionResult:
        records["emit_awaiting_approval"].append(
            {
                "intent": intent,
                "tool": tool,
                "preview": preview,
                "approval_snapshot_hash": approval_snapshot_hash,
                "args": args,
                "total_tool_calls": total_tool_calls,
                "step_results_len": len(step_results),
                "resolved_model": resolved_model,
            }
        )
        return awaiting_approval_result or _FakeExecutionResult(
            ok=True,
            status="approval_required",
            summary=f"Tool {tool.tool_name} requires approval.",
            structured_output={
                "status": "approval_required",
                "tool_name": tool.tool_name,
                "permission_class": tool.permission_class,
                "preview": preview,
                "approval_snapshot_hash": approval_snapshot_hash,
            },
        )

    return (
        OrchestrationDispatcher(
            check_policy=_check_policy,
            execute_read_only=_execute_read_only,
            execute_dry_run=_execute_dry_run,
            dispatch_owner_operation=_dispatch_owner_operation,
            emit_awaiting_approval=_emit_awaiting_approval,
        ),
        records,
    )


def _run(
    *,
    adapter: Any,
    tools: list[Any],
    dispatcher: OrchestrationDispatcher,
    intent: _FakeIntent | None = None,
    resolved_model: str = "gpt-5.4-mini",
    max_iterations: int = 8,
    max_tool_calls: int = 12,
    max_output_tokens: int = 4096,
    require_approval_for_actions: bool = False,
    exec_ctx: dict[str, Any] | None = None,
) -> OrchestrationOutcome:
    """Convenience wrapper that builds tool_by_name + provider_tools and
    plugs in a make_adapter that returns the given adapter once and the
    fallback adapter (also passed in via the same ``adapter`` arg) on
    subsequent calls. Most tests need only one adapter; the
    cross-provider tests pass a dedicated builder."""

    return run_orchestrate_loop(
        intent=intent or _FakeIntent(),
        resolved_model=resolved_model,
        tool_by_name={t.tool_name: t for t in tools},
        provider_tools=[
            ProviderToolDefinition(
                name=t.tool_name,
                description="x",
                parameters={"type": "object", "properties": {}},
            )
            for t in tools
        ],
        system_prompt="SYSTEM",
        initial_user_message="USER",
        max_iterations=max_iterations,
        max_tool_calls=max_tool_calls,
        max_output_tokens=max_output_tokens,
        exec_ctx=exec_ctx or {},
        require_approval_for_actions=require_approval_for_actions,
        dispatcher=dispatcher,
        make_adapter=lambda _model: adapter,
    )


# ---------------------------------------------------------------------------
# Constants are stable
# ---------------------------------------------------------------------------


def test_constants_match_platform():
    assert OPENAI_MODEL_PREFIXES == ("gpt-", "o1-", "o3-")
    assert ANTHROPIC_MODEL_PREFIXES == ("claude-",)
    assert CROSS_PROVIDER_FALLBACK_MODEL == "claude-haiku-4-5-20251001"


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_end_turn_immediately_returns_completed_with_no_steps():
    """LLM returns end_turn on the first iteration with no tool calls.
    Loop should exit cleanly with final_status=completed and zero steps."""
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={"usage": {"input_tokens": 10, "output_tokens": 5}},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher()

    outcome = _run(adapter=adapter, tools=[], dispatcher=dispatcher)

    assert outcome.final_status == "completed"
    assert outcome.final_text == "done"
    assert outcome.step_results == []
    assert outcome.last_tool_output is None
    assert outcome.total_tool_calls == 0
    assert outcome.iterations_used == 1
    assert outcome.llm_input_tokens_total == 10
    assert outcome.llm_output_tokens_total == 5
    assert outcome.failure_error_class is None
    assert outcome.early_return_result is None
    # No callbacks fired.
    assert all(len(v) == 0 for v in records.values())


def test_one_installed_tool_call_then_end_turn_completed():
    """tool_use turn -> execute_read_only -> end_turn. Records one step,
    last_tool_output set, completed."""
    tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="calling tool",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={"q": "ai"})
                ],
                stop_reason="tool_use",
                raw_provider_payload={"usage": {"input_tokens": 3, "output_tokens": 2}},
            ),
            ToolTurnResult(
                assistant_text="here is the result",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={"usage": {"input_tokens": 4, "output_tokens": 7}},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher()

    outcome = _run(adapter=adapter, tools=[tool], dispatcher=dispatcher)

    assert outcome.final_status == "completed"
    assert outcome.final_text == "here is the result"
    assert outcome.total_tool_calls == 1
    assert outcome.iterations_used == 2
    assert outcome.llm_input_tokens_total == 7
    assert outcome.llm_output_tokens_total == 9
    assert outcome.last_tool_output == {"items": [1, 2, 3]}
    assert len(outcome.step_results) == 1
    step = outcome.step_results[0]
    assert step["tool_name"] == tool.tool_name
    assert step["tool_kind"] == "installed_tool"
    assert step["operation_name"] is None
    assert step["ok"] is True
    assert step["status"] == "completed"
    assert step["error_class"] is None
    assert step["error_message"] is None
    assert len(records["check_policy"]) == 1
    assert len(records["execute_read_only"]) == 1
    assert records["execute_read_only"][0]["args"] == {"q": "ai"}


def test_owner_operation_call_completes_normally():
    tool = _make_owner_op_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={})
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher()

    outcome = _run(
        adapter=adapter,
        tools=[tool],
        dispatcher=dispatcher,
        require_approval_for_actions=False,
    )

    assert outcome.final_status == "completed"
    assert len(outcome.step_results) == 1
    assert outcome.step_results[0]["tool_kind"] == "owner_operation"
    assert outcome.step_results[0]["operation_name"] == tool.operation_name
    assert outcome.step_results[0]["ok"] is True
    assert outcome.last_tool_output == {"posted": True}
    # check_policy / dry_run / read_only NOT fired for owner operations.
    assert records["check_policy"] == []
    assert records["execute_read_only"] == []
    assert records["execute_dry_run"] == []
    assert len(records["dispatch_owner_operation"]) == 1
    assert records["dispatch_owner_operation"][0]["require_approval"] is False


# ---------------------------------------------------------------------------
# Early-return paths
# ---------------------------------------------------------------------------


def test_owner_operation_approval_required_returns_early():
    """When dispatch_owner_operation returns status=='approval_required',
    the loop must short-circuit, return outcome with
    final_status='approval_required' and surface the ExecutionResult
    via early_return_result."""
    tool = _make_owner_op_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="ask",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={})
                ],
                stop_reason="tool_use",
                raw_provider_payload={"usage": {"input_tokens": 2, "output_tokens": 3}},
            ),
        ]
    )
    canned = _FakeExecutionResult(
        ok=True,
        status="approval_required",
        summary="Owner op requires approval.",
        structured_output={"status": "approval_required"},
    )
    dispatcher, records = _identity_dispatcher(owner_op_result=canned)

    outcome = _run(
        adapter=adapter,
        tools=[tool],
        dispatcher=dispatcher,
        require_approval_for_actions=True,
    )

    assert outcome.final_status == "approval_required"
    assert outcome.early_return_result is canned
    assert outcome.final_text == "Owner op requires approval."
    assert outcome.last_tool_output == {"status": "approval_required"}
    assert outcome.failure_error_class is None
    assert len(outcome.step_results) == 1
    assert outcome.step_results[0]["status"] == "approval_required"
    # require_approval was forwarded.
    assert records["dispatch_owner_operation"][0]["require_approval"] is True


def test_installed_tool_approval_required_returns_early():
    """When require_approval_for_actions=True and the tool is not
    auto-approve, the loop runs dry_run, then emit_awaiting_approval,
    then short-circuits with the wrapper's ExecutionResult."""
    tool = _make_resolved_tool(permission_class="action")
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={"a": 1})
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
        ]
    )
    awaiting_canned = _FakeExecutionResult(
        ok=True,
        status="approval_required",
        summary=f"Tool {tool.tool_name} requires approval.",
        structured_output={"status": "approval_required"},
    )
    dispatcher, records = _identity_dispatcher(awaiting_approval_result=awaiting_canned)

    outcome = _run(
        adapter=adapter,
        tools=[tool],
        dispatcher=dispatcher,
        require_approval_for_actions=True,
    )

    assert outcome.final_status == "approval_required"
    assert outcome.early_return_result is awaiting_canned
    assert len(records["check_policy"]) == 1
    assert len(records["execute_dry_run"]) == 1
    assert len(records["emit_awaiting_approval"]) == 1
    assert records["execute_read_only"] == []
    # The step_results entry built BEFORE emit_awaiting_approval was passed in.
    step = outcome.step_results[0]
    assert step["status"] == "approval_required"
    assert step["ok"] is True
    assert step["output"]["preview"] == {"shape": "ok"}
    assert step["output"]["approval_snapshot_hash"] == "hash-1"
    # last_tool_output is the preview payload, not the canned wrapper output.
    assert outcome.last_tool_output["preview"] == {"shape": "ok"}


def test_installed_tool_approval_skipped_when_intent_already_approved():
    """If intent.status == 'approved' the loop must skip the dry_run gate
    and go straight to execute_read_only — even when
    require_approval_for_actions=True. Mirrors the platform's
    out-of-band approval path."""
    tool = _make_resolved_tool(permission_class="action")
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={})
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher()

    outcome = _run(
        adapter=adapter,
        tools=[tool],
        dispatcher=dispatcher,
        intent=_FakeIntent(status="approved"),
        require_approval_for_actions=True,
    )

    assert outcome.final_status == "completed"
    assert records["execute_dry_run"] == []
    assert records["emit_awaiting_approval"] == []
    assert len(records["execute_read_only"]) == 1


def test_dry_run_preview_failure_surfaces_as_failed_with_specific_error_class():
    """Option (b) contract: when execute_dry_run returns ok=False, the loop
    surfaces final_status='failed' with
    failure_error_class='approval_preview_failed'. early_return_result
    stays None — the platform shim is responsible for routing through
    its own _fail_intent."""
    tool = _make_resolved_tool(permission_class="action")
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={})
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher(
        dry_run_result=_FakeDryResult(
            ok=False,
            preview={"error": "missing required arg X"},
            approval_snapshot_hash="",
        ),
    )

    outcome = _run(
        adapter=adapter,
        tools=[tool],
        dispatcher=dispatcher,
        require_approval_for_actions=True,
    )

    assert outcome.final_status == "failed"
    assert outcome.failure_error_class == "approval_preview_failed"
    assert outcome.failure_error_message == "missing required arg X"
    assert outcome.early_return_result is None
    assert outcome.step_results == []  # No step recorded — preview failed before that.
    assert records["emit_awaiting_approval"] == []
    assert records["execute_read_only"] == []


def test_dry_run_preview_failure_with_blank_error_uses_default_message():
    """If preview['error'] is missing/blank, the loop must fall back to
    'Approval preview failed.' verbatim — matches the platform's default."""
    tool = _make_resolved_tool(permission_class="action")
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={})
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, _ = _identity_dispatcher(
        dry_run_result=_FakeDryResult(ok=False, preview={}, approval_snapshot_hash=""),
    )

    outcome = _run(
        adapter=adapter,
        tools=[tool],
        dispatcher=dispatcher,
        require_approval_for_actions=True,
    )

    assert outcome.failure_error_message == "Approval preview failed."


# ---------------------------------------------------------------------------
# Per-step error / unknown-tool / budget paths
# ---------------------------------------------------------------------------


def test_unknown_tool_records_step_and_continues():
    """LLM hallucinates a tool name. Loop records an unknown_tool step,
    feeds an error back to the LLM, and keeps going."""
    real_tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name="not_a_real_tool", arguments={})
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="recovered",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher()

    outcome = _run(adapter=adapter, tools=[real_tool], dispatcher=dispatcher)

    # Failed step + no successful step => final_status flips to 'failed'.
    assert outcome.final_status == "failed"
    assert outcome.failure_error_class == "unknown_tool"
    assert len(outcome.step_results) == 1
    assert outcome.step_results[0]["tool_kind"] == "unknown_tool"
    assert outcome.step_results[0]["error_class"] == "unknown_tool"
    assert records["check_policy"] == []
    assert records["execute_read_only"] == []


def test_unknown_tool_then_real_success_completes():
    """If a real tool succeeds AFTER an unknown_tool failure, the run
    completes — at least one successful step trumps the failure."""
    real_tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name="ghost", arguments={}),
                    NormalizedToolCall(id="call-2", tool_name=real_tool.tool_name, arguments={}),
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, _ = _identity_dispatcher()

    outcome = _run(adapter=adapter, tools=[real_tool], dispatcher=dispatcher)

    assert outcome.final_status == "completed"
    assert outcome.failure_error_class is None
    assert len(outcome.step_results) == 2


def test_policy_denied_records_step_without_executing():
    tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={})
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher(
        check_policy_decision=_FakeDecision(allowed=False, reason="rate-limited"),
    )

    outcome = _run(adapter=adapter, tools=[tool], dispatcher=dispatcher)

    assert outcome.final_status == "failed"
    assert outcome.failure_error_class == "policy_denied"
    assert outcome.failure_error_message == "rate-limited"
    assert outcome.step_results[0]["error_class"] == "policy_denied"
    assert records["execute_read_only"] == []


def test_tool_call_budget_exhausted_blocks_further_calls_in_same_turn():
    """When max_tool_calls is hit, subsequent tool calls in the SAME turn
    get a budget-exhausted error message but no step_results entry."""
    tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="call-1", tool_name=tool.tool_name, arguments={}),
                    NormalizedToolCall(id="call-2", tool_name=tool.tool_name, arguments={}),
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher()

    outcome = _run(adapter=adapter, tools=[tool], dispatcher=dispatcher, max_tool_calls=1)

    assert outcome.final_status == "completed"
    # First call counted; second was blocked before incrementing.
    assert outcome.total_tool_calls == 1
    assert len(outcome.step_results) == 1
    assert len(records["execute_read_only"]) == 1
    # The second adapter turn received the budget-exhausted tool message
    # so the LLM never sees a missing tool_result for call-2 (Anthropic
    # would 400 on resume otherwise). Inspect the messages kwarg the
    # adapter received on the second turn.
    second_turn_messages = adapter.run_turn_calls[1]["messages"]
    budget_msg = next(
        (m for m in second_turn_messages if getattr(m, "tool_call_id", None) == "call-2"),
        None,
    )
    assert budget_msg is not None, "budget-exhausted tool_result for call-2 was not emitted"
    assert budget_msg.role == "tool"
    import json as _json

    parsed = _json.loads(budget_msg.content)
    assert parsed == {
        "error": "tool_call_budget_exhausted",
        "message": "Max 1 tool calls reached.",
    }


def test_max_iterations_exhausted_falls_through_else_clause():
    """When the loop runs to max_iterations without an end_turn / max_tokens
    stop, final_text is the canned overflow string. Mirrors the
    platform's behaviour at line 1408 of the v0.5.0 monorepo HEAD."""
    tool = _make_resolved_tool()
    # Each turn uses a tool but never returns end_turn.
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text=f"iter {i}",
                tool_calls=[
                    NormalizedToolCall(id=f"call-{i}", tool_name=tool.tool_name, arguments={})
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            )
            for i in range(3)
        ]
    )
    dispatcher, _ = _identity_dispatcher()

    outcome = _run(adapter=adapter, tools=[tool], dispatcher=dispatcher, max_iterations=3)

    assert outcome.final_status == "completed"  # tool calls were successful
    assert outcome.iterations_used == 3
    assert outcome.final_text == "iter 2"
    # Note: the else-clause overrides final_text only if last_turn.assistant_text
    # is None / empty. Here we have "iter 2" so that's what's used.


def test_max_iterations_exhausted_with_no_text_uses_overflow_message():
    tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text=None,
                tool_calls=[NormalizedToolCall(id="c", tool_name=tool.tool_name, arguments={})],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="",
                tool_calls=[NormalizedToolCall(id="c2", tool_name=tool.tool_name, arguments={})],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, _ = _identity_dispatcher()

    outcome = _run(adapter=adapter, tools=[tool], dispatcher=dispatcher, max_iterations=2)

    assert outcome.final_text == "(orchestration exceeded max_iterations without completion)"


def test_unknown_stop_reason_breaks_with_assistant_text():
    tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="weird stop",
                tool_calls=[],
                stop_reason="some_other_stop_reason",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, _ = _identity_dispatcher()

    outcome = _run(adapter=adapter, tools=[tool], dispatcher=dispatcher)

    assert outcome.final_status == "completed"
    assert outcome.final_text == "weird stop"


# ---------------------------------------------------------------------------
# Read-only tool failure aggregation
# ---------------------------------------------------------------------------


def test_all_steps_failed_reports_last_failure():
    """When every step failed, final_status='failed' and the LAST failed
    step's error_class / error_message win — matches platform behaviour."""
    tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="c1", tool_name=tool.tool_name, arguments={}),
                    NormalizedToolCall(id="c2", tool_name=tool.tool_name, arguments={}),
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    failures = [
        _FakeExecutionResult(
            ok=False, status="failed", error_class="upstream_5xx", error_message="first fail"
        ),
        _FakeExecutionResult(
            ok=False, status="failed", error_class="auth_failed", error_message="second fail"
        ),
    ]

    records: list[Any] = []

    def _execute_read_only(
        intent: Any, tool: Any, args: dict, exec_ctx: dict
    ) -> _FakeExecutionResult:
        records.append({"intent": intent, "tool": tool, "args": args})
        return failures[len(records) - 1]

    dispatcher = OrchestrationDispatcher(
        check_policy=lambda i, t: _FakeDecision(allowed=True),
        execute_read_only=_execute_read_only,
        execute_dry_run=lambda i, t, a: _FakeDryResult(ok=True),
        dispatch_owner_operation=lambda i, t, a, **kw: _FakeExecutionResult(
            ok=True, status="completed"
        ),
        emit_awaiting_approval=lambda *a, **k: _FakeExecutionResult(
            ok=True, status="approval_required"
        ),
    )

    outcome = _run(adapter=adapter, tools=[tool], dispatcher=dispatcher)

    assert outcome.final_status == "failed"
    assert outcome.failure_error_class == "auth_failed"
    assert outcome.failure_error_message == "second fail"


def test_some_succeed_some_fail_reports_completed():
    """If any step succeeds, final_status='completed' regardless of how many
    other steps failed."""
    tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[
                    NormalizedToolCall(id="c1", tool_name=tool.tool_name, arguments={}),
                    NormalizedToolCall(id="c2", tool_name=tool.tool_name, arguments={}),
                ],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    results = [
        _FakeExecutionResult(ok=False, status="failed", error_class="x"),
        _FakeExecutionResult(ok=True, status="completed", structured_output={"k": 1}),
    ]
    counter = {"i": 0}

    def _execute_read_only(
        intent: Any, tool: Any, args: dict, exec_ctx: dict
    ) -> _FakeExecutionResult:
        r = results[counter["i"]]
        counter["i"] += 1
        return r

    dispatcher = OrchestrationDispatcher(
        check_policy=lambda i, t: _FakeDecision(allowed=True),
        execute_read_only=_execute_read_only,
        execute_dry_run=lambda i, t, a: _FakeDryResult(ok=True),
        dispatch_owner_operation=lambda i, t, a, **kw: _FakeExecutionResult(
            ok=True, status="completed"
        ),
        emit_awaiting_approval=lambda *a, **k: _FakeExecutionResult(
            ok=True, status="approval_required"
        ),
    )

    outcome = _run(adapter=adapter, tools=[tool], dispatcher=dispatcher)

    assert outcome.final_status == "completed"
    assert outcome.failure_error_class is None


# ---------------------------------------------------------------------------
# Cross-provider fallback
# ---------------------------------------------------------------------------


def test_cross_provider_fallback_on_iteration_zero_with_openai_primary():
    """First adapter call raises; loop swaps to fallback model and retries."""
    primary = _FakeAdapterRaising(raise_on={0}, turns_after=[])
    fallback = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="recovered via fallback",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={"usage": {"input_tokens": 1, "output_tokens": 1}},
            ),
        ]
    )
    dispatcher, _ = _identity_dispatcher()

    adapters_made: list[str] = []

    def _make_adapter(model: str) -> Any:
        adapters_made.append(model)
        if len(adapters_made) == 1:
            return primary
        return fallback

    outcome = run_orchestrate_loop(
        intent=_FakeIntent(),
        resolved_model="gpt-5.4-mini",
        tool_by_name={},
        provider_tools=[],
        system_prompt="S",
        initial_user_message="U",
        max_iterations=4,
        max_tool_calls=12,
        max_output_tokens=4096,
        exec_ctx={},
        require_approval_for_actions=False,
        dispatcher=dispatcher,
        make_adapter=_make_adapter,
    )

    assert outcome.final_status == "completed"
    assert outcome.final_text == "recovered via fallback"
    assert outcome.resolved_model == CROSS_PROVIDER_FALLBACK_MODEL
    # Two adapters built: gpt then claude.
    assert adapters_made == ["gpt-5.4-mini", CROSS_PROVIDER_FALLBACK_MODEL]
    # Token usage on the fallback turn is still recorded — customers are
    # billed for these tokens, so a regression that dropped extraction on
    # the fallback path would be silent in production.
    assert outcome.llm_input_tokens_total == 1
    assert outcome.llm_output_tokens_total == 1


def test_cross_provider_fallback_does_not_fire_on_iteration_one():
    """Mid-loop OpenAI failure must propagate, not silently swap. The
    in-flight messages list already has OpenAI tool_call IDs that
    Anthropic would 400 on resume."""
    tool = _make_resolved_tool()
    # Iteration 0 succeeds with a tool_use; iteration 1 raises.
    primary = _FakeAdapterRaising(
        raise_on={1},
        turns_after=[
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[NormalizedToolCall(id="c", tool_name=tool.tool_name, arguments={})],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
        ],
    )
    dispatcher, _ = _identity_dispatcher()

    with pytest.raises(RuntimeError, match="primary adapter failed at call 1"):
        run_orchestrate_loop(
            intent=_FakeIntent(),
            resolved_model="gpt-5.4-mini",
            tool_by_name={tool.tool_name: tool},
            provider_tools=[
                ProviderToolDefinition(
                    name=tool.tool_name,
                    description="x",
                    parameters={"type": "object", "properties": {}},
                ),
            ],
            system_prompt="S",
            initial_user_message="U",
            max_iterations=4,
            max_tool_calls=12,
            max_output_tokens=4096,
            exec_ctx={},
            require_approval_for_actions=False,
            dispatcher=dispatcher,
            make_adapter=lambda _model: primary,
        )


def test_cross_provider_fallback_does_not_fire_when_primary_is_anthropic():
    """When the primary model is already Anthropic, a failure must propagate
    — there's no further fallback to swap to."""
    primary = _FakeAdapterRaising(raise_on={0}, turns_after=[])
    dispatcher, _ = _identity_dispatcher()

    with pytest.raises(RuntimeError, match="primary adapter failed at call 0"):
        run_orchestrate_loop(
            intent=_FakeIntent(),
            resolved_model="claude-opus-4-7",
            tool_by_name={},
            provider_tools=[],
            system_prompt="S",
            initial_user_message="U",
            max_iterations=4,
            max_tool_calls=12,
            max_output_tokens=4096,
            exec_ctx={},
            require_approval_for_actions=False,
            dispatcher=dispatcher,
            make_adapter=lambda _model: primary,
        )


def test_cross_provider_fallback_does_not_fire_when_already_fallback_model():
    """If somehow we're already running the fallback model and it fails on
    iter 0, propagate honestly — no infinite swap loop."""
    primary = _FakeAdapterRaising(raise_on={0}, turns_after=[])
    dispatcher, _ = _identity_dispatcher()

    with pytest.raises(RuntimeError):
        run_orchestrate_loop(
            intent=_FakeIntent(),
            resolved_model=CROSS_PROVIDER_FALLBACK_MODEL,
            tool_by_name={},
            provider_tools=[],
            system_prompt="S",
            initial_user_message="U",
            max_iterations=4,
            max_tool_calls=12,
            max_output_tokens=4096,
            exec_ctx={},
            require_approval_for_actions=False,
            dispatcher=dispatcher,
            make_adapter=lambda _model: primary,
        )


# ---------------------------------------------------------------------------
# exec_ctx pass-through
# ---------------------------------------------------------------------------


def test_exec_ctx_passed_to_execute_read_only():
    tool = _make_resolved_tool()
    adapter = _FakeAdapter(
        [
            ToolTurnResult(
                assistant_text="x",
                tool_calls=[NormalizedToolCall(id="c", tool_name=tool.tool_name, arguments={})],
                stop_reason="tool_use",
                raw_provider_payload={},
            ),
            ToolTurnResult(
                assistant_text="done",
                tool_calls=[],
                stop_reason="end_turn",
                raw_provider_payload={},
            ),
        ]
    )
    dispatcher, records = _identity_dispatcher()

    _run(
        adapter=adapter,
        tools=[tool],
        dispatcher=dispatcher,
        exec_ctx={"agent_id": "agent-1", "intent_id": "intent-1", "job_reference": "job-99"},
    )

    assert records["execute_read_only"][0]["exec_ctx"] == {
        "agent_id": "agent-1",
        "intent_id": "intent-1",
        "job_reference": "job-99",
    }
