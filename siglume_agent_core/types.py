"""Shared value types for Siglume open-core modules.

Lives apart from any specific orchestration module so multiple modules
(prefilter, future resolver/selector) can refer to the same shape without
circular imports. These are pure dataclasses — no DB, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Canonical permission_class values. The validator (tool_manual_validator)
# ALSO authoritatively gates these three at submission time; this Literal
# keeps the public dataclass and the validator in lockstep. The underscore
# form ("read_only", not "read-only") is the canonical platform spelling
# — historical drift in this repo's tests and downstream code that used
# the hyphenated form was a representation bug fixed in v0.2.2.
PermissionClass = Literal["read_only", "action", "payment"]

# Canonical account_readiness values. The platform sets one of these
# strictly; consumers can switch on this without worrying about
# unexpected casing or alternate spellings.
AccountReadiness = Literal["ready", "missing", "unhealthy"]


@dataclass
class ResolvedToolDefinition:
    """A fully-resolved tool definition available for an agent to use.

    The platform builds these by joining capability bindings, releases, and
    listings; the open-core modules accept already-resolved values so they
    can be reasoned about without DB access. Fields mirror the platform's
    ResolvedToolDefinition so the platform can construct one and hand it
    straight to a public-package function.
    """

    binding_id: str
    grant_id: str
    release_id: str
    listing_id: str
    capability_key: str
    tool_name: str
    display_name: str
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    permission_class: PermissionClass
    approval_mode: str
    dry_run_supported: bool
    required_connected_accounts: list[dict[str, Any]]
    account_readiness: AccountReadiness
    usage_hints: list[str] = field(default_factory=list)
    result_hints: list[str] = field(default_factory=list)
    cost_hint_usd_cents: int | None = None
    settlement_mode: str | None = None
    settlement_currency: str | None = None
    settlement_network: str | None = None
    accepted_payment_tokens: list[str] = field(default_factory=list)
    compact_prompt: str = ""
    execution_adapter_config: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "AccountReadiness",
    "PermissionClass",
    "ResolvedToolDefinition",
]
