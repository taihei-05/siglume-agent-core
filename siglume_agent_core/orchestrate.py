"""Pure orchestrate loop — Tier C Phase 2 (v0.6).

The platform's :py:meth:`ToolUseRuntime.orchestrate` had a ~990 line body
mixing five concerns:

* **system-prompt build** (pure — already in :mod:`orchestrate_helpers` since v0.5)
* **per-iteration LLM tool-use loop** (pure — this module)
* **DB / outbox side-effects on the intent and receipt** (platform glue)
* **gateway dispatch** (policy / read-only execute / dry-run / owner ops)
* **cross-provider fallback when the primary adapter fails on iteration 0**

This module owns the second one. The platform passes a callback bag
(:class:`OrchestrationDispatcher`) so the loop can ask the platform to
"run this tool, check this policy, prepare this approval" without
agent-core ever importing the gateway, the ORM session, or the outbox.

Returns :class:`OrchestrationOutcome` — a record of what happened. The
platform shim reads the outcome and persists the receipt / outbox /
failure-learning rows itself. When ``final_status`` is
``"approval_required"`` the wrapper for the dispatcher callback that
detected the approval has already mutated intent state and the
``early_return_result`` field carries the ``ExecutionResult`` that the
platform should return to its caller verbatim.

Byte-equivalence contract (verified against the v0.5.0 monorepo HEAD):

* ``step_results`` dict shape per tool call — exact key set, exact ordering
* ``messages`` ToolMessage construction order across iterations
* Cross-provider fallback only fires when the primary model is OpenAI,
  the failure happened on ``iteration == 0``, AND we are not already on
  ``CROSS_PROVIDER_FALLBACK_MODEL``. A second failure propagates as the
  original exception so capability_failure_learning records the right
  ``error_class``.
* ``llm_input_tokens_total`` / ``llm_output_tokens_total`` accumulated
  via :func:`siglume_agent_core.orchestrate_helpers.extract_llm_usage`
  every turn, including the fallback turn.
* ``final_status`` derives from the per-step ``ok`` flag the same way
  the platform did: any successful step => completed; otherwise the
  last failed (non-``approval_required``) step's ``error_class`` /
  ``error_message`` win.

The loop is sync to mirror the platform's :py:meth:`orchestrate` signature.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .orchestrate_helpers import (
    OwnerOperationToolDefinition,
    extract_llm_usage,
    permission_can_run_without_approval,
)
from .provider_adapters.types import (
    ProviderToolDefinition,
    ToolMessage,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Provider routing constants — duplicated from the platform's
# tool_use_runtime so the OSS loop is self-contained. Kept byte-equivalent.
# ---------------------------------------------------------------------------

OPENAI_MODEL_PREFIXES: tuple[str, ...] = ("gpt-", "o1-", "o3-")
"""Model id prefixes routed to the OpenAI adapter."""

ANTHROPIC_MODEL_PREFIXES: tuple[str, ...] = ("claude-",)
"""Model id prefixes routed to the Anthropic adapter."""

CROSS_PROVIDER_FALLBACK_MODEL: str = "claude-haiku-4-5-20251001"
"""Model used when the primary OpenAI adapter raises on iteration 0.

Mirrors the chat path's ``_FALLBACK_MODEL`` so an OpenAI quota / outage
on the orchestrate path does not silently kill every tool-use turn.
"""


# ---------------------------------------------------------------------------
# Dispatcher callback bag — the only seam between this loop and the
# platform's gateway / DB / outbox.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestrationDispatcher:
    """Five callables the platform passes in for gateway / DB side-effects.

    Every callable receives the opaque ``intent`` object back unchanged
    so the platform can mutate ORM state inside the wrapper without the
    pure loop having to know about SQLAlchemy.

    * ``check_policy(intent, tool) -> Decision``: must return an object
      with ``.allowed: bool`` and ``.reason: str | None``. The platform
      wrapper is also the place where the first-call binding side-effect
      lives — i.e. setting ``intent.binding_id / release_id /
      permission_class`` from ``tool`` when ``intent.binding_id`` is
      still None. The pure loop never reaches into intent for this.
    * ``execute_read_only(intent, tool, args, exec_ctx) -> ExecutionResult``:
      run a resolved installed-tool call. Result must have ``.ok``,
      ``.status``, ``.structured_output``, ``.error_class``, ``.error_message``.
    * ``execute_dry_run(intent, tool, args) -> DryResult``: preview the
      call without hitting the upstream API. Result must have ``.ok``,
      ``.preview: dict``, ``.approval_snapshot_hash: str``. When
      ``.ok`` is False the loop short-circuits with ``final_status=
      "failed"`` / ``failure_error_class="approval_preview_failed"`` and
      the platform shim is expected to call its own ``_fail_intent``
      with that error class.
    * ``dispatch_owner_operation(intent, tool, args, *, require_approval)
      -> ExecutionResult``: run a first-party owner operation. The
      wrapper internally writes ``intent.plan_jsonb`` when it returns
      ``status == "approval_required"`` so the pure loop only has to
      check the status and surface the result via
      ``early_return_result``.
    * ``emit_awaiting_approval(intent, tool, *, preview,
      approval_snapshot_hash, args, total_tool_calls, step_results,
      resolved_model) -> ExecutionResult``: end-to-end "this installed
      tool needs approval" handler. The wrapper mutates ``intent.status``
      / ``intent.approval_status`` / ``intent.metadata_jsonb`` /
      ``intent.plan_jsonb``, emits the outbox event, and returns the
      ``ExecutionResult`` the orchestrate caller should see.
    """

    check_policy: Callable[[Any, Any], Any]
    execute_read_only: Callable[[Any, Any, dict[str, Any], dict[str, Any]], Any]
    execute_dry_run: Callable[[Any, Any, dict[str, Any]], Any]
    dispatch_owner_operation: Callable[..., Any]
    emit_awaiting_approval: Callable[..., Any]


# ---------------------------------------------------------------------------
# Outcome record — the only thing the loop returns to the caller.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OrchestrationOutcome:
    """Result of one full orchestrate loop run.

    ``final_status`` takes one of three values:

    * ``"completed"`` — at least one tool call succeeded (or no tool
      was called and the LLM returned end_turn). Platform shim does the
      normal post-loop bookkeeping (receipt + outbox + failure_learning).
    * ``"failed"`` — every tool call failed, or the loop hit a hard
      error (currently ``approval_preview_failed`` from a dry-run that
      itself failed). Platform shim either runs the same post-loop
      bookkeeping (LLM-reported failure case) or routes through
      ``_fail_intent`` (preview-failure case, distinguished by
      ``failure_error_class == "approval_preview_failed"``).
    * ``"approval_required"`` — short-circuit: ``early_return_result``
      carries the ExecutionResult the platform should return to its
      caller verbatim. Intent state mutation has already been done
      inside the dispatcher wrapper that detected the approval.

    The token / iteration / step counters are reported regardless of
    final_status so the platform shim can record usage even when the
    run aborted early.
    """

    final_text: str | None
    step_results: list[dict[str, Any]]
    last_tool_output: dict[str, Any] | None
    total_tool_calls: int
    iterations_used: int
    llm_input_tokens_total: int
    llm_output_tokens_total: int
    final_status: str  # "completed" | "failed" | "approval_required"
    failure_error_class: str | None
    failure_error_message: str | None
    resolved_model: str
    early_return_result: Any | None = None


# ---------------------------------------------------------------------------
# The loop itself.
# ---------------------------------------------------------------------------


def run_orchestrate_loop(
    *,
    intent: Any,
    resolved_model: str,
    tool_by_name: dict[str, Any],
    provider_tools: list[ProviderToolDefinition],
    system_prompt: str,
    initial_user_message: str,
    max_iterations: int,
    max_tool_calls: int,
    max_output_tokens: int,
    exec_ctx: dict[str, Any],
    require_approval_for_actions: bool,
    dispatcher: OrchestrationDispatcher,
    make_adapter: Callable[[str], Any],
) -> OrchestrationOutcome:
    """Run the orchestrate inner loop.

    The pure body of :py:meth:`tool_use_runtime.ToolUseRuntime.orchestrate`
    extracted in v0.6. The platform shim:

    1. Resolves intent / tools / system prompt / exec_ctx (DB heavy).
    2. Builds the :class:`OrchestrationDispatcher` with closures over
       the gateway, the ORM session, and the outbox.
    3. Calls this function.
    4. Reads the :class:`OrchestrationOutcome` and either returns
       ``early_return_result`` directly (approval path) or runs the
       post-loop receipt / outbox / failure_learning bookkeeping
       (completed / failed path).

    Arguments mirror the platform's local variables at the start of the
    loop block (line 1053 of the v0.5.0 monorepo HEAD). The
    ``tool_by_name`` mapping holds both ``ResolvedToolDefinition`` and
    ``OwnerOperationToolDefinition`` values; the loop dispatches on
    ``isinstance(tool_def, OwnerOperationToolDefinition)``.

    The ``intent`` object is opaque except for one read: ``intent.status``
    is consulted in the installed-tool approval guard (line 1286 of the
    monorepo HEAD) to skip the dry-run preview when the intent has
    already been approved out-of-band. All writes to ``intent`` happen
    inside dispatcher wrappers.
    """

    messages: list[ToolMessage] = [
        ToolMessage(role="system", content=system_prompt),
        ToolMessage(role="user", content=initial_user_message),
    ]

    adapter = make_adapter(resolved_model)

    total_tool_calls = 0
    final_text: str | None = None
    last_turn: Any | None = None
    step_results: list[dict[str, Any]] = []
    last_tool_output: dict[str, Any] | None = None
    llm_input_tokens_total = 0
    llm_output_tokens_total = 0
    iteration = 0

    for iteration in range(max_iterations):
        try:
            turn = adapter.run_turn(
                model=resolved_model,
                messages=messages,
                tools=provider_tools,
                max_output_tokens=max_output_tokens,
                tool_choice="auto",
            )
        except Exception as primary_exc:
            # Cross-provider fallback (v0.5.x stress-test fix). When OpenAI
            # raises on iteration 0 (insufficient_quota / auth / transient
            # outage), swap to the Anthropic fallback adapter for the
            # rest of this orchestrate run. We re-use the in-flight
            # ``messages`` list as-is — both adapters consume the
            # provider-neutral ``ToolMessage`` shape, and on iteration 0
            # no tool_call IDs have been recorded yet, so there is no
            # mid-loop ID-mismatch risk. Mid-loop OpenAI failure (rare)
            # propagates honestly so capability_failure_learning sees
            # the right error_class.
            is_openai_primary = any(
                resolved_model.lower().startswith(p) for p in OPENAI_MODEL_PREFIXES
            )
            if not is_openai_primary or resolved_model == CROSS_PROVIDER_FALLBACK_MODEL:
                raise
            if iteration != 0:
                raise
            logger.warning(
                "tool_use_runtime: OpenAI tool-turn failed (%s); "
                "falling back to %s for the rest of this orchestrate run",
                primary_exc,
                CROSS_PROVIDER_FALLBACK_MODEL,
            )
            resolved_model = CROSS_PROVIDER_FALLBACK_MODEL
            adapter = make_adapter(resolved_model)
            turn = adapter.run_turn(
                model=resolved_model,
                messages=messages,
                tools=provider_tools,
                max_output_tokens=max_output_tokens,
                tool_choice="auto",
            )
        last_turn = turn

        # Best-effort token accumulation. Provider may not always
        # populate the usage payload; treat missing as 0.
        usage = extract_llm_usage(turn.raw_provider_payload or {})
        llm_input_tokens_total += usage["input_tokens"]
        llm_output_tokens_total += usage["output_tokens"]

        if turn.stop_reason in ("end_turn", "max_tokens"):
            final_text = turn.assistant_text
            break

        if turn.stop_reason == "tool_use" and turn.tool_calls:
            messages.append(
                ToolMessage(
                    role="assistant",
                    content=turn.assistant_text or "",
                    tool_calls=turn.tool_calls,
                )
            )

            for call in turn.tool_calls:
                if total_tool_calls >= max_tool_calls:
                    messages.append(
                        ToolMessage(
                            role="tool",
                            content=json.dumps(
                                {
                                    "error": "tool_call_budget_exhausted",
                                    "message": f"Max {max_tool_calls} tool calls reached.",
                                }
                            ),
                            tool_call_id=call.id,
                        )
                    )
                    continue

                total_tool_calls += 1
                tool_def = tool_by_name.get(call.tool_name)
                if tool_def is None:
                    step_results.append(
                        {
                            "tool_name": call.tool_name,
                            "tool_kind": "unknown_tool",
                            "operation_name": None,
                            "ok": False,
                            "status": "failed",
                            "output": None,
                            "error_class": "unknown_tool",
                            "error_message": f"No tool named '{call.tool_name}'.",
                        }
                    )
                    messages.append(
                        ToolMessage(
                            role="tool",
                            content=json.dumps(
                                {
                                    "error": "unknown_tool",
                                    "message": f"No tool named '{call.tool_name}'.",
                                }
                            ),
                            tool_call_id=call.id,
                        )
                    )
                    continue

                if isinstance(tool_def, OwnerOperationToolDefinition):
                    call_args = dict(call.arguments or {})
                    exec_result = dispatcher.dispatch_owner_operation(
                        intent,
                        tool_def,
                        call_args,
                        require_approval=require_approval_for_actions,
                    )
                    step_results.append(
                        {
                            "tool_name": call.tool_name,
                            "tool_kind": "owner_operation",
                            "operation_name": tool_def.operation_name,
                            "ok": exec_result.ok,
                            "status": exec_result.status,
                            "output": exec_result.structured_output,
                            "error_class": exec_result.error_class,
                            "error_message": exec_result.error_message,
                        }
                    )
                    if exec_result.ok and exec_result.structured_output is not None:
                        last_tool_output = exec_result.structured_output

                    if exec_result.status == "approval_required":
                        # Wrapper has already written intent.plan_jsonb.
                        # Surface the result verbatim so the platform
                        # shim returns it to its caller.
                        return OrchestrationOutcome(
                            final_text=exec_result.summary,
                            step_results=step_results,
                            last_tool_output=exec_result.structured_output,
                            total_tool_calls=total_tool_calls,
                            iterations_used=iteration + 1,
                            llm_input_tokens_total=llm_input_tokens_total,
                            llm_output_tokens_total=llm_output_tokens_total,
                            final_status="approval_required",
                            failure_error_class=None,
                            failure_error_message=None,
                            resolved_model=resolved_model,
                            early_return_result=exec_result,
                        )

                    if exec_result.ok:
                        tool_content = json.dumps(
                            exec_result.structured_output or {"status": "ok"},
                            ensure_ascii=False,
                            default=str,
                        )
                    else:
                        tool_content = json.dumps(
                            {
                                "error": exec_result.error_class or "execution_failed",
                                "message": exec_result.error_message or "",
                            },
                            ensure_ascii=False,
                            default=str,
                        )
                    messages.append(
                        ToolMessage(
                            role="tool",
                            content=tool_content,
                            tool_call_id=call.id,
                        )
                    )
                    continue

                # Resolved installed-tool path. The platform's check_policy
                # wrapper folds in the first-call binding side-effect.
                decision = dispatcher.check_policy(intent, tool_def)
                if not decision.allowed:
                    step_results.append(
                        {
                            "tool_name": call.tool_name,
                            "tool_kind": "installed_tool",
                            "operation_name": None,
                            "ok": False,
                            "status": "failed",
                            "output": None,
                            "error_class": "policy_denied",
                            "error_message": decision.reason or "Policy check failed.",
                        }
                    )
                    messages.append(
                        ToolMessage(
                            role="tool",
                            content=json.dumps(
                                {
                                    "error": "policy_denied",
                                    "message": decision.reason or "Policy check failed.",
                                }
                            ),
                            tool_call_id=call.id,
                        )
                    )
                    continue

                call_args = dict(call.arguments or {})
                if (
                    require_approval_for_actions
                    and not permission_can_run_without_approval(tool_def.permission_class)
                    and intent.status != "approved"
                ):
                    dry_result = dispatcher.execute_dry_run(intent, tool_def, call_args)
                    if not dry_result.ok:
                        # Option (b): surface as failed with a specific
                        # error_class so the platform shim routes through
                        # _fail_intent rather than the normal post-loop
                        # receipt path. No early_return_result — the
                        # shim builds the ExecutionResult itself.
                        return OrchestrationOutcome(
                            final_text=None,
                            step_results=step_results,
                            last_tool_output=last_tool_output,
                            total_tool_calls=total_tool_calls,
                            iterations_used=iteration + 1,
                            llm_input_tokens_total=llm_input_tokens_total,
                            llm_output_tokens_total=llm_output_tokens_total,
                            final_status="failed",
                            failure_error_class="approval_preview_failed",
                            failure_error_message=str(
                                dry_result.preview.get("error") or "Approval preview failed."
                            )[:500],
                            resolved_model=resolved_model,
                            early_return_result=None,
                        )

                    preview_payload = {
                        "status": "approval_required",
                        "tool_name": tool_def.tool_name,
                        "permission_class": tool_def.permission_class,
                        "preview": dry_result.preview,
                        "approval_snapshot_hash": dry_result.approval_snapshot_hash,
                    }
                    step_results.append(
                        {
                            "tool_name": call.tool_name,
                            "tool_kind": "installed_tool",
                            "operation_name": None,
                            "ok": True,
                            "status": "approval_required",
                            "output": preview_payload,
                            "error_class": None,
                            "error_message": None,
                        }
                    )
                    exec_result = dispatcher.emit_awaiting_approval(
                        intent,
                        tool_def,
                        preview=dry_result.preview,
                        approval_snapshot_hash=dry_result.approval_snapshot_hash,
                        args=call_args,
                        total_tool_calls=total_tool_calls,
                        step_results=step_results,
                        resolved_model=resolved_model,
                    )
                    return OrchestrationOutcome(
                        final_text=exec_result.summary,
                        step_results=step_results,
                        last_tool_output=preview_payload,
                        total_tool_calls=total_tool_calls,
                        iterations_used=iteration + 1,
                        llm_input_tokens_total=llm_input_tokens_total,
                        llm_output_tokens_total=llm_output_tokens_total,
                        final_status="approval_required",
                        failure_error_class=None,
                        failure_error_message=None,
                        resolved_model=resolved_model,
                        early_return_result=exec_result,
                    )

                exec_result = dispatcher.execute_read_only(
                    intent,
                    tool_def,
                    call_args,
                    exec_ctx,
                )
                step_results.append(
                    {
                        "tool_name": call.tool_name,
                        "tool_kind": "installed_tool",
                        "operation_name": None,
                        "ok": exec_result.ok,
                        "status": exec_result.status,
                        "output": exec_result.structured_output,
                        "error_class": exec_result.error_class,
                        "error_message": exec_result.error_message,
                    }
                )
                if exec_result.ok and exec_result.structured_output is not None:
                    last_tool_output = exec_result.structured_output

                if exec_result.ok:
                    tool_content = json.dumps(
                        exec_result.structured_output or {"status": "ok"},
                        ensure_ascii=False,
                        default=str,
                    )
                else:
                    tool_content = json.dumps(
                        {
                            "error": exec_result.error_class or "execution_failed",
                            "message": exec_result.error_message or "",
                        },
                        ensure_ascii=False,
                        default=str,
                    )
                messages.append(
                    ToolMessage(
                        role="tool",
                        content=tool_content,
                        tool_call_id=call.id,
                    )
                )
            # continue outer loop
            continue

        # Unknown stop_reason — bail with whatever assistant text we have.
        final_text = turn.assistant_text
        break

    else:
        # Loop ran to max_iterations without an end_turn / max_tokens stop.
        final_text = (last_turn.assistant_text if last_turn else None) or (
            "(orchestration exceeded max_iterations without completion)"
        )

    # Aggregate success / failure across the recorded steps. Any
    # successful step => completed. Otherwise the last failed
    # (non-approval_required) step's error_class wins.
    successful_steps = [step for step in step_results if bool(step.get("ok"))]
    failed_steps = [
        step
        for step in step_results
        if not bool(step.get("ok"))
        and str(step.get("status") or "").strip().lower() != "approval_required"
    ]
    final_status = "completed"
    failure_error_class: str | None = None
    failure_error_message: str | None = None
    if failed_steps and not successful_steps:
        failed_step = failed_steps[-1]
        final_status = "failed"
        failure_error_class = (
            str(failed_step.get("error_class") or "tool_execution_failed").strip()
            or "tool_execution_failed"
        )
        failure_error_message = (
            str(failed_step.get("error_message") or "").strip()
            or str(final_text or "").strip()
            or "All orchestrated tool invocations failed."
        )

    return OrchestrationOutcome(
        final_text=final_text,
        step_results=step_results,
        last_tool_output=last_tool_output,
        total_tool_calls=total_tool_calls,
        iterations_used=min(iteration + 1, max_iterations) if last_turn else 0,
        llm_input_tokens_total=llm_input_tokens_total,
        llm_output_tokens_total=llm_output_tokens_total,
        final_status=final_status,
        failure_error_class=failure_error_class,
        failure_error_message=failure_error_message,
        resolved_model=resolved_model,
        early_return_result=None,
    )


__all__ = [
    "ANTHROPIC_MODEL_PREFIXES",
    "CROSS_PROVIDER_FALLBACK_MODEL",
    "OPENAI_MODEL_PREFIXES",
    "OrchestrationDispatcher",
    "OrchestrationOutcome",
    "run_orchestrate_loop",
]
